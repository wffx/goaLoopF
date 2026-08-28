# Python + DeepSeek Harness 的 Fuzz Harness Generation Goal Loop

最后刷新：2026-08-19

## 1. 工程目标

本工程面向开源社区项目进行主动质量保障，提前发现软件bug并推动修复，辅助项目维护者提升代码健壮性。这是一个用于开源项目守护与防御性的测试工具，不是用于未授权访问或破坏的攻击性工具。

本工程使用大模型辅助生成和迭代改进 C/C++ libFuzzer harness，用于在用户已授权的产品源码中发现可复现的软件缺陷，并记录模型在源码理解、harness 生成、构建适配和执行反馈改进方面的能力指标。

首版边界如下：

- 用户明确提供源码目录和目标函数，不由模型自主选择产品攻击面。
- 只支持源码可用的 C/C++ 目标和 libFuzzer。
- 只把 ASan、UBSan、目标程序 crash、断言失败以及稳定可复现的 timeout/hang 作为 bug 证据。
- 不修改产品源码，不生成利用代码，不访问未授权目标，不自动公开或提交漏洞。
- 生成物、构建结果、日志和 crash 输入均写入独立的 `work/<project>/runs/<run-id>/`。

`harness_verified` 只表示 harness 已编译、执行且满足本次配置的覆盖策略，并在有限 fuzz 预算内没有发现 bug，不代表产品不存在 bug。

## 2. 总体架构

### 2.1 Python 是控制平面

工程使用 Python 3.11+ 实现：

- CLI 和配置加载。
- Harness 生成 Goal loop、工作流状态和断点恢复。
- 源码范围检查和上下文提取。
- Pydantic 数据契约与模型响应校验。
- 生成物的原子写入和版本留痕。
- Linux 执行 backend、sandbox、harness 编译和 fuzz 执行。
- sanitizer/libFuzzer 日志解析、crash 复现和研究指标导出。

使用 `pyproject.toml` 和 `uv.lock` 固定依赖。核心依赖包括 `deepseek-harness-sdk`、Pydantic 2 和 Typer；测试与静态检查使用 pytest、Ruff 和 mypy。`src/goaloop/` 按领域划分为 CLI、配置/契约、状态机、DeepSeek Harness 适配器、源码检查、生成物存储、执行 backend、验证结果解析和研究数据导出。

### 2.2 DeepSeek Harness 是模型运行时

