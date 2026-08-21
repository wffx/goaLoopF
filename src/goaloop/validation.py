"""Static artifact policy, build planning and deterministic execution decisions."""

from __future__ import annotations

import re
from pathlib import Path

from .models import (
    BuildPlan,
    CoverageDecisionPolicy,
    CoverageMetrics,
    ExecutionDisposition,
    GeneratedArtifactSet,
    GenerationDecision,
    GenerationFeedback,
    HarnessExecutionResult,
    ProcessRequest,
    ProcessResult,
    ValidationProfile,
)

REQUIRED_EXACT_FILES = {"Makefile", "build.sh", "endpoint.json", "README.fuzz.md"}
HARNESS_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_FUZZER_STATS = re.compile(
    r"cov:\s*(?P<cov>\d+).*?ft:\s*(?P<features>\d+).*?corp:\s*(?P<corpus>\d+)"
    r"(?:/\S+)?(?:.*?exec/s:\s*(?P<execs>\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)


class ArtifactPolicyError(ValueError):
    """A generated candidate violates controller-owned policy."""


def validate_generated_artifacts(
    artifacts: GeneratedArtifactSet,
    profile: ValidationProfile,
) -> None:
    """Reject incomplete or over-privileged model output before materialization."""

    paths = {item.path for item in artifacts.files}
    missing = sorted(REQUIRED_EXACT_FILES - paths)
    if missing:
        raise ArtifactPolicyError(f"required generated files are missing: {', '.join(missing)}")

    harness_file = artifacts.endpoint_plan.build.harness_file
    if harness_file not in paths:
        raise ArtifactPolicyError(f"declared harness file was not generated: {harness_file}")
    if Path(harness_file).suffix.lower() not in HARNESS_SUFFIXES:
        raise ArtifactPolicyError("harness file must be C or C++ source")

    endpoint_file = next(item for item in artifacts.files if item.path == "endpoint.json")
    if not endpoint_file.content.strip():
        raise ArtifactPolicyError("endpoint.json must not be empty")

    _validate_build_tokens(artifacts.endpoint_plan.build, profile)


def assemble_compile_request(
    artifacts: GeneratedArtifactSet,
    profile: ValidationProfile,
    source_root: Path,
    candidate_dir: Path,
) -> ProcessRequest:
    """Build a compiler argv from structured data; generated scripts are never executed."""

    validate_generated_artifacts(artifacts, profile)
    source_root = source_root.resolve()
    candidate_dir = candidate_dir.resolve()
    build = artifacts.endpoint_plan.build
    compiler = profile.tools.clang if build.compiler == "clang" else profile.tools.clangxx

    harness = _contained(candidate_dir, build.harness_file, "harness file")
    binary = _contained(candidate_dir, build.binary_name, "fuzzer binary")
    target_sources = [_contained(source_root, item, "target source") for item in build.target_sources]
    include_dirs = [_contained(source_root, item, "include directory") for item in build.include_dirs]

    argv = [
        compiler,
        "-fsanitize=fuzzer,address,undefined",
        "-fprofile-instr-generate",
        "-fcoverage-mapping",
        str(harness),
        *(str(item) for item in target_sources),
        *(f"-I{item}" for item in [*profile.default_include_dirs, *include_dirs]),
        *(f"-D{item}" for item in [*profile.default_defines, *build.defines]),
        *build.cflags,
        *build.ldflags,
        *(_library_argument(item) for item in build.libraries),
        "-o",
        str(binary),
    ]
    return ProcessRequest(
        argv=argv,
        cwd=candidate_dir,
        timeout_seconds=profile.resources.timeout_seconds,
    )


def assemble_fuzz_request(
    *,
    binary: Path,
    corpus_dir: Path,
    crashes_dir: Path,
    fuzz_seconds: int,
    timeout_seconds: int,
) -> ProcessRequest:
    """Build the bounded libFuzzer argv for one candidate execution.

    Exactly one ``compile + bounded fuzz`` pass per candidate; there is no
    separate smoke run. Crash artifacts are written to the crashes directory.
    """
    argv = [
        str(binary),
        str(corpus_dir),
        "-timeout=5",
        "-rss_limit_mb=2048",
        "-max_len=1048576",
        f"-max_total_time={fuzz_seconds}",
        f"-artifact_prefix={crashes_dir}/",
        "-print_final_stats=1",
    ]
    return ProcessRequest(argv=argv, cwd=corpus_dir.parent, timeout_seconds=timeout_seconds)


def parse_libfuzzer_metrics(output: str) -> CoverageMetrics:
    """Extract initial/final libFuzzer counters without depending on one banner variant."""

    samples: list[tuple[int, int, int, float | None]] = []
    for match in _FUZZER_STATS.finditer(output):
        execs = match.group("execs")
        samples.append(
            (
                int(match.group("cov")),
                int(match.group("features")),
                int(match.group("corpus")),
                float(execs) if execs is not None else None,
            )
        )
    if not samples:
        return CoverageMetrics()
    initial = samples[0]
    final = samples[-1]
    latest_execs = next((item[3] for item in reversed(samples) if item[3] is not None), None)
    return CoverageMetrics(
        initial_cov=initial[0],
        final_cov=final[0],
        cov_delta=max(0, final[0] - initial[0]),
        initial_features=initial[1],
        final_features=final[1],
        feature_delta=max(0, final[1] - initial[1]),
        initial_corpus=initial[2],
        final_corpus=final[2],
        corpus_delta=max(0, final[2] - initial[2]),
        execs_per_second=latest_execs,
    )


def detect_sanitizer(output: str) -> str | None:
    """Return a stable sanitizer/error category from process output."""

    lowered = output.lower()
    signatures = (
        ("addresssanitizer", "address"),
        ("undefinedbehaviorsanitizer", "undefined"),
        ("runtime error:", "undefined"),
        ("memorysanitizer", "memory"),
        ("threadsanitizer", "thread"),
        ("assertion failed", "assertion"),
        ("failed assertion", "assertion"),
        ("deadly signal", "signal"),
        ("libfuzzer: timeout after", "timeout"),
        ("error: libfuzzer: timeout", "timeout"),
    )
    return next((kind for marker, kind in signatures if marker in lowered), None)


def decide_generation(
    execution: HarnessExecutionResult,
    policy: CoverageDecisionPolicy,
) -> GenerationDecision:
    """Apply the profile's coverage policy to controller-collected evidence."""

    if execution.disposition is not ExecutionDisposition.ACCEPTED:
        feedback = None
        if execution.disposition is ExecutionDisposition.NEEDS_REGENERATION:
            feedback = _feedback_from_execution(execution)
        return GenerationDecision(
            disposition=execution.disposition,
            completes_goal=False,
            reason=execution.reason,
            feedback=feedback,
        )

    failures: list[str] = []
    metrics = execution.coverage
    if policy.require_target_function_hit and not metrics.target_function_hit:
        failures.append("target function was not hit")
    if metrics.cov_delta < policy.min_libfuzzer_cov_delta:
        failures.append("libFuzzer coverage delta is below policy")
    if metrics.feature_delta < policy.min_feature_delta:
        failures.append("feature delta is below policy")
    if metrics.corpus_delta < policy.min_corpus_delta:
        failures.append("corpus delta is below policy")
    if metrics.target_line_delta < policy.min_target_line_delta:
        failures.append("target line coverage delta is below policy")
    if metrics.execs_per_second is not None and metrics.execs_per_second < policy.min_execs_per_second:
        failures.append("execution rate is below policy")
    positive_growth = any(
        value > 0
        for value in (
            metrics.cov_delta,
            metrics.feature_delta,
            metrics.corpus_delta,
            metrics.target_line_delta,
        )
    )
    if policy.require_any_positive_growth and not positive_growth:
        failures.append("no positive coverage, feature, corpus, or target-line growth")

    if failures:
        reason = "; ".join(failures)
        return GenerationDecision(
            disposition=ExecutionDisposition.NEEDS_REGENERATION,
            completes_goal=False,
            reason=reason,
            feedback=_feedback_from_execution(execution, reason=reason),
        )
    return GenerationDecision(
        disposition=ExecutionDisposition.ACCEPTED,
        completes_goal=True,
        reason="candidate compiled, reached the target, and satisfied coverage policy",
    )


def make_execution_result(
    *,
    run_id: str,
    generation_loop: int,
    compile_result: ProcessResult,
    fuzz_result: ProcessResult | None,
    coverage: CoverageMetrics | None = None,
    coverage_valid: bool = True,
    crash_artifact: str | None = None,
) -> HarnessExecutionResult:
    """Classify raw process evidence into exactly one workflow disposition."""

    metrics = coverage or CoverageMetrics()
    if compile_result.timed_out or compile_result.exit_code is None:
        disposition = ExecutionDisposition.ENVIRONMENT_ERROR
        reason = "compiler did not return a usable result"
    elif compile_result.exit_code != 0:
        disposition = ExecutionDisposition.NEEDS_REGENERATION
        reason = "candidate failed to compile"
    elif fuzz_result is None:
        disposition = ExecutionDisposition.ENVIRONMENT_ERROR
        reason = "fuzzer process result is missing"
    else:
        # Crash evidence (sanitizer report, crash artifact, timeout) takes
        # priority over coverage attribution: a crashing fuzzer aborts before
        # flushing its .profraw, so missing coverage is expected, not an
        # environment error. Without crash evidence, missing/corrupt coverage
        # artifacts are an environment_error per the coverage decision policy.
        output = f"{fuzz_result.stdout}\n{fuzz_result.stderr}"
        sanitizer = detect_sanitizer(output)
        if sanitizer is not None or crash_artifact is not None or fuzz_result.timed_out:
            disposition = ExecutionDisposition.CRASH_CANDIDATE
            reason = "fuzzing produced sanitizer, crash, assertion, or timeout evidence"
        elif not coverage_valid:
            disposition = ExecutionDisposition.ENVIRONMENT_ERROR
            reason = "coverage artifacts are missing, corrupt, or cannot be attributed"
        elif fuzz_result.exit_code not in (0, None):
            disposition = ExecutionDisposition.NEEDS_REGENERATION
            reason = "fuzzer exited abnormally without product crash evidence"
        else:
            disposition = ExecutionDisposition.ACCEPTED
            reason = "execution completed; coverage policy evaluation is required"
        return HarnessExecutionResult(
            run_id=run_id,
            generation_loop=generation_loop,
            disposition=disposition,
            compile_result=compile_result,
            fuzz_result=fuzz_result,
            coverage=metrics,
            sanitizer_kind=sanitizer,
            crash_artifact=crash_artifact,
            reason=reason,
        )
    return HarnessExecutionResult(
        run_id=run_id,
        generation_loop=generation_loop,
        disposition=disposition,
        compile_result=compile_result,
        fuzz_result=fuzz_result,
        coverage=metrics,
        crash_artifact=crash_artifact,
        reason=reason,
    )


def _validate_build_tokens(build: BuildPlan, profile: ValidationProfile) -> None:
    for flag in (*build.cflags, *build.ldflags):
        if not any(flag.startswith(prefix) for prefix in profile.allowed_compiler_flags):
            raise ArtifactPolicyError(f"compiler flag is not allowed by profile: {flag}")
    disallowed = sorted(set(build.libraries) - set(profile.allowed_libraries))
    if disallowed:
        raise ArtifactPolicyError(f"libraries are not allowed by profile: {', '.join(disallowed)}")


def _contained(root: Path, relative: str, label: str) -> Path:
    destination = (root / relative).resolve()
    if not destination.is_relative_to(root):
        raise ArtifactPolicyError(f"{label} escapes its allowed root: {relative}")
    return destination


def _library_argument(value: str) -> str:
    return value if value.startswith("-") else f"-l{value}"


def _feedback_from_execution(
    execution: HarnessExecutionResult,
    *,
    reason: str | None = None,
) -> GenerationFeedback:
    metrics = execution.coverage
    log = execution.compile_result.stderr
    if execution.fuzz_result is not None:
        log = f"{log}\n{execution.fuzz_result.stderr}"
    return GenerationFeedback(
        category=execution.disposition.value,
        summary=reason or execution.reason,
        compile_exit_code=execution.compile_result.exit_code,
        sanitizer_kind=execution.sanitizer_kind,
        cov_delta=metrics.cov_delta,
        feature_delta=metrics.feature_delta,
        corpus_delta=metrics.corpus_delta,
        target_function_hit=metrics.target_function_hit,
        target_line_coverage=metrics.target_line_coverage,
        log_excerpt=log[-8000:] or None,
    )
