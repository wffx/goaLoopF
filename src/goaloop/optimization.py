"""Objective signal collection and rendering for model-generated optimization advice."""

from __future__ import annotations

from typing import Any, cast

from .models import (
    HarnessExecutionResult,
    OptimizationAnalysis,
    ResearchMetrics,
)

OPTIMIZATION_ANALYSIS_FILENAME = "optimization-suggestions.json"
OPTIMIZATION_REPORT_FILENAME = "optimization-suggestions.md"


def collect_optimization_signals(
    *,
    metrics: ResearchMetrics,
    execution: HarnessExecutionResult | None,
    trace_summary: dict[str, Any],
) -> dict[str, int | float | str | bool | None]:
    """Collect facts for the model without deriving or seeding recommendations."""

    raw_model_calls = trace_summary.get("model_calls")
    model_calls = cast(dict[str, object], raw_model_calls) if isinstance(raw_model_calls, dict) else {}
    failed_model_calls = _int_value(model_calls.get("failed"))
    average_call_seconds = metrics.model_call_seconds / metrics.model_calls if metrics.model_calls else 0.0
    average_input_tokens = metrics.estimated_input_tokens / metrics.model_calls if metrics.model_calls else 0.0
    tool_results = _int_value(trace_summary.get("tool_results"))
    dominant_phase, dominant_seconds, dominant_share = _dominant_phase(metrics.phase_durations)
    return {
        "generation_loops_used": metrics.generation_loops_used,
        "format_retries": metrics.format_retries,
        "first_compile_success": metrics.first_compile_success,
        "model_calls": metrics.model_calls,
        "model_call_failures": failed_model_calls,
        "model_call_seconds": metrics.model_call_seconds,
        "average_model_call_seconds": round(average_call_seconds, 6),
        "estimated_input_tokens": metrics.estimated_input_tokens,
        "average_input_tokens": round(average_input_tokens, 2),
        "tool_calls": metrics.tool_calls,
        "tool_results": tool_results,
        "krepo_queries": _tool_call_count(trace_summary, "query_krepo_symbol"),
        "dominant_phase": dominant_phase,
        "dominant_phase_seconds": round(dominant_seconds, 6),
        "dominant_phase_share": round(dominant_share, 6),
        "target_function_hit": execution.coverage.target_function_hit if execution is not None else None,
    }


def render_optimization_markdown(analysis: OptimizationAnalysis) -> str:
    lines = [
        f"# GoaLoop Optimization Suggestions — {analysis.run_id}",
        "",
        f"- **status**: `{analysis.final_status.value}`",
        f"- **generated**: {analysis.generated_at.isoformat()}",
        f"- **source metrics**: `{analysis.source_metrics_path}`",
        f"- **trace summary**: `{analysis.trace_summary_path or 'unavailable'}`",
        f"- **generator**: `{analysis.generator}`",
        f"- **generation status**: `{analysis.generation_status}`",
        f"- **failure reason**: {analysis.failure_reason or '—'}",
        f"- **summary**: {analysis.summary}",
        "",
        "## Signals",
        "",
    ]
    lines.extend(f"- **{key}**: `{value}`" for key, value in sorted(analysis.signals.items()))
    lines.extend(["", "## Suggestions", ""])
    for index, suggestion in enumerate(analysis.suggestions, start=1):
        lines.extend(
            [
                f"### {index}. [{suggestion.priority.value.upper()}] {suggestion.title}",
                "",
                f"- **category**: `{suggestion.category.value}`",
                f"- **evidence**: {'；'.join(suggestion.evidence)}",
                f"- **recommendation**: {suggestion.recommendation}",
                f"- **expected impact**: {suggestion.expected_impact}",
                "",
            ]
        )
    return "\n".join(lines)


def _tool_call_count(trace_summary: dict[str, Any], tool_name: str) -> int:
    tool_names = trace_summary.get("tool_call_names")
    if not isinstance(tool_names, dict):
        return 0
    return _int_value(tool_names.get(tool_name))


def _int_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _dominant_phase(phase_durations: dict[str, float]) -> tuple[str | None, float, float]:
    positive = {name: duration for name, duration in phase_durations.items() if duration > 0}
    if not positive:
        return None, 0.0, 0.0
    phase, duration = max(positive.items(), key=lambda item: item[1])
    total = sum(positive.values())
    return phase, duration, duration / total if total else 0.0
