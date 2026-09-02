# 数据契约

所有 Pydantic 模型定义在 `src/goaloop/models.py`，全局 `extra="forbid"`（拒绝未知字段），`validate_assignment=True`（赋值即校验）。核心模型按四阶段分布：

## 输入

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `FuzzRunRequest` | CLI 参数 | `repo`（代码仓根目录）、`source`（仓内目标目录或文件）、`function`（`[\\w:][\\w:$~.<>-]*`）、`max_generation_loops: 1–20`、`fuzz_seconds: 1–86400` |
| `ValidationProfile` | 工具链/沙箱/资源/覆盖策略 | `allowed_compiler_flags` 白名单（前缀匹配）、`allowed_libraries` 白名单 |
| `ModelProfile` | 模型 Provider/Model/Cordis 路径 | `cordis` 相对路径会解析为 workspace 绝对路径 |

## 预处理

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `PreprocessResult` | 预处理输出 | `source_root` 为仓库边界，`source_scope` 为符号搜索范围；`candidate_signatures` 是范围内启发式提取的声明/定义（头文件声明、实现或重载可产生多个成员，最多 10 个）；`ready` 与 `terminal_status` 互斥 |
| `SourceContext` | 基础函数上下文 + SHA-256 | `kind` 仅允许 `target_function`/`incoming_tree`/`outgoing_tree`/`param_constraints`；目标片段带 `start_line`/`end_line`；`path` 必须相对、无 `..`、无 `\\`；`truncated` 标记预算截断 |
| `CapabilityReport` / `Capability` | 工具链能力探测 | `ready` 属性 = 全部 `available` |

## 生成阶段

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `GenerationGoal` | 控制器侧目标追踪 | `current_loop` ≤ `max_generation_loops`；`completed` 由控制器设置 |
| `EndpointPlan` | 函数签名、生命周期、构建依赖 | `location` 必须相对路径；`language` 限 `c`/`cpp` |
| `BuildPlan` | 编译计划 | 标准模式校验相对路径、flags 和 libraries；build-dir 模式要求 `harness_file=harness.c` 且所有构建数组为空 |
| `GeneratedFile` | 单个生成文件 | `path` 必须相对；`content` ≤ 1,000,000 字符 |
| `GeneratedArtifactSet` | 模型响应（每轮） | `files` 4–64 个且路径唯一；`candidate_ready` 必须 `True`；必须携带 `schema_version`/`run_id`/`phase`/`generation_loop` |
| `GenerationFeedback` | 反馈给模型的执行证据 | `category`、`summary`、`log_excerpt`（已脱敏）、`artifact_hashes` |

generation 可在最终 `GeneratedArtifactSet` 前调用 DSH 原生 `query_krepo_symbol` Tool
查询非函数符号。Tool Schema 要求 `symbol`、`repo`、`function`、`file`，`kind` 可选。
后三项必须使用 prompt 提供的值，并与 Controller 写入当前 session 的绑定交叉校验。
Python 窄桥接复用 `KRepoQueryService` 持久化缓存和审计；每个 session 不限制查询次数。

## 执行阶段

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `ProcessRequest` | 后端执行请求 | `argv` 长度 1–512；`stdout_path` 可选（大输出直写文件） |
| `ProcessResult` | 后端执行结果 | `exit_code` 可为 `None`（OSError）；`timed_out`/`output_truncated` 标记 |
| `CoverageMetrics` | 覆盖指标 | `initial_cov`/`final_cov` 来自 libFuzzer 统计行；`target_function_hit`/`target_line_coverage` 来自 llvm-cov 归因 |
| `HarnessExecutionResult` | 执行 + 分流 | `disposition` 四种（`accepted`/`needs_regeneration`/`crash_candidate`/`environment_error`） |
| `GenerationDecision` | 控制器决策 | `completes_goal` 仅当 `ACCEPTED`；`NEEDS_REGENERATION` 必须带 `feedback` |

## Crash 分析

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `CrashAnalysisResult` | 分析结论 | `ownership`（`product`/`harness`/`unknown`）；`reproductions` 0–3；`required_reproductions` 固定 3 |
| `CrashOwnership` | 归属枚举 | `product`（有产品源码帧）、`harness`（仅 harness 帧）、`unknown`（无归属） |

## 报告

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `ValidationResult` | 验证结论 | 携带 `status`、`execution`、`crash_analysis`、`report_path` |
| `ResearchMetrics` | 研究指标导出 | `token_source` 限 `sdk`/`unavailable`；包含阶段耗时、generation loops、格式重试，以及 DSH trace 路径、事件数、模型调用耗时/规模和工具调用计数 |
| `OptimizationSuggestion` | 单条优化建议 | 固定 `priority`/`category`，必须包含本次运行证据、可审计建议和预期收益 |
| `OptimizationAnalysis` | 自动优化分析产物 | 绑定 run 终态、指标/trace 路径、基础信号、最多 3 条建议及生成状态；模型失败时建议为空并记录原因 |

## 状态与持久化

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `RunState` | 全量检查点 | `phase` 四阶段枚举；`generation_loop` 为已完成轮次；`active_loop`/`loop_stage` 标记当前候选子阶段；`terminal_phase` 记录失败发生阶段；`terminal_status` 为终态；`optimization_analysis_path` 指向自动分析产物；`goal` 嵌入 `GenerationGoal` |
| `RunEvent` | 追加式事件 | `sequence` 自增；`phase` 当前阶段；`kind` 事件类型；`payload` 任意 JSON |
| `RunContext` | 后端执行上下文 | `run_dir`/`source_root`/`candidate_dir`/`binary_name` |
| `ExecutionLease` | 命令白名单授权 | `allowed_executables`（绝对路径）、`allowed_dirs`（目录级授权）；`commands_used` 计数；`authorize(argv)` 校验 |

## 终态映射

| 条件 | `TerminalStatus` |
|---|---|
| 执行分流 `accepted` + 未发现产品 crash | `harness_verified` |
| 产品 crash 稳定复现（3 次） | `bug_reproduced` |
| 证据无法明确归属 | `needs_review` |
| 预处理输入不完整 | `needs_input` |
| 环境/工具链/SDK 不可用 | `blocked` |
| loop 耗尽 / 模型输出持续无效 / 控制器内部错误 | `failed` |

## 合同校验点

- 所有路径字段禁止 `\\`、`..`、`/` 开头（`_validate_relative_path`）
- `BuildPlan` 的 `cflags`/`ldflags` 禁止 shell 元字符（`\x00`、`\n`、`\r`、`;`、`&&`、`\|\|`、`` ` ``、`$(`、`>`、`<`）
- 标准模式的 `GeneratedArtifactSet` 必须包含 harness + `Makefile`、`build.sh`、`endpoint.json`、`README.fuzz.md`
- build-dir 模式必须且只能包含 `harness.c`；控制器覆盖复制到 `<build-dir>/src/harness.cpp` 后执行用户预置 `build.sh`
- `build.sh` 输出识别优先使用 `GOALOOP_FUZZER=<path>`，其次解析标签和 `-o`；框架不扫描文件系统猜测产物，外部输出目录必须显式声明
- `GenerationDecision`：`completes_goal` 仅当 `ACCEPTED`；`NEEDS_REGENERATION` 必须带 `feedback`
- `PreprocessResult`：`ready` 与 `terminal_status` 互斥
- `ExecutionLease.authorize()`：同时支持精确名称匹配和 `shutil.which` 解析 + 目录级授权
