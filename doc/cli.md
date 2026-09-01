# CLI 参考

## 通用约定

- 工作区根目录：`GOALOOP_WORKSPACE` 环境变量，或当前目录；相对 `--repo` 路径以此为根
- Profile 搜索路径：`<workspace>/profiles/` → `~/.config/goaloop/profiles/`
- 模型 Profile 搜索路径：`<workspace>/model-profiles/` → `~/.config/goaloop/model-profiles/`
- kRepo：默认 `<workspace>/tools/kRepo/main.py`；可用 `GOALOOP_KREPO` 指向 kRepo 根目录或新版入口脚本 `main.py`
- 所有 run 产物写入 `work/<project>/runs/<run-id>/`
- 原始模型会话：`.private-sessions/<run-id>/`（仅供恢复审计）

## 命令

### `goaloop run` — 完整四阶段流程

```bash
goaloop run --repo repos/<project> --source <dir-or-file> --function <symbol>
            [--language auto|c|cpp] [--profile default]
            [--model-profile default] [--max-generation-loops 5]
            [--fuzz-seconds 600] [--debug] [--workspace <path>]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--repo` | 必填 | 代码仓根目录，相对于 workspace 或使用绝对路径 |
| `--source` | 必填 | 被测函数所在目录或文件；相对路径以 `--repo` 为根，绝对路径也必须位于仓内。符号查找仅在此范围执行，用于区分同仓同名函数 |
| `--function` | 必填 | 目标函数符号（`[\\w:][\\w:$~.<>-]*`） |
| `--language` | `auto` | 自动检测 / `c` / `cpp` |
| `--profile` | `default` | 验证 Profile 名称 |
| `--model-profile` | `default` | 模型 Profile 名称 |
| `--max-generation-loops` | `5` | 1–20，模型生成/修订的最大轮数 |
| `--fuzz-seconds` | `600` | 1–86400，每个候选的 libFuzzer 执行时长 |
| `--max-context-kb` | `96` | 8–1024，注入每个生成提示词的源码上下文预算（KiB）。这是输入 token 的最大来源：降低它直接压缩每次模型输入（96 KiB ≈ 25–32K token） |
| `--max-input-tokens` | Profile 值 | 输入窗口守卫：生成前估算提示词 token，超过该值 90% 时快速失败并给出可操作报错，而不是等端点返回晦涩的“输入超限”错误。默认取模型 Profile 的 `max_input_tokens` |
| `--seed-corpus` | — | 可选：目录，其中的种子输入复制进 run corpus（跨 run 复用上一轮语料） |
| `--build-dir` | — | 可选：可信构建目录（含 `build.sh` 和 `src/`）。控制器把模型生成的 `harness.c` 覆盖复制为 `src/harness.cpp` 后运行脚本，并从输出识别可执行文件 |
| `--model-name` | Profile 值 | 覆盖模型 ID（如 `gpt-4o`、`deepseek-v4-pro`） |
| `--base-url` | Profile 值 | 覆盖模型端点（deepseek 适配器生效） |
| `--api-key` | 环境变量 | 覆盖模型凭据（注入 Profile 的 `api_key_env`，仅本次进程生效，不落盘） |
| `--output` | `<workspace>/work` | 产物根目录（run 目录 = `<output>/<project>/runs/<run-id>/`）。适合把产物放到外部磁盘/独立目录；`resume`/`status`/`report` 需传相同的 `--output` 定位 run |
| `--verbose` | `false` | 默认已实时打印 phase/step 进度；启用后额外输出每个事件的 payload 详情 |
| `--debug` | `false` | 实时输出经过过滤和聚合的 DSH/model 进度；隐藏 title/request/prompt 噪声，合并流式 chunk，保留模型、turn、step、工具和最终响应摘要 |
| `--workspace` | cwd | 工作区根目录 |

运行期间持续输出当前 `phase`、`step` 和 loop；模型调用、编译、fuzz、覆盖、crash
分析与报告写入等耗时步骤都会在开始和完成时分别输出。结束时输出 run-id、终态和产物路径。

### `goaloop resume` — 从断点续跑

```bash
goaloop resume --run-id <id> [--output <dir>] [--verbose] [--debug] [--workspace <path>]
```

从 `state.json`/`events.jsonl` 恢复状态，继续执行。已完成的证据不重复覆盖。
若该 run 使用了 `--output`，这里必须传相同的 `--output`。
`--debug` 与 `run` 行为相同，会实时显示恢复后发生的 DSH/model trace。

