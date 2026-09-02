"""Deterministic post-run optimization analysis over persisted run metrics."""

from __future__ import annotations

from typing import Any, cast

from .models import (
    HarnessExecutionResult,
    OptimizationAnalysis,
    OptimizationCategory,
    OptimizationPriority,
    OptimizationSuggestion,
    ResearchMetrics,
    RunState,
    TerminalStatus,
)

OPTIMIZATION_ANALYSIS_FILENAME = "optimization-suggestions.json"
OPTIMIZATION_REPORT_FILENAME = "optimization-suggestions.md"
MAX_SUGGESTIONS = 3
HIGH_INPUT_TOKENS_PER_CALL = 50_000
HIGH_TOTAL_INPUT_TOKENS = 100_000
SLOW_MODEL_CALL_SECONDS = 60.0
DOMINANT_PHASE_MIN_SECONDS = 30.0
DOMINANT_PHASE_SHARE = 0.7

_PRIORITY_ORDER = {
    OptimizationPriority.HIGH: 0,
    OptimizationPriority.MEDIUM: 1,
    OptimizationPriority.LOW: 2,
}


def analyze_run_optimization(
    *,
    state: RunState,
    metrics: ResearchMetrics,
    reason: str,
    execution: HarnessExecutionResult | None,
    trace_summary: dict[str, Any],
) -> OptimizationAnalysis:
    """Produce bounded, evidence-backed recommendations without another model call."""

    suggestions: list[OptimizationSuggestion] = []
    seen: set[str] = set()

    def add(
        suggestion_id: str,
        priority: OptimizationPriority,
        category: OptimizationCategory,
        title: str,
        evidence: list[str],
        recommendation: str,
        expected_impact: str,
    ) -> None:
        if suggestion_id in seen:
            return
        seen.add(suggestion_id)
        suggestions.append(
            OptimizationSuggestion(
                id=suggestion_id,
                priority=priority,
                category=category,
                title=title,
                evidence=evidence,
                recommendation=recommendation,
                expected_impact=expected_impact,
            )
        )

    status = metrics.final_status
    full_reason = reason.strip() or status.value
    reason_text = _truncate(full_reason)
    if status is TerminalStatus.NEEDS_INPUT:
        add(
            "fix-input-scope",
            OptimizationPriority.HIGH,
            OptimizationCategory.INPUT,
            "修正任务输入与目标符号范围",
            [f"任务终态为 needs_input：{reason_text}"],
            "核对 --repo、--source 和 --function；同名函数场景应让 --source 指向实现文件或最小目录后重试。",
            "避免在预处理阶段终止，并减少错误候选签名带来的上下文噪声。",
        )
    elif status is TerminalStatus.BLOCKED:
        add(
            "restore-runtime-prerequisites",
            OptimizationPriority.HIGH,
            OptimizationCategory.ENVIRONMENT,
            "恢复运行环境或外部依赖",
            [f"任务终态为 blocked：{reason_text}"],
            (
                "先运行 goaloop doctor，并修复报告中的模型凭据、kRepo、BROWSE.VC.DB、"
                "编译工具链或沙箱能力问题，再 resume 当前 run。"
            ),
            "复用当前检查点继续执行，避免重复生成和重复消耗模型调用。",
        )
    elif status is TerminalStatus.NEEDS_REVIEW:
        add(
            "triage-uncertain-crash",
            OptimizationPriority.HIGH,
            OptimizationCategory.TRIAGE,
            "人工复核无法归属的崩溃",
            [f"任务终态为 needs_review：{reason_text}"],
            "检查 crash-analysis.json、最小化输入和符号化栈；补齐调试符号或源码映射后重新复现。",
            "提高产品缺陷与 harness 自身缺陷的归属准确率。",
        )
    elif status is TerminalStatus.BUG_REPRODUCED:
        add(
            "preserve-reproduced-bug",
            OptimizationPriority.HIGH,
            OptimizationCategory.TRIAGE,
            "固化并扩展已复现缺陷证据",
            [f"任务终态为 bug_reproduced：{reason_text}"],
            "保存最小化 crash 输入、构建参数和 sanitizer 栈，并用相同 profile 重复验证后进入缺陷修复流程。",
            "形成可重复、可回归的产品缺陷证据。",
        )

    invalid_output = "model output" in full_reason.lower() or "format" in full_reason.lower()
    if metrics.format_retries > 0 or invalid_output:
        priority = OptimizationPriority.HIGH if status is TerminalStatus.FAILED else OptimizationPriority.MEDIUM
        add(
            "stabilize-model-output",
            priority,
            OptimizationCategory.MODEL_OUTPUT,
            "提高模型结构化输出稳定性",
            [
                f"格式重试次数为 {metrics.format_retries}",
                f"终态原因：{reason_text}",
            ],
            (
                "检查原始 DSH trace 中最后一次完整回复；收紧 GeneratedArtifactSet 示例与约束，"
                "并优先用原生 Tool Schema 替代需要模型记忆的文本协议。"
            ),
            "降低空响应、非 JSON 响应和格式重试造成的失败与额外 token 消耗。",
        )

    if metrics.first_compile_success is False:
        krepo_queries = _tool_call_count(trace_summary, "query_krepo_symbol")
        recommendation = (
            "将首轮编译错误中稳定的 include、宏和链接参数固化到 Validation Profile；"
            "缺少类型或宏定义时在 generation 早期按需查询 kRepo。"
        )
        if krepo_queries == 0:
            recommendation += " 本次没有记录到 kRepo dependency 查询，应检查模型是否获得了足够明确的查询时机。"
        add(
            "improve-first-pass-build-context",
            OptimizationPriority.HIGH,
            OptimizationCategory.BUILD,
            "提升首轮候选的编译成功率",
            [
                "首轮候选未编译成功",
                f"generation loops 使用 {metrics.generation_loops_used} 轮",
                f"kRepo dependency 查询 {krepo_queries} 次",
            ],
            recommendation,
            "减少因猜测构建依赖产生的 regeneration loop 和模型调用。",
        )

    if metrics.generation_loops_used > 1:
        add(
            "reduce-generation-rework",
            OptimizationPriority.MEDIUM,
            OptimizationCategory.GENERATION,
            "减少候选重生成轮次",
            [
                f"generation loops 使用 {metrics.generation_loops_used}/{state.request.max_generation_loops} 轮",
                f"首轮编译成功：{metrics.first_compile_success}",
            ],
            (
                "对比各 loop 的 execution.json 与反馈，找出重复失败原因；"
                "把稳定的构建知识写入 profile，把必要 dependency 改为更早的按需查询。"
            ),
            "缩短完成时间，并降低重复 prompt 与候选编译成本。",
        )

    raw_model_calls = trace_summary.get("model_calls")
    model_calls = cast(dict[str, object], raw_model_calls) if isinstance(raw_model_calls, dict) else {}
    failed_model_calls = _int_value(model_calls.get("failed"))
    completed_model_calls = _int_value(model_calls.get("completed"))
    if failed_model_calls > 0:
        add(
            "stabilize-model-provider",
            OptimizationPriority.HIGH,
            OptimizationCategory.ENVIRONMENT,
            "降低模型调用失败率",
            [f"模型调用成功 {completed_model_calls} 次、失败 {failed_model_calls} 次"],
            (
                "按 call_id 对齐 dsh-trace.jsonl 中的 failed 事件，检查超时、限流、端点和上下文窗口；"
                "修复后 resume 当前 run。"
            ),
            "减少被阻断的任务和无法复用的模型等待时间。",
        )

    average_call_seconds = metrics.model_call_seconds / metrics.model_calls if metrics.model_calls else 0.0
    if average_call_seconds >= SLOW_MODEL_CALL_SECONDS:
        add(
            "reduce-model-latency",
            OptimizationPriority.MEDIUM,
            OptimizationCategory.PERFORMANCE,
            "降低单次模型调用延迟",
            [
                f"模型调用 {metrics.model_calls} 次，累计 {metrics.model_call_seconds:.2f}s",
                f"平均每次 {average_call_seconds:.2f}s",
            ],
            "先减少重复基础上下文和无效格式重试，再对相同 suite 比较模型 profile、max-context-kb 与端点延迟。",
            "缩短 generation phase 的等待时间并提高批量 evaluate 吞吐。",
        )

    average_input_tokens = metrics.estimated_input_tokens / metrics.model_calls if metrics.model_calls else 0.0
    if (
        average_input_tokens >= HIGH_INPUT_TOKENS_PER_CALL
        or metrics.estimated_input_tokens >= HIGH_TOTAL_INPUT_TOKENS
    ):
        add(
            "reduce-input-context",
            OptimizationPriority.MEDIUM,
            OptimizationCategory.CONTEXT,
            "压缩重复输入上下文",
            [
                f"估算输入 token 总量 {metrics.estimated_input_tokens}",
                f"平均每次模型调用 {average_input_tokens:.0f}",
            ],
            (
                "降低 --max-context-kb，保留目标函数、调用树和参数约束；"
                "dependency 继续通过 kRepo 按需获取，并用相同任务做 A/B 验证。"
            ),
            "降低 token 消耗，并减少大 prompt 导致的延迟和输出不稳定。",
        )

    tool_results = _int_value(trace_summary.get("tool_results"))
    if metrics.tool_calls > tool_results:
        add(
            "repair-tool-lifecycle",
            OptimizationPriority.HIGH,
            OptimizationCategory.TOOLING,
            "检查未闭合的工具调用",
            [f"tool/call 为 {metrics.tool_calls} 次，tool/result 为 {tool_results} 次"],
            (
                "按 session、turn 和 step 检查原始 trace 中缺少结果的调用，"
                "确认 Tool executor、超时和异常返回都产生标准 tool/result。"
            ),
            "避免 Agent 在等待工具结果时丢失上下文或产生错误决策。",
        )

    dominant_phase, dominant_seconds, dominant_share = _dominant_phase(metrics.phase_durations)
    if (
        dominant_phase is not None
        and dominant_seconds >= DOMINANT_PHASE_MIN_SECONDS
        and dominant_share >= DOMINANT_PHASE_SHARE
    ):
        phase_recommendations = {
            "preprocess": "缩小 --source 范围，并检查 kRepo 数据库与 report 查询耗时。",
            "harness_generation": "减少重复上下文、格式重试和 regeneration loop，并对比更低延迟的模型 profile。",
            "harness_execution": "调优期间先降低 fuzz_seconds 做快速 A/B，最终验证再恢复目标 fuzz 预算。",
            "crash_analysis_report": "检查符号化、最小化和重复复现耗时，优先复用已有 crash 与构建产物。",
        }
        add(
            "focus-dominant-phase",
            OptimizationPriority.LOW,
            OptimizationCategory.PERFORMANCE,
            "优先优化耗时占比最高的阶段",
            [
                f"{dominant_phase} 耗时 {dominant_seconds:.2f}s",
                f"占已记录阶段总耗时 {dominant_share:.1%}",
            ],
            phase_recommendations.get(dominant_phase, "对该阶段做细分计时，并优先优化占比最高的步骤。"),
            "先处理主导总时长的阶段，获得更明显的端到端吞吐改善。",
        )

    if status is TerminalStatus.FAILED and not suggestions:
        add(
            "inspect-terminal-failure",
            OptimizationPriority.HIGH,
            OptimizationCategory.VALIDATION,
            "定位未分类的终态失败",
            [f"任务终态为 failed：{reason_text}"],
            (
                "从 events.jsonl 的 run:terminal 事件回溯到对应 phase，"
                "再结合 execution.json 和 DSH trace 修复最早出现的失败信号。"
            ),
            "把无法行动的笼统失败转化为可复现、可验证的工程问题。",
        )

    if not suggestions:
        add(
            "validate-success-baseline",
            OptimizationPriority.LOW,
            OptimizationCategory.VALIDATION,
            "建立成功任务的稳定基线",
            [
                f"任务终态为 {status.value}",
                f"generation loops 使用 {metrics.generation_loops_used} 轮",
                f"格式重试 {metrics.format_retries} 次",
            ],
            "使用相同 suite 和 repetitions 重复运行，比较成功率、平均模型耗时、估算输入 token 与工具调用数量。",
            "确认当前成功不是偶然结果，并为后续 prompt、Tool 或模型版本提供可比较基线。",
        )

    suggestions.sort(key=lambda item: (_PRIORITY_ORDER[item.priority], item.id))
    suggestions = suggestions[:MAX_SUGGESTIONS]
    highest = suggestions[0].priority.value
    summary = f"基于任务终态、研究指标和 DSH trace 摘要生成 {len(suggestions)} 条建议，最高优先级为 {highest}。"
    return OptimizationAnalysis(
        run_id=state.run_id,
        final_status=status,
        trace_summary_path=metrics.dsh_trace_summary_path,
        summary=summary,
        signals={
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
        },
        suggestions=suggestions,
    )


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


def _truncate(value: str, limit: int = 900) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
