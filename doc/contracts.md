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
| `SourceContext` | 函数级上下文 + SHA-256 | `kind` 区分 `target_function`/`incoming_tree`/`outgoing_tree`/`param_constraints`/`dependency`/`build`；目标片段带 `start_line`/`end_line`；`path` 必须相对、无 `..`、无 `\\`；`truncated` 标记预算截断 |
| `CapabilityReport` / `Capability` | 工具链能力探测 | `ready` 属性 = 全部 `available` |

## 生成阶段

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `GenerationGoal` | 控制器侧目标追踪 | `current_loop` ≤ `max_generation_loops`；`completed` 由控制器设置 |
| `EndpointPlan` | 函数签名、生命周期、构建依赖 | `location` 必须相对路径；`language` 限 `c`/`cpp` |
| `BuildPlan` | 编译计划 | `harness_file`/`binary_name` 必须相对路径；`cflags`/`ldflags` 禁止 shell 元字符（`\x00`、`\n`、`;`、`&&`、`$(`等）；`libraries` 必须在 `allowed_libraries` 内 |
| `GeneratedFile` | 单个生成文件 | `path` 必须相对；`content` ≤ 1,000,000 字符 |
| `GeneratedArtifactSet` | 模型响应（每轮） | `files` 4–64 个且路径唯一；`candidate_ready` 必须 `True`；必须携带 `schema_version`/`run_id`/`phase`/`generation_loop` |
| `GenerationFeedback` | 反馈给模型的执行证据 | `category`、`summary`、`log_excerpt`（已脱敏）、`artifact_hashes` |

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
| `ResearchMetrics` | 研究指标导出 | `token_source` 限 `sdk`/`unavailable`；`phase_durations`、`generation_loops_used`、`first_compile_success`、`time_to_bug_seconds` |

## 状态与持久化

| 模型 | 用途 | 关键约束 |
|---|---|---|
| `RunState` | 全量检查点 | `phase` 四阶段枚举；`generation_loop` 计数；`terminal_status` 终态；`goal` 嵌入的 `GenerationGoal` |
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
- `GeneratedArtifactSet` 必须包含 `harness_file` 的源文件 + `Makefile`、`build.sh`、`endpoint.json`、`README.fuzz.md`
- `GenerationDecision`：`completes_goal` 仅当 `ACCEPTED`；`NEEDS_REGENERATION` 必须带 `feedback`
- `PreprocessResult`：`ready` 与 `terminal_status` 互斥
- `ExecutionLease.authorize()`：同时支持精确名称匹配和 `shutil.which` 解析 + 目录级授权