恢复时会对 run 目录获取独占锁；同一 run 已由另一个 `run`/`resume` 进程执行时，
命令立即失败并显示持锁 PID 和时间。`blocked`/`failed` 根据原失败阶段分别返回
preprocess、模型生成或候选执行；候选执行使用 `active_loop`/`loop_stage` 检查点，
已物化的候选不会再次创建。已耗尽生成预算以及报告阶段失败保持终态。

### `goaloop status` — 查看 run 状态

```bash
goaloop status --run-id <id> [--output <dir>] [--json] [--workspace <path>]
```

不带 `--json` 时打印摘要（run-id、project、phase、loop、status、产物路径）；带 `--json` 时打印完整 `RunState` JSON。

### `goaloop report` — 查看报告

```bash
goaloop report --run-id <id> [--output <dir>] [--format markdown|json] [--workspace <path>]
```

`--format` 默认 `markdown`（打印 `report.md`）；`json` 打印 `validation.json`。

### `goaloop doctor` — 环境自检

```bash
goaloop doctor [--profile default] [--model-profile default] [--workspace <path>]
```

逐项检查：Linux 平台、clang/clang++/llvm-profdata/llvm-cov、bubblewrap（仅
`sandbox.required` 时）、kRepo CLI、SDK 可导入、API key 已设置。全部 ok 时
`environment is ready`（exit 0），否则 exit 1。具体被测仓的
`.vscode/BROWSE.VC.DB` 在执行 preprocess 时检查。

### `goaloop evaluate` — 批量研究

```bash
goaloop evaluate <suite.json> [--repetitions 3] [--output <dir>] [--debug] [--workspace <path>]
```

suite.json 格式：

```json
{
  "entries": [
    {"repo": "repos/cJSON-1.7.17", "source": "cJSON.c", "function": "cJSON_Parse", "max_generation_loops": 3, "fuzz_seconds": 30}
  ]
}
```

对每个 entry 重复 `repetitions` 次，汇总终态分布以及 DSH trace 事件、模型调用耗时、
估算输入 token、响应规模、工具调用和格式重试，写入 `evaluate-results.json`。
每个 run 的自动优化建议会写入 `results[].optimization_suggestions`，并在顶层
`optimization` 按目标函数和建议 ID 汇总出现次数。
启用 `--debug` 时，每个运行同样实时输出 DSH/model trace。

### 自动优化建议

`run`、`resume` 和 `evaluate` 在任务进入终态并写完研究指标后，默认执行确定性分析，
无需额外开关，也不会再次调用大模型。Terminal 会输出建议数量、最高优先级和建议内容；
run 目录同时生成：

- `optimization-suggestions.json`：机器可读的信号、证据、优先级、建议与预期收益；
- `optimization-suggestions.md`：完整的人类可读分析，与任务验证报告独立存放；
- `report.md`：只保留任务执行、验证和崩溃分析结果，不包含工程优化建议。

当前规则关注输入范围、环境阻断、模型格式失败、首轮编译、重生成轮次、模型调用失败、
平均调用超过 60 秒、累计估算输入超过 100K token/单次超过 50K token，以及未闭合的
`tool/call`/`tool/result`；某阶段耗时至少 30 秒且占比达到 70% 时，也会指出主导阶段。
即使任务一次成功且没有异常信号，也会输出低优先级的重复运行与 A/B 基线建议。
`resume` 旧终态 run 时，如果缺少该产物，会自动补生成。

### 实时 DSH/model trace

`run`、`resume` 和 `evaluate` 的 `--debug` 会将 DSH 通知转换成面向用户的单行进度，
每条记录以 `[goaloop][debug][dsh]` 开头。Terminal 视图会：

- 隐藏 `session/title`、request header/context、完整 user prompt 和 inbox 事件；
- 不逐条打印 reasoning/text delta，而是每新增约 2 KiB 输出一次累计流式进度；
- 在 committed message 时输出 reasoning 长度和尾部摘要；
- 对 `GeneratedArtifactSet` 仅显示 loop、ready、文件数和 summary，不展开文件内容；
- 显示 turn/step、工具调用与结果、goal 变化、模型耗时和 kRepo 查询状态；
- 对未知事件输出最多 360 字符的紧凑摘要。

Terminal 输出仍会脱敏 API key、Bearer token、workspace 和绝对路径，只应在可信终端
中启用。需要逐事件查看时，应读取 run 目录中的原始 trace，而不是依赖 Terminal。
不开启时没有额外 Terminal 输出，但通知仍会被订阅和持久化：

