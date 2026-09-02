# 架构设计

## 四阶段状态机

```
                    ┌─────────────────────────────────────────────────────────────┐
                    │                     RunController.run()                     │
                    │  while state.terminal_status is None:                      │
                    │      dispatch(state.phase)                                 │
                    └─────────────────────────────────────────────────────────────┘
                                              │
            ┌─────────────────────────────────┼───────────────────────────────┐
            ▼                                 ▼                               ▼
   ┌─────────────────┐               ┌─────────────────┐             ┌─────────────────┐
   │   PREPROCESS    │               │HARNESS_GENERATION│             │CRASH_ANALYSIS_REPORT
   │ (preprocess.py) │               │ (workflow/)     │             │ (workflow/)     │
   └────────┬────────┘               └────────┬────────┘             └────────┬────────┘
            │                                 │                                 │
            │  ready?  ──────────── loop ─────┘                                 │
            │  │           ┌──────────────────┐                                │
            │  │  yes      │  driver.generate │                                │
            │  │           │  → validate      │◄── feedback (needs_regen)      │
            │  │           │  → materialize   │                                │
            │  │           │  → compile       │                                │
            │  │           │  → fuzz (30s)    │                                │
            │  │           │  → coverage      │                                │
            │  │           │  → decide        │                                │
            │  │           └──────┬───────────┘                                │
            │  │                  │                                             │
            │  │     ┌────────────┼────────────┐                               │
            │  │     ▼            ▼            ▼                               │
            │  │  accepted    needs_regen  crash_candidate                     │
            │  │     │            │            │                 ┌─────────────┤
            │  │     │        loop<max?  ┌────┴────┐             │             │
            │  │     │       ┌──yes──┘   │ product │ harness     │  terminal?  │
            │  │     │       │           │ bug_reproduced │     │  │ write     │
            │  │     │       │           │                  │     │  │ report    │
            │  │  harness_verified  budget exhausted → failed │  │  └───────────┘
            │  │                                              │  │
            │  │  not ready?  ────────────────────────────────┘  │
            │  │  needs_input / blocked                           │
            │  └──────────────────────────────────────────────────┘
            │
            ▼
    ┌──────────────────────────────────────────────────────────────────────────┐
    │  phase = CRASH_ANALYSIS_REPORT                                            │
    │  ┌─ crash analysis (if crash_candidate):                                 │
    │  │   classify_stack → minimize → reproduce 3× → ownership                │
    │  └─ write report + metrics + automatic optimization suggestions         │
    └──────────────────────────────────────────────────────────────────────────┘
```

进入 generation 后，Driver 始终订阅 DSH notification，将未脱敏原始记录追加到
`logs/dsh-trace.jsonl`，并同步维护 `logs/dsh-trace-summary.json`。该观察链路不向模型
新增 Bash、文件、网络或 subagent 权限。

进入终态后，`ReportMixin` 先写入 `research-metrics.json` 并采集不含建议的客观信号，再通过
DSH Python SDK 启动独立的 `<run-id>-optimization` session。该 session 读取有界的原始
session trace、workflow events、历轮 execution、kRepo 查询审计、指标和基础信号，最多
提炼 3 条带本次运行证据的工程建议。本地代码不按终态或阈值生成建议。模型不可用或严格
JSON 输出持续无效时，报告阶段不会被阻塞，但不会输出替代建议；分析产物标记 `generation_status=failed`、记录
`failure_reason` 并保持 `suggestions` 为空。优化建议写入
`optimization-suggestions.json/.md`，与 `report.md` 完全分离。

## 一次候选执行（compile → fuzz → coverage → decide）

```
┌─ compile ────────────────────────────────────────────────────────────────────┐
│  assemble_compile_request(artifacts, profile) → ProcessRequest(argv)         │
│  backend.execute → ProcessResult(exit_code, duration, stdout, stderr)        │
│  失败 → exit != 0 → needs_regeneration（反馈编译错误）                        │
└───────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼  exit == 0
┌─ fuzz ───────────────────────────────────────────────────────────────────────┐
│  assemble_fuzz_request(binary, corpus, crashes, fuzz_seconds, timeout)       │
│  LLVM_PROFILE_FILE = coverage/loop-<N>.profraw                               │
│  backend.execute → ProcessResult                                             │
│  退出原因：exit 0（正常） / exit != 0（crash/abort） / timed_out（超时）      │
└───────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─ coverage ────────────────────────────────────────────────────────────────────┐
│  llvm-profdata merge -sparse loop-<N>.profraw → loop-<N>.profdata            │
│  llvm-cov export --format=text → 解析 JSON                                    │
│  → target_function_hit（函数计数 > 0）                                        │
│  → target_line_coverage（source_root 下文件的行覆盖比例）                     │
│  失败 → coverage_valid = False → environment_error                           │
└───────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─ decide ──────────────────────────────────────────────────────────────────────┐
│  make_execution_result() → 执行分流（accepted / needs_regen / crash_candidate │
│                             / environment_error）                             │
│  decide_generation() → CoverageDecisionPolicy 判定：                          │
│    - 目标函数未命中 → needs_regeneration                                     │
│    - 覆盖/语料/feature 任一项无正向增长 → needs_regeneration                 │
│    - 全部满足 → accepted（completes_goal）                                    │
│  crash_candidate → 不经过覆盖判定，直接进入 crash 分析                        │
│  environment_error → blocked（不消耗 generation loop）                       │
└───────────────────────────────────────────────────────────────────────────────┘
```

