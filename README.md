# goaloop-fuzz

`goaloop-fuzz` 是面向开源 C/C++ 项目的主动质量保障工具：用大模型生成 libFuzzer
harness，在用户授权的源码中编译、执行并发现可复现的软件缺陷，同时记录模型在
源码理解、harness 生成、构建适配和执行反馈改进方面的能力指标。

四阶段流程：预处理 → Harness 生成 → Harness 执行 → Crash 分析与报告。Python
控制器根据真实编译、fuzz 和覆盖证据决定是否完成生成目标；模型只产出严格 JSON
工件，不接触文件、命令与网络。

## 安装

```bash
git clone --recurse-submodules https://github.com/wffx/goaLoopF.git
cd goaLoop
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dsh,dev]'   # -e 开发模式；dsh=模型 SDK，dev=测试/静态检查
```

已有 checkout 需初始化只读 kRepo 子模块：

```bash
git submodule update --init --recursive
```

运行环境：Clang/Clang++（自带 libFuzzer）、`llvm-profdata`、`llvm-cov`、
`DEEPSEEK_API_KEY`。每个被测仓还需由 VS Code C/C++ 扩展生成
`<repo>/.vscode/BROWSE.VC.DB`；预处理只读调用 `tools/kRepo` 的统一报告，取得
目标函数原始片段、上下游调用树和参数约束。默认无沙箱运行（沙箱为可选，见
[doc/cli.md](doc/cli.md#沙箱选项)）。

## 快速使用

```bash
export DEEPSEEK_API_KEY='...'
goaloop doctor --profile default
goaloop run --repo repos/cJSON-1.7.17 --source . --function cJSON_Parse --profile default
```

- 代码仓放在 `repos/<project>/`（或任意自定义目录，见下）；`--source` 指定仓内
  被测函数所在目录或文件
- 所有输出写入 `work/<project>/runs/<run-id>/`（可用 `--output <dir>` 指定
  产物根目录），报告为 `report.md`
- 支持 `resume` / `status` / `report` / `evaluate`，详见 [doc/cli.md](doc/cli.md)
- 默认使用 DeepSeek 模型；也支持 OpenAI/Anthropic 等任意模型或 OpenAI 兼容
  网关（通过 pi-ai 适配器，`--model-profile` 选择，见
  [model-profiles/](model-profiles/) 示例与 [doc/cli.md](doc/cli.md#模型-profile)）

## 测试目标

被测试源码**由用户提供**（`repos/` 不入库）。默认约定放在
`repos/<project>/`，也接受**任意自定义目录**：

```bash
goaloop run --repo /path/to/any/project --source src/parser.c --function <symbol> --profile default
```

不同项目可配置各自的验证 Profile（构建宏、链接库等构建知识固化在
`default_defines` / `default_include_dirs`），见 [profiles/c-ares.toml](profiles/c-ares.toml)
示例与 [doc/README.md](doc/README.md)。

**CMake 构建目录模式**：项目有现成 CMake 工程时，用 `--build-dir` 指向含
`CMakeLists.txt` 的目录。控制器在该目录内（out-of-source 到
`<build-dir>/goaloop-build`）用 clang + ASan/UBSan/覆盖插桩配置并构建，产出
的插桩静态库直接链接进 harness——模型只需写 harness，不再猜构建参数，显著
节省 token：

```bash
goaloop run --repo repos/<project> --source src/target.c --function <symbol> \
  --build-dir repos/<project> --profile default
```

## 模块结构

```
src/goaloop/
├── models.py          # Pydantic 数据契约
├── config.py          # 验证/模型 Profile 加载
├── preprocess.py      # 源码范围、符号查找、上下文收集、能力探测
├── krepo.py           # kRepo 只读子进程适配器（函数片段 + 上下游调用树）
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

第三方工具 `tools/kRepo/` 以 Git 子模块固定版本引入；goaloop 不修改其源码，也不调用
会生成源码包的命令，仅读取 `main.py report --format json` 的标准输出。可用
`GOALOOP_KREPO` 指向其他 kRepo 根目录或新版入口脚本 `main.py`。
适配不依赖 kRepo 的 `schema_version`；只提取 `source`、上下游调用树和
`param_constraints` 四项业务内容，并写入 `preprocess.json` 的 `contexts`。

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
