# 测试

## 运行测试

```bash
.venv/bin/python -m pytest tests/ -q           # 全部测试
.venv/bin/python -m pytest tests/test_workflow.py -q  # 仅端到端
.venv/bin/ruff check src tests                  # Lint
.venv/bin/mypy src/goaloop/                     # 类型检查（strict）
```

## 测试结构

```
tests/
├── conftest.py              # 共享 fixtures：workspace_root、default_profile、fake_api_key
├── helpers.py               # 生成工件 payload 构建器（make_artifact_payload 等）
├── fixtures/                # 真实 C 项目（供端到端编译 + fuzz）
│   └── repos/
│       ├── safe/            # safe_parse：无 bug 的 byte-sum 解析器
│       └── fragile/         # fragile_parse：intentional stack-buffer-overflow
├── test_models.py           # Pydantic 合同校验（字段约束、默认值、互斥规则）
├── test_config.py           # Profile 加载（TOML 解析、路径遍历防护、缺失报错）
├── test_preprocess.py       # 预处理（源码范围、符号查找、symlink 逃逸、语言检测、上下文截断）
├── test_trace.py            # 原始 DSH trace、摘要聚合、Terminal formatter、resume 序列恢复
├── test_optimization.py     # 自动分析规则、优先级、Markdown/report 渲染
├── test_storage.py          # 持久化（run 目录、原子写入、append-only 事件、物化、路径逃逸）
├── test_validation.py       # 策略/解析（ArtifactPolicy、compile/fuzz argv 组装、libFuzzer 指标解析、sanitizer 检测、决策策略、分流）
├── test_backend.py          # 后端执行（echo、timeout、白名单、输出截断、stdout_path 直写、bwrap argv 构造）
├── test_driver.py           # 模型驱动（JSON 提取、防陈旧、ScriptedGenerationDriver 行为、prompt 构建）
├── test_crash.py            # crash 分析（classify_stack、FakeExecutor 复现/最小化、ownership 映射）
├── test_coverage.py         # 覆盖归因（_parse_export、target 命中/未命中、无归属异常）
├── test_report.py           # 报告/脱敏（redaction、markdown 生成、validation_result、research_metrics）
└── test_workflow.py         # 端到端状态机（11 个场景，见下方）
```

## 端到端工作流测试（`test_workflow.py`）

全部使用真实 clang 编译 + libFuzzer 执行（无沙箱，default profile），模型阶段使用 `ScriptedGenerationDriver`（确定性 payload 序列）。

| 测试 | 场景 | 预期终态 |
|---|---|---|
| `test_harness_verified` | safe 项目 + 有效 harness | `harness_verified`（loop 1） |
| `test_resume_terminal_run_refreshes_report` | 已终态的 run 恢复 → 刷新报告 | `harness_verified`（不重执行） |
| `test_bug_reproduced` | fragile 项目（stack-buffer-overflow） | `bug_reproduced`（3 次复现，product） |
| `test_broken_then_fixed` | loop 1 编译错误 → loop 2 有效 | `harness_verified`（loop 2） |
| `test_no_reach_revises_via_coverage_feedback` | loop 1 未触达目标 → loop 2 有效 | `harness_verified`（loop 2） |
| `test_loop_budget_exhausted` | 2 轮全部编译失败 | `failed`（loop 2） |
| `test_missing_required_file_fails_after_budget` | max=1 且缺失 README.fuzz.md | `failed`（policy） |
| `test_harness_crash_returns_to_generation` | loop 1 harness 自身 crash（null deref）→ loop 2 有效 | `harness_verified`（loop 2） |
| `test_resume_continues_generation` | 中断后 resume → 继续生成 | `harness_verified`（loop 2） |
| `test_resume_reuses_materialized_candidate_after_execution_interrupt` | 执行中断后复用候选，不重复调用模型 | 同轮执行完成，无目录冲突 |
| `test_resume_applies_persisted_execution_without_rerunning_backend` | 已写 execution 检查点后中断 | 只重放决策，不重复编译/fuzz |
| `test_terminal_failure_routes_to_recorded_phase` | blocked/failed 按 `terminal_phase` 恢复 | preprocess/generation/execution 精确路由 |
| `test_needs_input_when_source_missing` | 源码目录不存在 | `needs_input` |
| `test_blocked_when_driver_unavailable` | 驱动不可用 | `blocked` |

## 如何添加新的端到端测试

1. 在 `tests/fixtures/repos/` 下创建新项目（C 源码 + 目标函数）
2. 在 `tests/helpers.py` 中（如需要）添加对应的 harness 模板
3. 在 `test_workflow.py` 中编写测试函数：
   ```python
   def test_my_scenario(self, workspace_root):
       request = _request(workspace_root, source="repos/myproj", function="my_func")
       payloads = [make_artifact_payload("myproj", "my_func")]
       controller = _controller(workspace_root, request, payloads, run_id="run-...")
       state = controller.run()
       assert state.terminal_status is TerminalStatus.HARNESS_VERIFIED
   ```

## ScriptedGenerationDriver

`ScriptedGenerationDriver` 是测试用确定性模型驱动，替代真实 DeepSeek API 调用：

- 构造函数接收 `payloads` 列表（dict），每个 dict 是 `GeneratedArtifactSet` 的 JSON payload
- 每次调用 `generate_artifacts()` 弹出下一个 payload，自动补全 `run_id`/`generation_loop`/`schema_version`
- 支持 `unavailable`（模拟驱动不可用）和 `interrupt_on_call`（模拟中断，供 resume 测试）

## 测试覆盖的关键边界

- **路径逃逸**：模型 schema 层（`_validate_relative_path`）+ 存储层（`ArtifactStore._contained`）双重防护
- **命令白名单**：`ExecutionLease.authorize()` 校验 argv[0] ∈ 允许清单或目录
- **防陈旧**：`DeepSeekHarnessDriver._coerce()` 校验 `schema_version`/`run_id`/`phase`/`generation_loop`
- **输出截断**：`max_output_bytes` 限制 + `stdout_path` 直写文件绕过
- **指标合并**：字段级合并（`target_function_hit`/`target_line_coverage`/`target_line_delta`），避免 `model_dump()` 全量覆盖 libFuzzer 计数器
- **RLIMIT_AS 与 ASan**：ASan 需 ~20TB 虚拟地址空间，不设 RLIMIT_AS（仅 RLIMIT_CPU + 墙钟超时）