- `<run-dir>/logs/dsh-trace.jsonl`：原始、未脱敏的 SDK notification 和 goaloop 遥测；
- `<run-dir>/logs/dsh-trace-summary.json`：事件、模型调用、耗时、输入/输出规模和工具调用摘要；
- `.private-sessions/<run-id>/`：DSH 自身的完整 session persistence。

原始 trace 可能包含源码、提示词、模型回答、绝对路径和凭据，应按敏感数据管理。
`resume` 会在同一 JSONL 后继续追加，并从原始记录重建摘要。详细字段和分析方法见
[observability.md](observability.md)。

## 使用示例

```bash
# 1. 环境检查
goaloop doctor --profile default

# 2. 对 cJSON 跑一次完整流程（默认无沙箱，30s fuzz，最多 3 轮模型生成）
goaloop run --repo repos/cJSON-1.7.17 --source cJSON.c --function cJSON_Parse --fuzz-seconds 30 --max-generation-loops 3

# 3. 查看状态
goaloop status --run-id 20260819T...  # 输出摘要

# 4. 查看报告
goaloop report --run-id 20260819T...

# 5. 中断后恢复
goaloop resume --run-id 20260819T...

# 6. 批量研究
goaloop evaluate suite.json --repetitions 3
```

## 模型 Profile（自定义模型）

`--model-profile` 选择模型配置（`model-profiles/*.toml`），不局限于 DeepSeek：

| 字段 | 默认 | 说明 |
|---|---|---|
| `provider` | `deepseek-official` | 适配器路由名。`deepseek-official` = dsh 内置 deepseek 适配器；pi-ai 适配器按 providers key（`openai`/`anthropic`/`deepseek`/... 或手写网关名） |
| `model` | `deepseek-v4-pro` | 模型 ID，由所选适配器路由解析 |
| `max_tokens` | — | 单次输出 token 上限（传给 SDK） |
| `max_input_tokens` | — | 模型输入窗口（token）。goaloop 每次生成前估算提示词，超过其 90% 快速失败。默认 profile 设为 `131071`；换更大窗口模型时调大或删除 |
| `cordis` | `cordis/goaloop.cordis.yml` | 使用的 Cordis 组合（deepseek 专用或 `goaloop.pi-ai.cordis.yml` 多 provider） |
| `base_url` | — | 自定义端点（仅 deepseek 适配器生效，SDK 转 `DEEPSEEK_BASE_URL`） |
| `api_key_env` | `DEEPSEEK_API_KEY` | 模型凭据所在环境变量（preprocess/doctor 按此检查） |
| `api_key` | — | 可选：直接写在 profile 的明文凭据（优先级：`--api-key` > `api_key` > `api_key_env` 环境变量） |

内置示例（见 [model-profiles/](../model-profiles/)）：

```bash
# DeepSeek 官方（默认）
goaloop run --repo ... --source ... --function ... --model-profile default

# 通过 pi-ai 使用 OpenAI（需 export OPENAI_API_KEY）
goaloop run --repo ... --source ... --function ... --model-profile pi-ai-openai

# 自定义 OpenAI 兼容网关（vLLM/Ollama，需 export CUSTOM_GATEWAY_API_KEY 等）
goaloop run --repo ... --source ... --function ... --model-profile pi-ai-custom
```

除 `export` 环境变量外，也可在命令行直接覆盖模型连接参数（优先级
CLI > Profile > 默认）：

```bash
goaloop run --repo ... --source ... --function ... \
    --model-profile default \
    --model-name gpt-4o \
    --base-url https://proxy.example/v1 \
    --api-key 'sk-...'
```

`--api-key` 仅注入本次进程的 `api_key_env`（不写入任何配置或日志）；注意
命令行参数可能在进程列表/Shell 历史中可见，敏感场景建议用环境变量。
`resume` 与 `evaluate`（manifest entry 可含 `model_name`/`base_url`/`api_key`
字段）同样支持这些覆盖。

除环境变量与 CLI 参数外，凭据也可直接写在 model-profile 的 toml 中：

```toml
# model-profiles/pi-ai-openai.toml
name = "pi-ai-openai"
provider = "openai"
model = "gpt-4o"
api_key_env = "OPENAI_API_KEY"
api_key = "sk-your-openai-key-here"   # 可选：明文凭据
```

