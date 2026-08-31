# goaloop-fuzz 工程文档

目标：**大模型辅助生成 C/C++ libFuzzer harness，在用户授权的源码中发现可复现的软件缺陷，并记录模型在源码理解、harness 生成、构建适配和执行反馈改进方面的能力指标。**

## 一句话架构

**Python 控制器（四阶段状态机）驱动 DeepSeek Harness 模型生成 JSON 工件，经编译 + 有界 fuzz + 覆盖归因的真实证据闭环迭代，直到达成目标或预算耗尽。**

## 目录结构

```
goaloop/
├── src/goaloop/
│   ├── models.py          # Pydantic 数据契约（全工程共享）
│   ├── config.py          # TOML Profile 加载
│   ├── preprocess.py      # 预处理：源码范围、符号查找、上下文收集、能力检查
│   ├── storage.py         # 执久化：run 目录、原子写入、append-only 事件、候选物化
│   ├── validation.py      # 静态策略、编译/fuzz/argv 组装、libFuzzer 指标解析、决策
│   ├── backend.py         # LocalLinuxBackend：bubblewrap 沙箱（可选）、进程执行、资源限制
│   ├── coverage.py        # profraw 合并、llvm-cov 导出、目标函数/源码覆盖归因
│   ├── driver.py          # DeepSeek Harness SDK 适配器 + 测试用 ScriptedGenerationDriver
│   ├── crash.py           # sanitizer 栈归属、crash 输入最小化、独立复现
│   ├── redaction.py       # 日志/研究导出脱敏（凭据、路径、用户名）
│   ├── report.py          # Markdown/JSON 报告、验证结果、研究指标
│   ├── workflow/          # 四阶段控制器（controller/generation/report mixin）
│   └── cli.py             # Typer CLI（run/resume/status/report/evaluate/doctor）
├── profiles/              # 验证 Profile（通用 default/sandboxed + 项目专用如 c-ares）
├── model-profiles/        # 模型 Profile（provider/model/cordis）
├── cordis/                # DeepSeek Harness 运行时 Cordis 组合
├── tests/                 # pytest 测试套件（含真实 clang + libFuzzer 端到端）
├── build-config/          # 项目构建产物头文件（如 c-ares 的 ares_build.h）
├── repos/                 # 被测试的 C/C++ 源码（由用户放置，不入库）
├── work/                  # 运行时产物（每个 run 的根目录）
└── .private-sessions/     # 模型会话原始记录（仅供恢复/审计，不公开）
```

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dsh,dev]'
export DEEPSEEK_API_KEY='sk-...'
goaloop doctor --profile default
goaloop run --repo repos/cJSON-1.7.17 --source cJSON.c --function cJSON_Parse --profile default
```

具体说明见 [CLI 参考](cli.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 四阶段状态机、数据流、决策逻辑、断点恢复 |
| [contracts.md](contracts.md) | Pydantic 数据契约（全模型、字段关系、约束） |
| [cli.md](cli.md) | CLI 命令参考与参数说明 |
| [testing.md](testing.md) | 测试结构、fixture 设计、端到端覆盖 |
| [context-optimization.md](context-optimization.md) | 已实施：preprocess 基础上下文 + generation 按需 kRepo dependency 查询 |
| [todo.md](todo.md) | 待办项：将 kRepo 查询迁移为 DSH 原生 Tool |

## 关键设计决策速览

- **模型只产出 JSON，不操作文件/命令**：Cordis 组合屏蔽了 Bash、文件编辑、网络、子 Agent 工具；模型通过持久化 goal 管理进度，控制器验证 JSON 合法性后才物化到磁盘。
- **多模型支持**：默认 DeepSeek 官方适配器；也提供 pi-ai 多 provider 组合
  （`cordis/goaloop.pi-ai.cordis.yml`），可接 OpenAI/Anthropic 等任意模型或
  OpenAI 兼容网关，`model-profiles/*.toml` 声明 provider/model/base_url/
  api_key_env，凭据检查随 profile 变化。
- **每个候选只执行一次“编译 + 有界 fuzz”**：没有独立的 smoke test；编译失败、fuzz 异常、覆盖不达标统一走反馈修订。
- **产品 crash 一旦稳定复现，禁止修改 harness 绕过**：crash 归属 → 最小化 → 独立复现 3 次 → 终态 `bug_reproduced`。
- **沙箱是可选的默认关闭**：`profiles/default.toml` 无沙箱，`profiles/sandboxed.toml` 可选开启 bubblewrap。
- **断点恢复**：每个阶段转换 + 核心事件写入 `events.jsonl` 和 `state.json`，支持 `resume` 续跑，不重复执行已完成的证据。
- **严格 JSON 防陈旧**：模型响应必须携带 `schema_version`、`run_id`、`phase`、`generation_loop`，控制器验证一致后才采纳；字段不匹配立即拒绝，防止旧轮次/跨 run 响应被错误应用。
- **单次格式修复重试**：JSON 解析或 schema 校验失败仅允许一次携带精确错误信息的重试；再次失败终止本轮生成。
