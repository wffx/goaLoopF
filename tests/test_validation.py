"""Validation module tests: artifact policy, argv assembly, log parsing, decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from goaloop.models import (
    BuildPlan,
    CoverageDecisionPolicy,
    ExecutionDisposition,
    GeneratedArtifactSet,
    ProcessResult,
    ValidationProfile,
)
from goaloop.validation import (
    ArtifactPolicyError,
    assemble_compile_request,
    assemble_fuzz_request,
    decide_generation,
    detect_sanitizer,
    find_build_output_binary,
    make_execution_result,
    parse_libfuzzer_metrics,
    validate_generated_artifacts,
)

from .helpers import make_artifact_payload, make_build_dir_artifact_payload

FUZZER_STATS_SAMPLE = """INFO: Running with entropic power schedule (0xFF, 100).
#2\tINITED cov: 4 ft: 5 corp: 1/1b exec/s: 0 rss: 25Mb
#10\tNEW    cov: 9 ft: 12 corp: 4/12b exec/s: 120 rss: 25Mb
#42\tNEW    cov: 14 ft: 21 corp: 9/47b exec/s: 240 rss: 26Mb
stat::number_of_executed_units: 42
stat::average_exec_per_sec: 240
"""


def _artifacts(workspace_root: Path, helper: dict | None = None, **overrides: object) -> GeneratedArtifactSet:
    payload = make_artifact_payload("safe", "safe_parse", **(helper or {}))
    payload.update(overrides)
    return GeneratedArtifactSet.model_validate(
        {
            "run_id": "run-v",
            "generation_loop": 1,
            **payload,
        }
    )


class TestArtifactPolicy:
    def test_required_files(self, workspace_root: Path) -> None:
        artifacts = _artifacts(workspace_root)
        artifacts.files = [item for item in artifacts.files if item.path != "README.fuzz.md"]
        with pytest.raises(ArtifactPolicyError, match="README.fuzz.md"):
            validate_generated_artifacts(artifacts, ValidationProfile(name="default"))

    def test_harness_file_must_exist(self, workspace_root: Path) -> None:
        artifacts = _artifacts(workspace_root)
        artifacts.endpoint_plan.build.harness_file = "missing.c"
        with pytest.raises(ArtifactPolicyError, match="not generated"):
            validate_generated_artifacts(artifacts, ValidationProfile(name="default"))

    def test_harness_suffix(self, workspace_root: Path) -> None:
        artifacts = _artifacts(workspace_root, helper={"harness_file": "harness.txt"})
        with pytest.raises(ArtifactPolicyError):
            validate_generated_artifacts(artifacts, ValidationProfile(name="default"))

    def test_disallowed_flag(self, workspace_root: Path) -> None:
        build = {
            "compiler": "clang",
            "harness_file": "harness_safe.c",
            "target_sources": ["src/safe.c"],
            "include_dirs": [],
            "defines": [],
            "cflags": ["-Xclang", "-load"],
            "ldflags": [],
            "libraries": [],
            "binary_name": "fuzzer",
        }
        artifacts = _artifacts(workspace_root)
        artifacts.endpoint_plan.build = BuildPlan.model_validate(build)
        with pytest.raises(ArtifactPolicyError, match="flag"):
            validate_generated_artifacts(artifacts, ValidationProfile(name="default"))

    def test_disallowed_library(self, workspace_root: Path) -> None:
        build = {
            "compiler": "clang",
            "harness_file": "harness_safe.c",
            "target_sources": ["src/safe.c"],
            "include_dirs": [],
            "defines": [],
            "cflags": ["-g"],
            "ldflags": [],
            "libraries": ["shell"],
            "binary_name": "fuzzer",
        }
        artifacts = _artifacts(workspace_root)
        artifacts.endpoint_plan.build = BuildPlan.model_validate(build)
        with pytest.raises(ArtifactPolicyError, match="not allowed by profile"):
            validate_generated_artifacts(artifacts, ValidationProfile(name="default"))

    def test_build_dir_accepts_only_harness(self, workspace_root: Path) -> None:
        artifacts = GeneratedArtifactSet.model_validate(
            {
                "run_id": "run-build",
                "generation_loop": 1,
                **make_build_dir_artifact_payload("safe_parse"),
            }
        )
        validate_generated_artifacts(
            artifacts,
            ValidationProfile(name="default"),
            build_dir_mode=True,
        )

    def test_build_dir_rejects_extra_stub(self, workspace_root: Path) -> None:
        payload = make_build_dir_artifact_payload("safe_parse")
        payload["files"].append(
            {"path": "stub.c", "content": "int missing(void) { return 0; }", "purpose": "stub"}
        )
        artifacts = GeneratedArtifactSet.model_validate(
            {"run_id": "run-build", "generation_loop": 1, **payload}
        )
        with pytest.raises(ArtifactPolicyError, match="exactly one file"):
            validate_generated_artifacts(
                artifacts,
                ValidationProfile(name="default"),
                build_dir_mode=True,
            )

    def test_build_dir_rejects_model_build_flags(self, workspace_root: Path) -> None:
        payload = make_build_dir_artifact_payload("safe_parse")
        payload["endpoint_plan"]["build"]["cflags"] = ["-g"]
        artifacts = GeneratedArtifactSet.model_validate(
            {"run_id": "run-build", "generation_loop": 1, **payload}
        )
        with pytest.raises(ArtifactPolicyError, match="arrays must be empty"):
            validate_generated_artifacts(
                artifacts,
                ValidationProfile(name="default"),
                build_dir_mode=True,
            )


class TestCompileAssembly:
    def test_argv_shape(self, workspace_root: Path) -> None:
        artifacts = _artifacts(workspace_root)
        source_root = workspace_root / "repos" / "safe"
        candidate = workspace_root / "work" / "safe" / "runs" / "r" / "iterations" / "loop-01" / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "harness_safe.c").write_text("int main(){}", encoding="utf-8")
        request = assemble_compile_request(
            artifacts,
            ValidationProfile(name="default"),
            source_root,
            candidate,
        )
        assert request.argv[0] == "clang"
        assert "-fsanitize=fuzzer,address,undefined" in request.argv
        assert "-fprofile-instr-generate" in request.argv
        assert "-fcoverage-mapping" in request.argv
        assert request.argv[-2:] == ["-o", str(candidate / "fuzzer")]
        assert any(str(source_root / "src" / "safe.c") == item for item in request.argv)


class TestBuildOutputDiscovery:
    def test_marker_finds_executable_in_nested_directory(self, tmp_path: Path) -> None:
        binary = tmp_path / "out" / "fuzzer"
        binary.parent.mkdir()
        binary.write_text("binary", encoding="utf-8")
        binary.chmod(0o755)
        assert find_build_output_binary("GOALOOP_FUZZER=out/fuzzer\n", tmp_path) == binary

    def test_compiler_output_is_supported(self, tmp_path: Path) -> None:
        binary = tmp_path / "bin" / "target fuzzer"
        binary.parent.mkdir()
        binary.write_text("binary", encoding="utf-8")
        binary.chmod(0o755)
        output = "clang src/harness.c -o 'bin/target fuzzer'\n"
        assert find_build_output_binary(output, tmp_path) == binary

    def test_explicit_marker_accepts_executable_outside_build_dir(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-fuzzer"
        outside.write_text("binary", encoding="utf-8")
        outside.chmod(0o755)
        assert find_build_output_binary(f"GOALOOP_FUZZER={outside}\n", tmp_path) == outside

    def test_generic_external_token_is_not_treated_as_output(self, tmp_path: Path) -> None:
        assert find_build_output_binary("checking /bin/sh\n", tmp_path) is None


class TestFuzzAssembly:
    def test_argv(self, tmp_path: Path) -> None:
        request = assemble_fuzz_request(
            binary=tmp_path / "fuzzer",
            corpus_dir=tmp_path / "corpus",
            crashes_dir=tmp_path / "crashes",
            fuzz_seconds=60,
            timeout_seconds=660,
        )
        assert request.argv[0] == str(tmp_path / "fuzzer")
        assert request.argv[1] == str(tmp_path / "corpus")
        assert "-max_total_time=60" in request.argv
        assert f"-artifact_prefix={tmp_path / 'crashes'}/" in request.argv
        assert "-timeout=5" in request.argv
        assert "-max_len=1048576" in request.argv
        assert "-print_final_stats=1" in request.argv


class TestLibFuzzerParsing:
    def test_metrics_extraction(self) -> None:
        metrics = parse_libfuzzer_metrics(FUZZER_STATS_SAMPLE)
        assert metrics.initial_cov == 4
        assert metrics.final_cov == 14
        assert metrics.cov_delta == 10
        assert metrics.initial_features == 5
        assert metrics.final_features == 21
        assert metrics.feature_delta == 16
        assert metrics.initial_corpus == 1
        assert metrics.final_corpus == 9
        assert metrics.corpus_delta == 8
        assert metrics.execs_per_second == 240

    def test_no_stats_returns_empty(self) -> None:
        metrics = parse_libfuzzer_metrics("no stats here")
        assert metrics.initial_cov is None
        assert metrics.cov_delta == 0


class TestSanitizerDetection:
    def test_address(self) -> None:
        assert detect_sanitizer("ERROR: AddressSanitizer: heap-buffer-overflow") == "address"

    def test_undefined(self) -> None:
        assert detect_sanitizer("runtime error: signed integer overflow") == "undefined"

    def test_timeout(self) -> None:
        assert detect_sanitizer("ERROR: libFuzzer: timeout after 5s") == "timeout"

    def test_none(self) -> None:
        assert detect_sanitizer("all good") is None


class TestDecisions:
    def _execution(
        self,
        *,
        coverage: dict | None = None,
        compile_exit: int = 0,
        crash: bool = False,
    ) -> object:
        fuzz_result = None
        if compile_exit == 0:
            if crash:
                fuzz_result = ProcessResult(
                    argv=["fuzzer"],
                    exit_code=1,
                    timed_out=False,
                    duration_seconds=2.0,
                    stderr="ERROR: AddressSanitizer: heap-buffer-overflow",
                )
            else:
                fuzz_result = ProcessResult(
                    argv=["fuzzer"],
                    exit_code=0,
                    timed_out=False,
                    duration_seconds=2.0,
                    stdout="#2 INITED cov: 1 ft: 1 corp: 1/1b exec/s: 10",
                )
        return make_execution_result(
            run_id="r",
            generation_loop=1,
            compile_result=ProcessResult(
                argv=["clang"],
                exit_code=compile_exit,
                timed_out=False,
                duration_seconds=1.0,
            ),
            fuzz_result=fuzz_result,
            coverage=coverage,
        )

    def test_accepted_when_policy_met(self) -> None:
        execution = self._execution(
            coverage={
                "target_function_hit": True,
                "target_line_delta": 12.5,
                "cov_delta": 3,
                "feature_delta": 2,
                "corpus_delta": 1,
                "execs_per_second": 10,
            },
        )
        decision = decide_generation(execution, CoverageDecisionPolicy())
        assert decision.completes_goal
        assert decision.disposition is ExecutionDisposition.ACCEPTED

    def test_target_not_hit_rejected(self) -> None:
        execution = self._execution(
            coverage={
                "target_function_hit": False,
                "target_line_delta": 12.5,
                "cov_delta": 3,
            },
        )
        decision = decide_generation(execution, CoverageDecisionPolicy())
        assert not decision.completes_goal
        assert "target function was not hit" in decision.reason
        assert decision.feedback is not None

    def test_no_growth_rejected(self) -> None:
        execution = self._execution(
            coverage={"target_function_hit": True, "target_line_delta": 0.0},
        )
        decision = decide_generation(execution, CoverageDecisionPolicy())
        assert decision.disposition is ExecutionDisposition.NEEDS_REGENERATION
        assert "no positive" in decision.reason

    def test_compile_failure_is_regeneration(self) -> None:
        execution = self._execution(compile_exit=1)
        decision = decide_generation(execution, CoverageDecisionPolicy())
        assert decision.disposition is ExecutionDisposition.NEEDS_REGENERATION
        assert decision.feedback is not None
        assert decision.feedback.compile_exit_code == 1

    def test_crash_candidate_never_completes(self) -> None:
        execution = self._execution(crash=True)
        decision = decide_generation(execution, CoverageDecisionPolicy())
        assert decision.disposition is ExecutionDisposition.CRASH_CANDIDATE
        assert not decision.completes_goal


class TestDispositionClassification:
    def test_compile_timeout_is_environment_error(self) -> None:
        result = make_execution_result(
            run_id="r",
            generation_loop=1,
            compile_result=ProcessResult(argv=["clang"], exit_code=None, timed_out=True, duration_seconds=10.0),
            fuzz_result=None,
        )
        assert result.disposition is ExecutionDisposition.ENVIRONMENT_ERROR

    def test_fuzz_crash_is_crash_candidate(self) -> None:
        result = make_execution_result(
            run_id="r",
            generation_loop=1,
            compile_result=ProcessResult(argv=["clang"], exit_code=0, timed_out=False, duration_seconds=1.0),
            fuzz_result=ProcessResult(
                argv=["fuzzer"],
                exit_code=1,
                timed_out=False,
                duration_seconds=3.0,
                stderr="ERROR: AddressSanitizer: heap-buffer-overflow",
            ),
        )
        assert result.disposition is ExecutionDisposition.CRASH_CANDIDATE
        assert result.sanitizer_kind == "address"

    def test_coverage_artifact_missing_is_environment_error(self) -> None:
        result = make_execution_result(
            run_id="r",
            generation_loop=1,
            compile_result=ProcessResult(argv=["clang"], exit_code=0, timed_out=False, duration_seconds=1.0),
            fuzz_result=ProcessResult(argv=["fuzzer"], exit_code=0, timed_out=False, duration_seconds=3.0),
            coverage_valid=False,
        )
        assert result.disposition is ExecutionDisposition.ENVIRONMENT_ERROR

    def test_crash_evidence_beats_missing_coverage(self) -> None:
        # Regression: a crashing fuzzer aborts before flushing .profraw, so
        # coverage is missing — but the sanitizer/crash evidence must win and
        # produce a crash_candidate, not an environment_error.
        result = make_execution_result(
            run_id="r",
            generation_loop=1,
            compile_result=ProcessResult(argv=["clang"], exit_code=0, timed_out=False, duration_seconds=1.0),
            fuzz_result=ProcessResult(
                argv=["fuzzer"],
                exit_code=1,
                timed_out=False,
                duration_seconds=0.1,
                stderr="ERROR: AddressSanitizer: heap-buffer-overflow\nABORTING",
            ),
            coverage_valid=False,
            crash_artifact="crash-abc",
        )
        assert result.disposition is ExecutionDisposition.CRASH_CANDIDATE
        assert result.sanitizer_kind == "address"
        assert result.crash_artifact == "crash-abc"


class TestDefaultDefines:
    def test_profile_defines_prepended_to_argv(self, workspace_root: Path) -> None:
        artifacts = _artifacts(workspace_root)
        source_root = workspace_root / "repos" / "safe"
        candidate = workspace_root / "work" / "safe" / "runs" / "r" / "iterations" / "loop-01" / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "harness_safe.c").write_text("int main(){}", encoding="utf-8")
        profile = ValidationProfile(name="test", default_defines=["HAVE_WRITEV=1"])
        request = assemble_compile_request(artifacts, profile, source_root, candidate)
        assert "-DHAVE_WRITEV=1" in request.argv
        assert request.argv.count("-DHAVE_WRITEV=1") == 1

    def test_profile_defines_load_from_toml(self, tmp_path: Path) -> None:
        from goaloop.config import load_validation_profile

        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "p.toml").write_text('name = "p"\ndefault_defines = ["HAVE_WRITEV=1"]\n', encoding="utf-8")
        profile = load_validation_profile("p", tmp_path)
        assert profile.default_defines == ["HAVE_WRITEV=1"]


class TestDefaultIncludeDirs:
    def test_profile_include_dirs_prepended(self, workspace_root: Path) -> None:
        artifacts = _artifacts(workspace_root)
        source_root = workspace_root / "repos" / "safe"
        candidate = workspace_root / "work" / "safe" / "runs" / "r" / "iterations" / "loop-01" / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "harness_safe.c").write_text("int main(){}", encoding="utf-8")
        profile = ValidationProfile(name="test", default_include_dirs=["/abs/build-config"])
        request = assemble_compile_request(artifacts, profile, source_root, candidate)
        assert "-I/abs/build-config" in request.argv

    def test_include_dirs_resolved_against_workspace(self, tmp_path: Path) -> None:
        from goaloop.config import load_validation_profile

        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "p.toml").write_text(
            'name = "p"\ndefault_include_dirs = ["build-config/c-ares"]\n', encoding="utf-8"
        )
        profile = load_validation_profile("p", tmp_path)
        assert profile.default_include_dirs == [(tmp_path / "build-config" / "c-ares").resolve().as_posix()]