## 数据流

```
FuzzRunRequest ──► preprocess ──► kRepo report（只读 BROWSE.VC.DB）
                                      │
                                      ▼
                   函数原片段 + incoming/outgoing tree + 参数约束
                                      │
                                      ▼
                              PreprocessResult ──► driver.generate_artifacts()
                                                          │
                                      dependency 需要时调用原生 Tool
                                              query_krepo_symbol
                                                          │
                                      Python 窄桥接执行 kRepo symbol
                                        + 持久化缓存/审计
                                                          │
                                      标准 tool/result 回填当前 session
                                                          │
                                                          ▼
                                              GeneratedArtifactSet（JSON）
                                                          │
                                                          ▼ validate + materialize
                                              candidate/（harness 源码 + 二进制）
                                                          │
                                                          ▼ compile + fuzz + coverage
                                              HarnessExecutionResult ──► decide_generation()
                                                          │                    │
                                                          ▼                    ▼
                                              GenerationDecision    CrashAnalysisResult
                                                          │
                                                          ▼
                                              ValidationResult + report.md
                                                          │
                                                          ▼
                                              ResearchMetrics + OptimizationAnalysis
```

## 模型集成

模型通过 **DeepSeek Harness Python SDK**（`deepseek_harness.DeepSeekHarness`）经 JSON-RPC stdio 调用 bundled runtime。专用 Cordis 组合（`cordis/goaloop.cordis.yml`）加载：
- `sdk-jsonrpc-server`（SDK 通信入口）
- `llm-deepseek`（模型适配器）
- `agent-spine-demo`（Agent 核心，屏蔽 Bash/文件/网络/子 Agent 工具）
- 本地 `goaloop-krepo-query` 插件（注册 `query_krepo_symbol` 原生 Tool）
- `goal` + `tool-goal`（持久化 same-session goal + 模型工具）
- `session-persistence-jsonl`（会话持久化）

模型通过 `create_goal`/`get_goal`/`update_goal` 管理生成进度，通过
`query_krepo_symbol(symbol, repo, function, file, kind?)` 按需读取非函数依赖，最终仅返回
结构化 JSON（`GeneratedArtifactSet`）。Controller 为每个 generation session 写入
repo/function/file 绑定并在 prompt 中提供对应值；插件拒绝与绑定不一致的调用，只负责
Tool 生命周期和固定、无 shell 的 Python bridge
进程，Python
`KRepoQueryService` 继续负责白名单、超时、缓存和审计。查询次数不受 session 限制。
控制器负责 JSON 提取、
schema 验证、防陈旧检查、单次格式修复重试，以及所有文件写入、构建执行和最终完成 goal 的决策。

详见 [driver.py](../src/goaloop/driver.py)。

## 断点恢复（resume）

每个阶段转换和关键事件写入 `events.jsonl`（append-only）和 `state.json`（RunState 全量）。`goaloop resume --run-id <id>` 先获取 run 目录的非阻塞独占锁，再从磁盘加载：
- `state.json` → 当前 phase、generation_loop、active_loop、loop_stage、terminal_status、terminal_phase
- `preprocess.json` → 跳过预处理
- `goal.json` → 恢复目标与反馈
- `executions/loop-*/execution.json` → 最后执行证据
- `crash-analysis.json`（如存在）→ 跳过已完成的 crash 分析

候选轮次按 `model_generation → materialized → executing → executed` 保存子阶段检查点：

- `materialized`/`executing`：读取原 `response.json` 和 `candidate/`，不再次调用模型或创建目录；未完成的编译/fuzz 可以安全重跑。
- `executed`：读取 `execution.json`，只重新应用确定性决策。
- `environment_error`：不消耗 generation loop，修复环境后用同一候选重新执行。

`blocked`/`failed` 根据 `terminal_phase` 回到 preprocess、harness generation 或 harness execution；报告阶段失败、不可恢复终态和已耗尽轮次预算的失败保持终态。候选目录通过临时目录完整写入后原子 rename，避免半成品被当作有效候选。锁在 `RunController.close()` 释放，锁文件保留最后一次持有者的 PID/时间用于诊断。

## 安全边界

- 模型不能读写文件、执行命令、访问网络、委托子 Agent（Cordis 组合屏蔽）
- 标准模式不执行模型生成的 shell 脚本（`build.sh`/`Makefile` 仅供审阅，权限 0600），编译命令由 `BuildPlan` 组装；`--build-dir` 模式只生成 `harness.c`，覆盖安装为 `<build-dir>/src/harness.cpp`，执行的是用户预置的可信 `<build-dir>/build.sh`
- 源码目录只读（`repos/`），run 产物只在 `work/<project>/runs/<id>/` 下写入
- 产品 crash 一旦稳定复现，禁止修改 harness 绕过触发条件
- 日志和模型反馈脱敏（凭据、绝对路径、用户名；`redaction.py`）