> **安全警告**：明文 key 随文件存在泄漏风险（版本控制、文件分享）。`model-profiles/`
> 在仓库内，**不要提交含真实 key 的 profile**；建议用环境变量，或把含 key 的
> profile 放到 `~/.config/goaloop/model-profiles/`（用户级路径，不入库）。

### 模型空响应与格式错误

模型返回普通空内容时，goaloop 将 run 标记为 `blocked`；因输出 token 耗尽而为空
则标记为 `failed`。reason 会输出 `finish_reason`、`session_id` 以及 SDK 事件中
可提取的脱敏端点错误，不再将空内容误报为 `Expecting value: line 1 column 1`。
模型两次返回非 JSON 内容时，reason 会包含
首次与格式重试响应的字符数和最多 240 字符的脱敏预览，便于区分拒答、代理错误页
和普通格式偏差。完整 SDK 会话仍位于 `.private-sessions/<run-id>/`。

`cordis/goaloop.pi-ai.cordis.yml` 挂载 `@deepseek-ai/dsh-llm-pi-ai` 多 provider
适配器：内置 catalog 路由（deepseek/openai/anthropic/google/groq/mistral/
openrouter/xai）+ 手写 OpenAI 兼容网关。自定义网关端点/模型名可用
`CUSTOM_GATEWAY_BASE_URL` / `CUSTOM_GATEWAY_MODEL` 环境变量覆盖，无需改文件。

## 输入长度（token）优化

“模型输入超过 131071 token”的根因与对策：

- **每一轮生成都重新内嵌全部源码上下文**（`preprocess.json` 中的 `contexts`）。
  默认预算 96 KiB ≈ 25–32K token；旧默认 256 KiB ≈ 65–85K token。用
  `--max-context-kb` 调小即可直接压缩每次输入。
- **函数级上下文替代整文件和调用方文件**：

  1. kRepo `report --format json` 返回的目标函数原始实现片段，最多占预算的 1/2；
  2. `incoming_tree` 和 `outgoing_tree` 调用树，各最多占预算的 1/5；
  3. `param_constraints` 参数约束，最多占预算的 1/10；

  dependency 头文件、调用/引用文件和构建文件都不再进入 `preprocess.json`。
  调用关系由两棵去重调用树代替。
- **dependency 按需查询**：generation 模型需要宏、typedef、enum、变量、struct 或
  union 时，可返回临时 `krepo_query` 请求。控制器仅执行 kRepo `symbol`，仓库边界
  由 preprocess 绑定；每次最多 3 个、每轮最多 6 个、最多 3 个回合，单结果最多
  16 KiB。相同请求跨 generation loop/resume 命中持久化缓存，查询与结果记录在
  `<run-dir>/krepo-queries/queries.jsonl`，其中包含实际 `command`、`argv` 和 `cwd`，
  包括执行失败的命令。实际 `symbol` 命令固定附加 `--function <目标函数>`，不再传递
  `--max-candidates`、`--max-snippet-lines`；缓存位于其 `cache/`，`--debug` 同时显示命令。
- **kRepo 前置条件**：初始化 `tools/kRepo` 子模块，并用 VS Code C/C++ 扩展为
  被测仓生成 `.vscode/BROWSE.VC.DB`。goaloop 只执行只读 `report`/`symbol`，不调用
  `source`/`outgoingFuncs` 等写文件命令。preprocess 执行前 Terminal 会输出完整、
  可复制的 kRepo `report` 命令；缺少工具或数据库时 run 进入 `blocked`。
- **会话不再跨轮累积**：每一轮生成使用独立 session（`<run-id>-gNN`），
  提示词只出现一次；结构化 `latest_feedback` 携带两轮之间的差异。旧实现所有
  轮共用同一 session，第 N 轮的输入 ≈ N 份源码上下文 + 历史回复，第 2~3 轮
  必然超限。
- **反馈去重**：`GenerationGoal.latest_feedback` 不再随 goal JSON 重复内嵌
  （它以独立 `## Latest execution feedback` 块传入，每轮只出现一次）。
- **快速失败守卫**：`max_input_tokens`（模型 Profile 或 `--max-input-tokens`）
  打开后，驱动在调用端点前按 ~3 字符/token 估算提示词，超过窗口 90% 时抛出
  可操作的错误（提示 `--max-context-kb`），而不是等端点返回晦涩的超限错误。

```bash
# 源码很大时降低上下文预算（96 KiB → 64 KiB），每次模型输入约省 10K token
goaloop run --repo repos/<project> --source src/target.c --function <symbol> --max-context-kb 64

# 排查：把输入窗口守卫调低，快速触发并确认报错信息
goaloop run --repo repos/<project> --source src/target.c --function <symbol> --max-input-tokens 40000
```