正式链路通过[官方 Python SDK](https://github.com/deepseek-ai/deepseek-harness/tree/master/python)调用 DeepSeek Harness：

```python
from deepseek_harness import DeepSeekHarness

with DeepSeekHarness(
    provider=model_profile.provider,
    model=model_profile.model,
    max_tokens=model_profile.max_tokens,
    cwd=str(run_workspace),
    session_root=str(private_session_root),
    cordis=str(cordis_config),
) as harness:
    result = harness.run(
        phase_prompt,
        session_id=run_id,
    )
```

同一 run 的 Harness 生成迭代复用一个 `session_id`：Python 首次提交 `PreprocessResult`，后续仅在需要再生成时提交静态检查或执行反馈。预处理、编译执行、覆盖判定和 Crash 复现由 Python 完成，不依赖模型会话。SDK 通过 JSON-RPC stdio 启动 bundled dsh runtime；开发和正式运行均不依赖对终端文本的脆弱解析。

默认模型为 `deepseek-v4-pro`，允许模型 Profile 覆盖。凭据从 `DEEPSEEK_API_KEY` 读取，可通过 `DEEPSEEK_BASE_URL` 选择企业代理或自托管端点。SDK 的基本调用方式参考[官方 Python 示例](https://github.com/deepseek-ai/deepseek-harness/blob/master/examples/jsonrpc-agent/minimal.py)。

专用 Cordis composition 加载模型、agent、session persistence、`goal` 及其模型工具和必要的上下文管理能力，不向模型暴露通用 Bash、文件编辑、网络或子 Agent 工具。模型通过持久化 goal 管理生成进度并只返回结构化 JSON；Python 控制器才拥有受限读取源码、写入生成物、执行命令和最终完成 goal 的权限。

Harness 生成阶段的所有模型响应都必须匹配对应的 Pydantic schema。JSON 解析或 schema 校验失败时，只允许一次携带精确错误信息的格式修复重试；再次失败则终止本轮生成并进入报告阶段。

### 2.3 Linux 是统一运行平台

工程核心以 Linux 为目标平台：

- 开发与调测时，在 WSL2 内运行同一套 Python 代码和 `LocalLinuxBackend`。
- 原生 Linux 使用 `LocalLinuxBackend`、验证 Profile 和统一数据契约运行。
- WSL 不是状态机中的特殊验证 backend；Windows 侧启动器只负责进入指定 distro 和转换工作区路径。

执行层定义稳定的 backend 协议：

```python
class ExecutionBackend(Protocol):
    def probe(self, profile: ValidationProfile) -> CapabilityReport: ...
    def prepare(self, run: RunContext) -> ExecutionLease: ...
    def execute(self, request: ProcessRequest) -> ProcessResult: ...
    def collect(self, run: RunContext) -> CollectedArtifacts: ...
    def close(self, run: RunContext) -> None: ...
```

当前执行 backend 为 `LocalLinuxBackend`，通过 backend 注册表加载。

沙箱是**可选的加固层**：默认运行 libFuzzer harness 不经过沙箱（直接执行，仅施加墙钟超时与 CPU 限制，内存由 libFuzzer `-rss_limit_mb` 约束）；bubblewrap 沙箱由验证 Profile 显式开启（`sandbox.required = true`）。沙箱开启时：禁用网络 namespace、只读绑定产品源码、只允许当前 run 目录写入，并施加 timeout、CPU、内存和进程数限制。所有进程使用 argv 数组启动，禁止 `shell=True`；允许执行的程序和参数前缀来自用户审阅的验证 Profile。

验证 Profile 不写入运行工件。Linux 默认从 `~/.config/goaloop/profiles/` 读取；Windows 开发启动器可将 `%LOCALAPPDATA%\goaLoop\profiles/` 映射到 WSL。Profile 只声明 backend、sandbox、工具路径、命令白名单、资源上限和执行授权，不保存模型或系统凭据。

## 3. 四阶段 Workflow

工作流包含以下四个顶层阶段。Goal loop 只用于第二阶段的 Harness 生成，并通过第三阶段的编译、执行和覆盖反馈决定是否继续下一轮生成。

### 3.1 `preprocess`：预处理

预处理负责输入、环境和确定性源码检查，不调用模型：

- 解析 `FuzzRunRequest`，校验源码、目标函数、语言、模型/验证 Profile 和生成 loop 次数。
- 确认源码位于 `repos/<project>/`，解析真实路径并阻止符号链接逃逸。
- 检查 DeepSeek 凭据、SDK runtime、Clang/Clang++、构建工具、libFuzzer、`llvm-profdata`、`llvm-cov`、sandbox、磁盘空间和输出目录。
- 使用 `rg` 和受限文件读取收集公共头文件、目标实现、测试/示例、构建文件、现有 fuzz 配置和候选函数签名。
- 生成不可变 request、`PreprocessResult`、能力报告和有界 `ExecutionLease`。

输入缺失时记录 `needs_input`；环境或隔离条件不满足时记录 `blocked`。两者都不消耗 generation loop，并直接进入第四阶段生成诊断报告，不调用 Harness 生成模型。

### 3.2 `harness_generation`：Fuzz harness 生成

Python 为本次 run 创建一个具体且持久化的 `GenerationGoal`：**生成可编译、可执行并能触达用户指定目标函数的 libFuzzer harness 文件**。DeepSeek Harness 使用同一 session 和 goal 持续生成或修订：

- 第一次迭代根据 `PreprocessResult` 生成 `EndpointPlan` 和 `GeneratedArtifactSet`。
- 后续迭代接收静态检查错误或上一轮执行证据，输出完整替换后的工件，不直接操作文件或 shell。
- 每次模型生成或修订计为一个 generation loop；用户通过 CLI 或 Profile 配置 `max_generation_loops`，默认 5，允许范围为 1–20。
- 模型只能声明候选工件已经准备好验证；只有 Python schema、路径和静态策略检查通过后，候选版本才可进入执行阶段。模型不能自行完成 goal，最终完成权属于 Python 控制器，并且只能依据第三阶段的真实执行结果。

工件至少包含 harness 源码、`Makefile`、`build.sh`、`endpoint.json`、`README.fuzz.md` 和可选 corpus seeds。Python 原子保存每轮版本、哈希和差异。

### 3.3 `harness_execution`：Fuzz harness 执行

该阶段不执行独立 smoke test；每个候选版本只执行一次“编译 + 有界 fuzz”验证：

- 使用 Profile 白名单内的 argv 编译 harness，默认启用 `-fsanitize=fuzzer,address,undefined`；同时启用 Clang source-based coverage 所需的 `-fprofile-instr-generate -fcoverage-mapping`。
- fuzz 默认单 worker、600 秒、`-timeout=5`、`-rss_limit_mb=2048`、`-max_len=1048576`。
- 运行时设置本轮独立的 `LLVM_PROFILE_FILE`，结束后使用 `llvm-profdata` 和 `llvm-cov` 取得目标文件/函数的命中与覆盖数据。
- 记录编译结果、退出原因、运行时间、libFuzzer `cov`/`ft`、目标函数命中、目标源码覆盖、coverage/feature 增量、exec/s、corpus 增长和 artifact，形成统一 `HarnessExecutionResult`。

执行结果只有四种分流：

- `accepted`：编译成功、执行完成且覆盖策略达标，完成 GenerationGoal 并进入第四阶段生成无 crash 报告。
- `needs_regeneration`：编译失败、harness 自身错误、没有触达目标代码，或覆盖率/feature/corpus 增长不满足 `CoverageDecisionPolicy`；若 loop 尚有余额，将证据反馈给 `GenerationGoal` 并返回第二阶段，否则进入第四阶段报告预算耗尽。
- `crash_candidate`：进入第四阶段分析，不得先修改 harness。
- `environment_error`：记录为 `blocked` 并进入第四阶段生成诊断报告，不得消耗 generation loop 伪装修复环境。

### 3.4 `crash_analysis_report`：Crash 分析与生成报告

第四阶段是所有路径的统一分析/报告入口，包含两个连续动作，但对状态机表现为一个顶层步骤：

1. **Crash 分析**：仅对 `crash_candidate` 执行。使用 Profile 授权的命令解析 sanitizer 栈，区分 harness 帧和产品源码帧；最小化 crash 输入并独立复现 3 次。产品证据一致时为 `bug_reproduced`，来源不明确时为 `needs_review`。仅证明是 harness 自身错误且仍有 loop 余额时，才允许跳过本轮报告并返回第二阶段。
2. **生成报告**：对 `accepted`、loop 耗尽、Crash 分析结论、`needs_input`、`blocked` 和内部失败全部执行，输出 `ValidationResult`、Markdown 报告、研究指标、fuzzer/corpus/crash 工件的相对路径以及每轮 GenerationGoal 的结果；不存在的工件明确标记为 `not_produced`。

产品 crash 一旦成立，不允许通过修改 harness 绕过触发条件。已经完成的执行和证据不得在恢复时重复覆盖。

终态固定为 `harness_verified`、`bug_reproduced`、`needs_review`、`needs_input`、`blocked` 和 `failed`，映射规则如下：

- `accepted` 且未发现产品 crash：`harness_verified`。
- 产品 crash 稳定复现：`bug_reproduced`；证据无法明确归属：`needs_review`。
- 输入不完整：`needs_input`；执行环境、SDK 或隔离条件不可用：`blocked`。
- generation loop 耗尽、模型输出持续无效、harness 问题无预算继续修订或控制器内部错误：`failed`。

## 4. Generation goal loop 与失败反馈

第三阶段决定当前候选是否完成 `GenerationGoal`。Python 根据编译状态、目标函数命中、目标源码覆盖、libFuzzer `cov`/`ft` 增量、corpus 增长、exec/s、运行终止原因和 sanitizer 证据产生确定性的 `GenerationDecision`。

决策规则由验证 Profile 中的 `CoverageDecisionPolicy` 声明，不在 prompt 中临时改变。默认策略至少要求：编译成功、目标函数存在非零执行计数、没有 harness 自身 sanitizer 错误，并且 libFuzzer `cov`、`ft`、corpus 或目标源码覆盖至少一项产生正向增量。Profile 可以提高阈值，但不能关闭目标函数命中要求或将异常退出当作通过。

编译失败、harness 自身错误、目标函数未命中或覆盖增长不足时必须返回生成阶段。只有全部策略条件满足时，Python 才将 goal 标记为完成。若覆盖工件损坏、缺失或无法归属到目标源码，则结果是 `environment_error`，不能猜测为覆盖不足并消耗 generation loop。

需要继续生成时，Python 将脱敏编译/执行日志、覆盖指标、当前工件哈希、失败分类和允许修改的文件列表反馈给同一 `GenerationGoal` 和 dsh session，由模型生成下一版完整工件。

允许返回生成阶段的问题包括编译/链接错误、错误 API 签名、缺少初始化/清理、输入适配错误、harness 自身 sanitizer 问题、目标代码未触达、覆盖增长不足，以及生成的构建脚本错误。以下情况不得进入下一轮生成：

- 产品源码中的 crash 已稳定复现。
- SDK、工具链、sandbox、Profile 或依赖环境不可用。
- 需要修改产品源码、关闭 sanitizer、访问网络或扩大 execution lease。
- generation loop 次数已经耗尽。

每轮修订后都必须重新执行静态策略检查和第三阶段，不能依据模型解释直接宣布成功。模型 JSON 格式修复仍只允许一次，且不计入 `max_generation_loops`；它与 harness 的生成/修订能力分开统计。

## 5. 公共接口与数据契约

CLI：

```text
goaloop run --repo repos/<project> --source <dir-or-file> --function <symbol>
             --language auto|c|cpp --profile <profile>
             [--model-profile <id>] [--max-generation-loops 5]
             [--fuzz-seconds 600]

goaloop resume --run-id <id>
goaloop status --run-id <id> [--json]
goaloop report --run-id <id> [--format json|markdown]
goaloop evaluate --suite <manifest.json> --repetitions 3
goaloop doctor --profile <profile>
```

核心 Pydantic 契约及用途：

- `FuzzRunRequest`：源码、目标函数、语言、模型/验证 Profile、`max_generation_loops` 和执行预算。
- `ValidationProfile`：backend、sandbox、工具链、命令白名单、资源限制和执行授权。
- `PreprocessResult`：预处理收集的目标源码上下文、构建事实、候选签名和能力报告。
- `GenerationGoal`：生成目标、验收条件、最大 loop 数、当前 loop 和最近执行反馈。
- `EndpointPlan`：函数签名、生命周期、输入模型和构建依赖。
- `GeneratedArtifactSet`：每一轮生成或修订后的完整文件内容、理由及用途。
- `CoverageDecisionPolicy`：目标函数命中、目标源码覆盖、libFuzzer coverage/feature、corpus 增长及执行健康度的判定规则。
- `HarnessExecutionResult`：harness 编译、fuzz 执行、覆盖指标和四类分流状态。
- `GenerationDecision`：当前候选完成 goal、返回生成、进入 Crash 分析或环境阻断的确定性决定。
- `RunState`：四阶段状态、generation loop、预算、checkpoint 和追加式事件引用。
- `ExecutionLease`：本次允许执行的命令、目录、时限和最大次数。
- `ProcessRequest` / `ProcessResult`：backend 无关的执行请求与脱敏结果。
- `ValidationResult`：执行、Crash 分析、覆盖、复现和终态。
- `ResearchMetrics`：模型/provider 标签、prompt 版本、token、时延、generation loop 数、首次编译率、覆盖增长和发现 bug 时间。

上述 Pydantic 模型需要导出 JSON Schema。模型返回的每一种 JSON 对象都携带 `schema_version`、`run_id`、`phase` 和 `generation_loop`，防止旧响应、旧轮次或跨 run 响应被错误应用。格式修复沿用同一 `generation_loop`，以单独的 `format_retry` 计数。

每个 run 写入 `work/<project>/runs/<run-id>/`，其中包含生成物、append-only 事件、脱敏日志、验证结果、研究指标、fuzzer 二进制和 crash reproducer。控制状态和证据使用相对路径互相引用，不复制 Profile 原文。

## 6. 研究数据与隐私

记录以下指标：

- provider、model、prompt 版本和 endpoint 标签。
- 实际 token 用量或“SDK 未提供”的明确状态。
- 四阶段耗时、模型轮次、generation loop 使用量和格式修复次数。
- 各 generation loop 的工件变化、首次编译成功率和最终状态。
- fuzz 覆盖、语料规模、exec/s 和发现 bug 时间。

原始 dsh session 仅放在访问受限的 private 目录，用于恢复和审计。公开研究导出移除密钥、源码正文、用户名、绝对路径和端点凭据，以哈希或占位符保留事件关系。

## 7. 测试与验收

- 单元测试覆盖四阶段状态迁移、预处理、路径逃逸、命令白名单、schema 校验、generation loop 预算、脱敏和 libFuzzer 日志解析。
- backend 合约测试覆盖 probe、只读源码、独立输出、timeout、取消、工件收集和清理。
- WSL 集成测试直接运行 `LocalLinuxBackend`，验证源码不可写、run 目录可写、网络不可达和资源限制生效。
- 原生 Linux CI 运行相同测试，确保核心不依赖 `wsl.exe` 或 Windows 路径。
- dsh replay/mock 测试固定 goal 创建、首次生成、编译反馈修订、覆盖反馈修订、goal 完成和 loop 耗尽行为。
- 执行测试必须证明流程只编译并运行 fuzzer，不执行额外 `-runs=10` smoke。
- 覆盖采集测试验证每轮独立 `.profraw`、`profdata` 合并、目标函数命中和目标源码覆盖归属；覆盖决策测试分别验证编译失败、目标未触达、覆盖增长不足、覆盖达标、覆盖工件损坏和 crash candidate 的分流。
- 安全 fixture 必须完成 `preprocess -> harness_generation -> harness_execution -> crash_analysis_report -> harness_verified`。
- 脆弱 fixture 必须产生产品源码栈、保存并最小化 crash 输入、复现 3 次并终止为 `bug_reproduced`。
- 注入错误 harness，验证执行反馈返回同一 GenerationGoal，且达到用户配置的 loop 上限后停止。
- 注入恶意源码注释，验证模型无法获得 shell、越界读取、修改 Profile 或开启网络。

真实 DeepSeek API 测试只在显式提供凭据时执行；普通 CI 使用回放或 mock runtime，不消耗外部模型额度。

## 8. 默认假设与暂不支持范围

- 用户拥有测试目标的授权，并负责预置目标依赖和验证 Profile。
- 默认模型为 `deepseek-v4-pro`，允许由模型 Profile 覆盖。
- `max_generation_loops` 默认 5，可由 CLI 或 Profile 配置为 1–20；CLI 显式值优先于 Profile。
- 首版不自动选择目标函数，不修改产品代码，不自动安装依赖。
- 首版不支持闭源二进制、多语言 fuzzer、业务逻辑 oracle、远程 SSH、长时集群 fuzz 或自动漏洞披露。
