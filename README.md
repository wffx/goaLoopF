# goaloop-fuzz

`goaloop-fuzz` 按 `plan_dsh.md` 实现四阶段流程：预处理、Harness 生成、Harness
执行、Crash 分析与报告。Goal loop 只发生在 Harness 生成阶段；Python 控制器根据真实
编译、fuzz 和覆盖证据决定是否完成 goal。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dsh,dev]'
```

上面第三条命令的含义（**在项目根目录执行**）：

- `python -m pip` — 用当前解释器（这里是 venv 里的 Python）调用 `pip` 模块；
- `install -e .` — 以 **editable（开发模式）** 安装当前目录的包：不复制源码，而是把
  `src/goaloop` 链接进环境，改代码立即生效（即 `goaloop` 命令直接使用你正在编辑的源码）；
- `'.[dsh,dev]'` — 请求两个**可选依赖组**：`dsh`（`deepseek-harness-sdk`，真实模型生成所需）
  和 `dev`（pytest、ruff、mypy，测试与静态检查所需）；单引号是 shell 必需的，否则
  `[dsh,dev]` 会被当作通配符展开。

运行环境需要 Clang/Clang++（自带 libFuzzer）、`llvm-profdata`、`llvm-cov` 和
`DEEPSEEK_API_KEY`。**默认运行 libFuzzer harness 不需要沙箱**：进程直接执行，仅施加墙钟
超时 + RLIMIT_CPU（内存由 libFuzzer `-rss_limit_mb` 约束）。bubblewrap 沙箱是**可选加固**
（禁用网络、只读绑定源码、仅 run 目录可写），启用方式见下方「沙箱（可选）」。
源码必须放在 `repos/<project>/`，所有输出写入 `work/<project>/runs/<run-id>/`。

```bash
export DEEPSEEK_API_KEY='...'
goaloop doctor --profile default
goaloop run --source repos/example --function example_parse --profile default
```

真实模型生成使用 DeepSeek Harness Python SDK，不调用 `dsh --profile headless`，也不解析
终端文本。生成的 `build.sh` 和 `Makefile` 仅供审阅；控制器只执行经 Profile 和结构化
`BuildPlan` 校验后自行组装的 argv。

## 沙箱（可选）

默认 Profile（`profiles/default.toml`）`sandbox.required = false`，无需 bubblewrap 即可
运行。如需在沙箱中执行编译/fuzz/覆盖命令：

```bash
# 1. 安装 bubblewrap（Linux），并确认内核允许非特权 user namespaces
sudo apt install bubblewrap

# 2. 用沙箱 Profile 运行（与 default 相同的工具链与覆盖策略，仅多了沙箱隔离）
goaloop doctor --profile sandboxed
goaloop run --source repos/example --function example_parse --profile sandboxed
```

沙箱行为：`--unshare-net` 禁用网络、`--ro-bind` 只读绑定产品源码、仅当前 run 目录可写、
`--rlimit-rss/nproc/cpu` 施加内存/进程数/CPU 限制、`--die-with-parent` 保证超时回收整个
进程树。沙箱不可用（未装 bwrap）时，`sandboxed` Profile 会在预处理阶段判为 `blocked`，
不会静默降级。

## 模块结构

- `models.py` — 全部 Pydantic 数据契约（请求、Profile、Goal、工件、执行结果、决策、指标）。
- `config.py` — 验证/模型 Profile 的 TOML 加载。
- `preprocess.py` — 源码范围检查、符号查找、上下文收集、能力探测。
- `storage.py` — run 目录、原子写入、append-only 事件、候选版本物化与哈希。
- `validation.py` — 生成工件静态策略、编译/fuzz argv 组装、libFuzzer 指标解析、决策策略。
- `backend.py` — `LocalLinuxBackend`：bubblewrap 沙箱（可选）、argv 执行、超时与资源限制。
- `coverage.py` — profraw 合并、llvm-cov 导出、目标函数命中与目标源码覆盖归因。
- `driver.py` — DeepSeek Harness SDK 适配器（严格 JSON、一次格式修复重试、防陈旧响应）
  与测试用 `ScriptedGenerationDriver`。
- `crash.py` — sanitizer 栈归属、crash 输入最小化、3 次独立复现。
- `report.py` / `redaction.py` — Markdown/JSON 报告、研究指标导出、日志脱敏。
- `workflow/` — 四阶段状态机与控制器（`controller.py` 核心循环与断点恢复、
  `generation.py` 生成循环、`report.py` crash 分析与报告）。
- `cli.py` — `run` / `resume` / `status` / `report` / `evaluate` / `doctor`。

## 本地开发调测

默认 Profile（无沙箱）即可直接开发调测，无需额外工具：

```bash
goaloop doctor --profile default
goaloop run --source repos/example --function example_parse --profile default
```

测试：

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check src tests
.venv/bin/mypy src/goaloop/
```

端到端测试使用真实 clang 编译与 libFuzzer 执行：`tests/fixtures/repos/safe` 生成
`harness_verified`；`tests/fixtures/repos/fragile` 稳定复现 stack-buffer-overflow 并终止为
`bug_reproduced`。

## 测试目标（repos/）

`repos/` 存放**用户提供**的待测 C/C++ 源码，不随本仓库分发（已加入
`.gitignore`）。每个项目可对应一个专用验证 Profile（如
`profiles/c-ares.toml`），把该项目 configure/构建阶段检测出的编译宏
（`default_defines`）与构建产物头文件目录（`default_include_dirs`，如
`build-config/c-ares/ares_build.h`）固化为构建知识，控制器在编译时强制附加，
不依赖模型猜测。示例：`c-ares` 1.11.0 需要 `HAVE_WRITEV=1`、`HAVE_RECV=1` 等
50 个宏，固化后一次配置、稳定复现。

```bash
# 用户侧：放置目标源码 + 建立专用 Profile
mkdir -p repos/<project>
cp -r /path/to/<project> repos/<project>/
# （可选）创建 profiles/<project>.toml 声明项目构建知识
```

## 安全边界

- 仅测试用户明确授权且源码可用的 C/C++ 目标。
- 不修改 `repos/`，不访问网络，不生成利用代码，不自动披露漏洞。
- 不执行模型生成的 shell 脚本，也不使用 `shell=True`。
- 每个候选只进行一次“编译 + 有界 fuzz”；没有独立 smoke test。
- 产品 crash 一旦稳定复现，不允许修改 harness 来绕过触发条件。

开发机可以在 WSL2 中运行同一套 Linux CLI；工程核心不依赖 `wsl.exe`。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