## 构建目录模式（可选）

`--build-dir <dir>` 要求目录下存在可信的 `build.sh` 和 `src/`：

```bash
goaloop run --repo repos/<project> --source src/target.c --function <symbol> --build-dir repos/<project>
```

流程：

1. build-dir 专用生成契约只允许模型返回一个 `harness.c`，禁止 Makefile、脚本、
   stub 或额外源文件。
2. 控制器覆盖复制到 `<build-dir>/src/harness.cpp`；已存在的同名文件会被替换。生成内容
   必须可按 C++ 编译，libFuzzer 入口使用 `extern "C"`，C 目标声明同样保持 C linkage。
3. 控制器以 `<build-dir>` 为工作目录执行 `sh <build-dir>/build.sh`。
4. 控制器解析构建输出中的 `GOALOOP_FUZZER=<path>`、`executable:`、`binary:` 或
   编译命令 `-o <path>`，只接受实际存在且可执行的文件。框架不扫描文件系统猜测产物。
5. 使用识别出的可执行文件直接 fuzz 和采集覆盖率。

要点：

- `BuildPlan` 的 sources/includes/flags/libraries 在此模式必须全部为空，构建知识只在
  用户提供的 `build.sh` 中维护。
- **构建文件内容不再进入上下文**：`preprocess.json` 的 `contexts` 不含
  CMakeLists/Makefile/build.sh 内容，只通过 `PreprocessResult.build_dir` 暴露解析后的
  工程路径——构建知识完全由 `--build-dir` 工程承载，模型无需（也无法）猜测。
- `build.sh` 应自行添加 libFuzzer、ASan/UBSan 和 LLVM coverage 插桩参数，并在成功后
  输出 `GOALOOP_FUZZER=<path>`；构建标准输出完整保存到 run 的 `logs/`。
- 控制器额外提供 `GOALOOP_HARNESS`、`GOALOOP_RUN_DIR`、`GOALOOP_LOOP` 环境变量，分别
  表示安装后的 harness 路径、当前 run 目录和生成轮次。它们只是可选提示，不是脚本接口
  的硬性要求；现有 `build.sh` 不读取它们也能正常执行。
- 可执行文件可以位于其他构建输出目录；目录内普通日志 token 不会被当成外部产物，
  外部路径必须通过明确标记或 `-o` 输出声明。
- `build.sh` 是用户提供的可信脚本；**要求 `sandbox.required = false`**。

## 沙箱选项（可选）

默认 Profile 不启用沙箱（`sandbox.required = false`），libFuzzer harness 直接在本地
执行：仅施加墙钟超时 + RLIMIT_CPU（内存由 libFuzzer `-rss_limit_mb` 约束；ASan 需要大
虚拟地址空间，故不设 RLIMIT_AS）。

如需 bubblewrap 沙箱隔离（禁用网络、只读绑定源码、仅 run 目录可写、RSS/进程数/CPU
限制、超时回收进程树）：

```bash
# 1. 安装 bubblewrap（Linux），并确认内核允许非特权 user namespaces
sudo apt install bubblewrap

# 2. 用沙箱 Profile 运行（与 default 相同的工具链与覆盖策略，仅多了沙箱隔离）
goaloop doctor --profile sandboxed
goaloop run --repo repos/... --source ... --function ... --profile sandboxed
```

沙箱不可用（未装 bwrap）时，`sandboxed` Profile 会在预处理阶段判为 `blocked`，不会
静默降级。`profiles/sandboxed.toml` 与 `profiles/default.toml` 的唯一区别是
`sandbox.required = true`。

## 环境变量

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 凭据（默认必需） |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 等 | pi-ai 各 provider 路由的凭据（用对应 `--model-profile` 时必需） |
| `CUSTOM_GATEWAY_BASE_URL` / `CUSTOM_GATEWAY_MODEL` | 自定义 OpenAI 兼容网关的端点与模型名 |
| `DEEPSEEK_BASE_URL` | 自定义 API 端点（可选，默认官方端点） |
| `GOALOOP_WORKSPACE` | 工作区根目录（可选，默认 cwd） |
| `DSH_SESSION_ROOT` | 会话持久化根目录（由控制器自动设置，不用户设置） |
| `DSH_CORDIS_CONFIG` | Cordis 配置路径（由控制器自动设置，不用户设置） |
