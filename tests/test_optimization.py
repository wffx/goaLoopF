"""Optimization signal collection and report rendering tests."""

from __future__ import annotations

from datetime import UTC, datetime

from goaloop.models import (
    OptimizationAnalysis,
    OptimizationSuggestion,
    ResearchMetrics,
    TerminalStatus,
)
from goaloop.optimization import collect_optimization_signals, render_optimization_markdown


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


def test_signal_collection_does_not_generate_suggestions() -> None:
    signals = collect_optimization_signals(
        metrics=_metrics(TerminalStatus.HARNESS_VERIFIED),
        execution=None,
        trace_summary={},
    )

    assert signals["generation_loops_used"] == 1
    assert signals["model_call_failures"] == 0


def test_signal_collection_records_metrics_without_status_rules() -> None:
    signals = collect_optimization_signals(
        metrics=_metrics(
            TerminalStatus.FAILED,
            loops=3,
            format_retries=1,
            first_compile_success=False,
            model_calls=4,
            model_call_seconds=280.0,
            estimated_input_tokens=160_000,
        ),
        execution=None,
        trace_summary={"methods": {}, "model_calls": {"completed": 3, "failed": 1}},
    )

    assert signals["model_call_failures"] == 1
    assert signals["average_model_call_seconds"] == 70.0
    assert signals["average_input_tokens"] == 40_000.0


def test_markdown_includes_actionable_content() -> None:
    analysis = OptimizationAnalysis(
        run_id="run-opt",
        final_status=TerminalStatus.BLOCKED,
        summary="模型根据运行证据生成建议。",
        suggestions=[
            OptimizationSuggestion(
                id="inspect-krepo-setup",
                priority="high",
                category="environment",
                title="检查 kRepo 环境",
                evidence=["kRepo database is missing"],
                recommendation="检查数据库配置后重新运行。",
                expected_impact="恢复分析流程。",
            )
        ],
    )

    markdown = render_optimization_markdown(analysis)
    assert "检查 kRepo 环境" in markdown
    assert "检查数据库配置后重新运行" in markdown


def test_dominant_phase_is_reported_from_duration_metrics() -> None:
    metrics = _metrics(TerminalStatus.HARNESS_VERIFIED).model_copy(
        update={"phase_durations": {"preprocess": 2.0, "harness_execution": 98.0}}
    )
    signals = collect_optimization_signals(
        metrics=metrics,
        execution=None,
        trace_summary={},
    )

    assert signals["dominant_phase"] == "harness_execution"
    assert signals["dominant_phase_share"] == 0.98
