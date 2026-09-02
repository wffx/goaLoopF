# DSH Trace 与可观测性

## 持久化策略

进入 harness generation 后，goaloop 无论是否启用 `--debug`，都会订阅 DSH SDK
通知并写入当前 run：

- `logs/dsh-trace.jsonl`：按接收顺序追加的原始 trace；
- `logs/dsh-trace-summary.json`：可跨 `resume` 重建的结构化摘要；
- `research-metrics.json`：复制关键汇总指标，供 `evaluate` 和跨 run 对比。

原始 trace **不做脱敏**。SDK notification 的 method 和 payload 会完整保存，可能包含
提示词、模型回复、源码、绝对路径、凭据或端点错误详情。run 目录必须按敏感数据管理，
不要直接提交、分享或上传。CLI `--debug` 的 Terminal 输出仍执行脱敏，并过滤 title、
request header/context、完整 prompt 等噪声；reasoning/text 流式 chunk 每累计约 2 KiB
才报告一次进度，committed message、工具调用和最终工件以摘要形式显示。

## 自定义遥测事件

除 DSH 原生通知外，Driver 记录以下 goaloop 事件：

- `goaloop.model_call.started`：session、prompt 字符数和估算输入 token；
- `goaloop.model_call.completed`：耗时、finish reason、响应字符数、事件数量；
- `goaloop.model_call.failed`：耗时、异常类型和原始错误；

generation 的 kRepo 原生查询不再生成自定义事件，而是使用 DSH 标准
`tool/call(name=query_krepo_symbol)` 和 `tool/result`。Tool result 首行包含实际执行命令
或 `cache hit`；完整命令、argv、cwd 和结果继续写入 `krepo-queries/queries.jsonl`。

## 摘要字段

`dsh-trace-summary.json` 汇总：

- trace 总事件数、无效记录数、首末时间；
- 各 notification method 和 `session.event` 类型计数；
- 模型调用次数、成功/失败、累计耗时；
- prompt 字符数、估算输入 token、响应字符数、finish reason 分布；
- 标准 `tool/call` 和 `tool/result` 数量，以及按 Tool 名称统计的 `tool_call_names`。

`goaloop evaluate` 会把每个 run 的关键字段写入 `results`，并在 `observability` 中按
目标函数汇总总量和平均模型耗时/估算输入 token，可直接用于版本间 A/B 对比。

`resume` 时 recorder 从现有 JSONL 重新计算摘要，再继续递增 sequence，避免仅依赖可能
中断写入的摘要文件。

## 自动优化分析

每个任务进入终态后，goaloop 先用确定性规则整理基础信号，再通过 DSH Python SDK 的独立
`<run-id>-optimization` session 读取有界的原始 session trace、workflow events、历轮
execution、kRepo 查询审计和指标，生成：

- `optimization-suggestions.json`：结构化信号、生成方式和最多 3 条有序建议；
- `optimization-suggestions.md`：包含证据、建议和预期收益的完整报告；
- `report.md`：保持任务验证报告，不混入工程优化建议。

模型只能根据本次运行证据提出建议，不能修改代码或自动实施；输出使用严格 JSON，并允许
一次格式修复。模型不可用或持续输出无效时，不生成任何替代建议，`generation_status`
标记为 `failed`、`failure_reason` 记录原因，报告阶段继续完成。该分析调用及原始
notification 继续追加到 DSH trace。
Terminal 默认显示建议摘要；`evaluate-results.json` 同时保存每个 run 的建议并按目标函数汇总高频项。

## 优化使用方式

自动建议用于快速定位，仍建议以 run/evaluate 为单位做离线验证，不让 generation Agent
直接修改工程：

1. 按模型、prompt version、目标函数和终态分组；
2. 对比模型耗时、估算输入规模、格式重试、工具调用和 generation loop 数；
3. 将编译/覆盖反馈与对应 session trace 对齐，定位错误决策发生在哪个 step；
4. 对 prompt、Tool Schema 或模型 Profile 做版本化修改；
5. 使用相同 suite 和 repetitions 做 A/B 验证后再更新 default。

当前 default Cordis 仍不开放 Bash、编辑器、网络、jobs 或 subagent。可观测性通过
只记录事件实现，不扩大 generation Agent 的权限边界。
