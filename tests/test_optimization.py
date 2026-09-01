"""Deterministic post-run optimization analysis tests."""

from __future__ import annotations

from datetime import UTC, datetime

from goaloop.models import (
    FuzzRunRequest,
    GenerationGoal,
    OptimizationPriority,
    Phase,
    ResearchMetrics,
    RunState,
    TerminalStatus,
)
from goaloop.optimization import analyze_run_optimization, render_optimization_markdown


def _state(status: TerminalStatus, *, loops: int = 1, max_loops: int = 3) -> RunState:
    return RunState(
        run_id="run-opt",
        project_name="safe",
        request=FuzzRunRequest(
            repo="repos/safe",
            source=".",
            function="safe_parse",
            max_generation_loops=max_loops,
        ),
        phase=Phase.CRASH_ANALYSIS_REPORT,
        generation_loop=loops,
        terminal_status=status,
        goal=GenerationGoal(
            run_id="run-opt",
            objective="generate a harness",
            target_function="safe_parse",
            acceptance_criteria=["compiles"],
            max_generation_loops=max_loops,
            current_loop=loops,
        ),
    )


def _metrics(
    status: TerminalStatus,
    *,
    loops: int = 1,
    format_retries: int = 0,
    first_compile_success: bool | None = True,
    model_calls: int = 1,
    model_call_seconds: float = 2.0,
    estimated_input_tokens: int = 10_000,
    tool_calls: int = 0,
) -> ResearchMetrics:
    now = datetime.now(UTC)
    return ResearchMetrics(
        run_id="run-opt",
        provider="test",
        model="test-model",
        prompt_version="v1",
        endpoint_label="test",
        started_at=now,
        finished_at=now,
        generation_loops_used=loops,
        format_retries=format_retries,
        first_compile_success=first_compile_success,
        final_status=status,
        model_calls=model_calls,
        model_call_seconds=model_call_seconds,
        estimated_input_tokens=estimated_input_tokens,
        tool_calls=tool_calls,
        dsh_trace_summary_path="logs/dsh-trace-summary.json",
    )


def test_successful_run_gets_baseline_recommendation() -> None:
    analysis = analyze_run_optimization(
        state=_state(TerminalStatus.HARNESS_VERIFIED),
        metrics=_metrics(TerminalStatus.HARNESS_VERIFIED),
        reason="candidate satisfied coverage policy",
        execution=None,
        trace_summary={},
    )

    assert [item.id for item in analysis.suggestions] == ["validate-success-baseline"]
    assert analysis.suggestions[0].priority is OptimizationPriority.LOW
    assert analysis.trace_summary_path == "logs/dsh-trace-summary.json"


def test_failed_generation_prioritizes_output_build_and_rework() -> None:
    analysis = analyze_run_optimization(
        state=_state(TerminalStatus.FAILED, loops=3, max_loops=3),
        metrics=_metrics(
            TerminalStatus.FAILED,
            loops=3,
            format_retries=1,
            first_compile_success=False,
            model_calls=4,
            model_call_seconds=280.0,
            estimated_input_tokens=160_000,
        ),
        reason="model output stayed invalid after the format retry",
        execution=None,
        trace_summary={"methods": {}, "model_calls": {"completed": 3, "failed": 1}},
    )

    ids = [item.id for item in analysis.suggestions]
    assert ids[:3] == [
        "improve-first-pass-build-context",
        "stabilize-model-output",
        "stabilize-model-provider",
    ]
    assert "reduce-generation-rework" in ids
    assert len(ids) <= 6
    assert analysis.signals["model_call_failures"] == 1


def test_markdown_includes_actionable_content() -> None:
    analysis = analyze_run_optimization(
        state=_state(TerminalStatus.BLOCKED, loops=0),
        metrics=_metrics(
            TerminalStatus.BLOCKED,
            loops=0,
            first_compile_success=None,
            model_calls=0,
            estimated_input_tokens=0,
        ),
        reason="kRepo database is missing",
        execution=None,
        trace_summary={},
    )

    markdown = render_optimization_markdown(analysis)
    assert "恢复运行环境或外部依赖" in markdown
    assert "goaloop doctor" in markdown


def test_dominant_phase_is_reported_from_duration_metrics() -> None:
    metrics = _metrics(TerminalStatus.HARNESS_VERIFIED).model_copy(
        update={"phase_durations": {"preprocess": 2.0, "harness_execution": 98.0}}
    )
    analysis = analyze_run_optimization(
        state=_state(TerminalStatus.HARNESS_VERIFIED),
        metrics=metrics,
        reason="candidate satisfied coverage policy",
        execution=None,
        trace_summary={},
    )

    assert analysis.suggestions[0].id == "focus-dominant-phase"
    assert analysis.signals["dominant_phase"] == "harness_execution"
    assert analysis.signals["dominant_phase_share"] == 0.98


def test_long_terminal_reason_is_bounded_in_evidence() -> None:
    analysis = analyze_run_optimization(
        state=_state(TerminalStatus.BLOCKED, loops=0),
        metrics=_metrics(
            TerminalStatus.BLOCKED,
            loops=0,
            first_compile_success=None,
            model_calls=0,
            estimated_input_tokens=0,
        ),
        reason="endpoint failure: " + "x" * 5000,
        execution=None,
        trace_summary={},
    )

    assert len(analysis.suggestions[0].evidence[0]) <= 1000
    assert analysis.suggestions[0].evidence[0].endswith("…")
