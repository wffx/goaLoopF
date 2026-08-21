"""Report generation: ValidationResult, Markdown report, research metrics export."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .models import (
    CrashAnalysisResult,
    HarnessExecutionResult,
    PreprocessResult,
    ResearchMetrics,
    RunState,
    TerminalStatus,
    ValidationResult,
)

REPORT_FILENAME = "report.md"
METRICS_FILENAME = "research-metrics.json"
VALIDATION_FILENAME = "validation.json"


def build_validation_result(
    *,
    state: RunState,
    reason: str,
    execution: HarnessExecutionResult | None = None,
    crash_analysis: CrashAnalysisResult | None = None,
    report_path: str | None = None,
) -> ValidationResult:
    status = state.terminal_status or TerminalStatus.FAILED
    return ValidationResult(
        run_id=state.run_id,
        status=status,
        generation_loops_used=state.generation_loop,
        execution=execution,
        crash_analysis=crash_analysis,
        report_path=report_path,
        reason=reason,
    )


def write_markdown_report(
    *,
    run_dir: Path,
    state: RunState,
    preprocess: PreprocessResult,
    execution: HarnessExecutionResult | None,
    crash_analysis: CrashAnalysisResult | None,
    reason: str,
) -> Path:
    lines: list[str] = [
        f"# Fuzz Harness Validation Report — {state.run_id}",
        "",
        f"- **status**: `{state.terminal_status or 'in_progress'}`",
        f"- **project**: {state.project_name}",
        f"- **source**: `{preprocess.source_root}`",
        f"- **target function**: `{state.request.function}`",
        f"- **language**: {preprocess.language.value}",
        f"- **profile**: {state.request.profile} / model profile: {state.request.model_profile}",
        f"- **generation loops used**: {state.generation_loop} / {state.request.max_generation_loops}",
        f"- **fuzz budget**: {state.request.fuzz_seconds}s per candidate",
        f"- **created**: {state.created_at.isoformat()}",
        "",
        "## Preprocess",
        "",
        f"- **ready**: {preprocess.ready}",
        f"- **reason**: {preprocess.reason or '(none)'}",
        f"- **candidate signatures** ({len(preprocess.candidate_signatures)}):",
        "",
    ]
    lines += [f"  - `{item}`" for item in preprocess.candidate_signatures[:10]]
    lines += [
        "",
        "| capability | available | detail |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {item.name} | {item.available} | {item.detail} |" for item in preprocess.capability_report.capabilities
    ]
    lines += [
        "",
        "## Execution loops",
        "",
        "| loop | compile | disposition | cov Δ | ft Δ | corpus Δ | exec/s | target hit | target cov % | reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.append(
        "| _no candidate executed_ | — | — | — | — | — | — | — | — | — |"
        if execution is None
        else _execution_row(execution)
    )
    if execution is not None and execution.fuzz_result is not None:
        lines += [
            "",
            "### Latest execution evidence",
            "",
            f"- **compile exit code**: {execution.compile_result.exit_code} "
            f"({execution.compile_result.duration_seconds}s)",
            f"- **fuzz exit code**: {execution.fuzz_result.exit_code} "
            f"({execution.fuzz_result.duration_seconds}s, timed out: {execution.fuzz_result.timed_out})",
            f"- **sanitizer**: {execution.sanitizer_kind or 'none'}",
            f"- **crash artifact**: {execution.crash_artifact or 'not_produced'}",
        ]
    lines += [
        "",
        "## Crash analysis",
        "",
        (
            "- _no crash analysis was performed_"
            if crash_analysis is None
            else (
                f"- **ownership**: `{crash_analysis.ownership.value}`\n"
                f"- **sanitizer**: {crash_analysis.sanitizer_kind or 'none'}\n"
                f"- **reproductions**: {crash_analysis.reproductions}/{crash_analysis.required_reproductions}\n"
                f"- **minimized artifact**: {crash_analysis.minimized_artifact or 'not_produced'}\n"
                f"- **reason**: {crash_analysis.reason}"
            )
        ),
        "",
        "## Conclusion",
        "",
        f"`{state.terminal_status or 'in_progress'}` — {reason}",
        "",
        "> `harness_verified` only means the harness compiled, ran and satisfied the configured "
        "coverage policy within a bounded fuzz budget. It does not prove the product is bug-free.",
        "",
    ]
    report_path = run_dir / REPORT_FILENAME
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def build_research_metrics(
    *,
    state: RunState,
    preprocess: PreprocessResult,
    model_profile_name: str,
    provider: str,
    model: str,
    prompt_version: str,
    endpoint_label: str,
    phase_durations: dict[str, float],
    format_retries: int,
    first_compile_success: bool | None,
    time_to_bug_seconds: float | None,
    token_source: Literal["sdk", "unavailable"] = "unavailable",
    tokens_used: int | None = None,
) -> ResearchMetrics:
    return ResearchMetrics(
        run_id=state.run_id,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        endpoint_label=endpoint_label,
        started_at=state.created_at,
        finished_at=datetime.now(UTC),
        phase_durations=phase_durations,
        generation_loops_used=state.generation_loop,
        format_retries=format_retries,
        first_compile_success=first_compile_success,
        final_status=state.terminal_status or TerminalStatus.FAILED,
        token_source=token_source,
        tokens_used=tokens_used,
        time_to_bug_seconds=time_to_bug_seconds,
        loop_hashes=_loop_hashes(state, preprocess),
    )


def _execution_row(execution: HarnessExecutionResult) -> str:
    cov = execution.coverage
    return (
        f"| {execution.generation_loop} | "
        f"{execution.compile_result.exit_code if execution.compile_result.exit_code is not None else 'timeout'} | "
        f"{execution.disposition.value} | {cov.cov_delta} | {cov.feature_delta} | {cov.corpus_delta} | "
        f"{cov.execs_per_second if cov.execs_per_second is not None else '—'} | "
        f"{cov.target_function_hit} | {cov.target_line_coverage if cov.target_line_coverage is not None else '—'} | "
        f"{execution.reason} |"
    )


def _loop_hashes(state: RunState, preprocess: PreprocessResult) -> dict[str, dict[str, str]]:
    # Loop hashes are recorded by the controller from materialized candidates;
    # this placeholder keeps the export shape stable when none are recorded.
    return {}
