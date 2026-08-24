# goaloop-fuzz

`goaloop-fuzz` 是面向开源 C/C++ 项目的主动质量保障工具：用大模型生成 libFuzzer
harness，在用户授权的源码中编译、执行并发现可复现的软件缺陷，同时记录模型在
源码理解、harness 生成、构建适配和执行反馈改进方面的能力指标。

四阶段流程：预处理 → Harness 生成 → Harness 执行 → Crash 分析与报告。Python
控制器根据真实编译、fuzz 和覆盖证据决定是否完成生成目标；模型只产出严格 JSON
工件，不接触文件、命令与网络。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dsh,dev]'   # -e 开发模式；dsh=模型 SDK，dev=测试/静态检查
```

运行环境：Clang/Clang++（自带 libFuzzer）、`llvm-profdata`、`llvm-cov`、
`DEEPSEEK_API_KEY`。默认无沙箱运行（沙箱为可选，见
[doc/cli.md](doc/cli.md#沙箱选项)）。

## 快速使用

```bash
export DEEPSEEK_API_KEY='...'
goaloop doctor --profile default
goaloop run --source repos/cJSON-1.7.17 --function cJSON_Parse --profile default
```

- 源码放在 `repos/<project>/`（或任意自定义目录，见下）
- 所有输出写入 `work/<project>/runs/<run-id>/`，报告为 `report.md`
- 支持 `resume` / `status` / `report` / `evaluate`，详见 [doc/cli.md](doc/cli.md)
- 默认使用 DeepSeek 模型；也支持 OpenAI/Anthropic 等任意模型或 OpenAI 兼容
  网关（通过 pi-ai 适配器，`--model-profile` 选择，见
  [model-profiles/](model-profiles/) 示例与 [doc/cli.md](doc/cli.md#模型-profile)）

## 测试目标

被测试源码**由用户提供**（`repos/` 不入库）。默认约定放在
`repos/<project>/`，也接受**任意自定义目录**：

```bash
goaloop run --source /path/to/any/project --function <symbol> --profile default
```

不同项目可配置各自的验证 Profile（构建宏、链接库等构建知识固化在
`default_defines` / `default_include_dirs`），见 [profiles/c-ares.toml](profiles/c-ares.toml)
示例与 [doc/README.md](doc/README.md)。

## 模块结构

```
src/goaloop/
├── models.py          # Pydantic 数据契约
├── config.py          # 验证/模型 Profile 加载
├── preprocess.py      # 源码范围、符号查找、上下文收集、能力探测
├── storage.py         # run 目录、原子写入、事件日志、候选物化
├── validation.py      # 工件策略、编译/fuzz argv、指标解析、决策
├── backend.py         # LocalLinuxBackend：执行、资源限制、可选沙箱
├── coverage.py        # profraw 合并、llvm-cov 导出、覆盖归因
├── driver.py          # DeepSeek Harness SDK 适配器 + 测试驱动
├── crash.py           # 栈归属、输入最小化、独立复现
├── report.py / redaction.py  # 报告、研究指标、日志脱敏
├── workflow/          # 四阶段状态机（controller/generation/report）
└── cli.py             # run/resume/status/report/evaluate/doctor
```

## 文档

| 文档 | 内容 |
|---|---|
| [doc/README.md](doc/README.md) | 架构总览、目录结构、设计决策 |
| [doc/architecture.md](doc/architecture.md) | 四阶段状态机、数据流、断点恢复 |
| [doc/contracts.md](doc/contracts.md) | 数据契约全景、终态映射 |
| [doc/cli.md](doc/cli.md) | 命令参考、沙箱选项、环境变量 |
| [doc/testing.md](doc/testing.md) | 测试结构与端到端覆盖 |

测试与静态检查：`.venv/bin/python -m pytest tests/ -q`、`ruff check src tests`、
`mypy src/goaloop/`（详见 [doc/testing.md](doc/testing.md)）。

## 安全边界

- 仅测试用户明确授权且源码可用的 C/C++ 目标（源码只读，不修改）。
- 不访问网络（默认无沙箱时控制器本身不联网），不生成利用代码，不自动披露漏洞。
- 不执行模型生成的 shell 脚本，不使用 `shell=True`；每个候选只执行一次
  “编译 + 有界 fuzz”。
- 产品 crash 一旦稳定复现，不允许修改 harness 绕过触发条件。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
