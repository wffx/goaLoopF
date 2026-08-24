# CLI 参考

## 通用约定

- 工作区根目录：`GOALOOP_WORKSPACE` 环境变量，或当前目录（需包含 `repos/`）
- Profile 搜索路径：`<workspace>/profiles/` → `~/.config/goaloop/profiles/`
- 模型 Profile 搜索路径：`<workspace>/model-profiles/` → `~/.config/goaloop/model-profiles/`
- 所有 run 产物写入 `work/<project>/runs/<run-id>/`
- 原始模型会话：`.private-sessions/<run-id>/`（仅供恢复审计）

## 命令

### `goaloop run` — 完整四阶段流程

```bash
goaloop run --source repos/<project> --function <symbol>
            [--language auto|c|cpp] [--profile default]
            [--model-profile default] [--max-generation-loops 5]
            [--fuzz-seconds 600] [--workspace <path>]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--source` | 必填 | 源码目录，相对于 workspace 或绝对路径，必须在 `repos/` 下 |
| `--function` | 必填 | 目标函数符号（`[\\w:][\\w:$~.<>-]*`） |
| `--language` | `auto` | 自动检测 / `c` / `cpp` |
| `--profile` | `default` | 验证 Profile 名称 |
| `--model-profile` | `default` | 模型 Profile 名称 |
| `--max-generation-loops` | `5` | 1–20，模型生成/修订的最大轮数 |
| `--fuzz-seconds` | `600` | 1–86400，每个候选的 libFuzzer 执行时长 |
| `--seed-corpus` | — | 可选：目录，其中的种子输入复制进 run corpus（跨 run 复用上一轮语料） |
| `--build-dir` | — | 可选：CMake 工程目录（含 `CMakeLists.txt`）。控制器在该目录内构建并链接插桩静态库，模型不再猜构建参数 |
| `--model-name` | Profile 值 | 覆盖模型 ID（如 `gpt-4o`、`deepseek-v4-pro`） |
| `--base-url` | Profile 值 | 覆盖模型端点（deepseek 适配器生效） |
| `--api-key` | 环境变量 | 覆盖模型凭据（注入 Profile 的 `api_key_env`，仅本次进程生效，不落盘） |
| `--verbose` | `false` | 实时打印控制器进度事件（preprocess/compile/fuzz/decided 等） |
| `--workspace` | cwd | 工作区根目录 |

输出：run-id、终态、产物路径。

### `goaloop resume` — 从断点续跑

```bash
goaloop resume --run-id <id> [--verbose] [--workspace <path>]
```

从 `state.json`/`events.jsonl` 恢复状态，继续执行。已完成的证据不重复覆盖。

### `goaloop status` — 查看 run 状态

```bash
goaloop status --run-id <id> [--json] [--workspace <path>]
```

不带 `--json` 时打印摘要（run-id、project、phase、loop、status、产物路径）；带 `--json` 时打印完整 `RunState` JSON。

### `goaloop report` — 查看报告

```bash
goaloop report --run-id <id> [--format markdown|json] [--workspace <path>]
```

`--format` 默认 `markdown`（打印 `report.md`）；`json` 打印 `validation.json`。

### `goaloop doctor` — 环境自检

```bash
goaloop doctor [--profile default] [--model-profile default] [--workspace <path>]
```

逐项检查：Linux 平台、clang/clang++/llvm-profdata/llvm-cov、bubblewrap（仅 sandbox.required 时）、SDK 可导入、API key 已设置。全部 ok 时 `environment is ready`（exit 0），否则 exit 1。

### `goaloop evaluate` — 批量研究

```bash
goaloop evaluate <suite.json> [--repetitions 3] [--workspace <path>]
```

suite.json 格式：

```json
{
  "entries": [
    {"source": "repos/cJSON-1.7.17", "function": "cJSON_Parse", "max_generation_loops": 3, "fuzz_seconds": 30}
  ]
}
```

对每个 entry 重复 `repetitions` 次，汇总终态分布，写入 `evaluate-results.json`。

## 使用示例

```bash
# 1. 环境检查
goaloop doctor --profile default

# 2. 对 cJSON 跑一次完整流程（默认无沙箱，30s fuzz，最多 3 轮模型生成）
goaloop run --source repos/cJSON-1.7.17 --function cJSON_Parse --fuzz-seconds 30 --max-generation-loops 3

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
| `cordis` | `cordis/goaloop.cordis.yml` | 使用的 Cordis 组合（deepseek 专用或 `goaloop.pi-ai.cordis.yml` 多 provider） |
| `base_url` | — | 自定义端点（仅 deepseek 适配器生效，SDK 转 `DEEPSEEK_BASE_URL`） |
| `api_key_env` | `DEEPSEEK_API_KEY` | 模型凭据所在环境变量（preprocess/doctor 按此检查） |

内置示例（见 [model-profiles/](../model-profiles/)）：

```bash
# DeepSeek 官方（默认）
goaloop run --source ... --function ... --model-profile default

# 通过 pi-ai 使用 OpenAI（需 export OPENAI_API_KEY）
goaloop run --source ... --function ... --model-profile pi-ai-openai

# 自定义 OpenAI 兼容网关（vLLM/Ollama，需 export CUSTOM_GATEWAY_API_KEY 等）
goaloop run --source ... --function ... --model-profile pi-ai-custom
```

除 `export` 环境变量外，也可在命令行直接覆盖模型连接参数（优先级
CLI > Profile > 默认）：

```bash
goaloop run --source ... --function ... \
    --model-profile default \
    --model-name gpt-4o \
    --base-url https://proxy.example/v1 \
    --api-key 'sk-...'
```

`--api-key` 仅注入本次进程的 `api_key_env`（不写入任何配置或日志）；注意
命令行参数可能在进程列表/Shell 历史中可见，敏感场景建议用环境变量。
`resume` 与 `evaluate`（manifest entry 可含 `model_name`/`base_url`/`api_key`
字段）同样支持这些覆盖。

`cordis/goaloop.pi-ai.cordis.yml` 挂载 `@deepseek-ai/dsh-llm-pi-ai` 多 provider
适配器：内置 catalog 路由（deepseek/openai/anthropic/google/groq/mistral/
openrouter/xai）+ 手写 OpenAI 兼容网关。自定义网关端点/模型名可用
`CUSTOM_GATEWAY_BASE_URL` / `CUSTOM_GATEWAY_MODEL` 环境变量覆盖，无需改文件。

## CMake 构建目录模式（可选）

用户提供现成 CMake 工程时，`--build-dir <dir>`（目录下固定存放
`CMakeLists.txt`）让控制器在该目录内完成构建：

```bash
goaloop run --source repos/<project> --function <symbol> --build-dir repos/<project>
```

流程（均在 `<build-dir>` 内完成，out-of-source）：

1. `cmake -S <build-dir> -B <build-dir>/goaloop-build -DCMAKE_C_COMPILER=clang
   -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_C_FLAGS="-fsanitize=address,undefined
   -fprofile-instr-generate -fcoverage-mapping"`（配置，插桩保证覆盖归因）
2. `cmake --build <build-dir>/goaloop-build`
3. harness 编译链接产物：`clang -fsanitize=fuzzer,address,undefined
   <harness.c> -I<build-dir> <build-dir>/goaloop-build/libxxx.a -o fuzzer`

要点：

- 模型生成的 `BuildPlan.target_sources` 在构建模式下被忽略——产品源码来自库，
  模型只写 harness，显著降低 token 消耗。
- 库产物查找：`profiles/*.toml` 的 `[build] library`（相对
  `goaloop-build`）优先，否则自动取第一个 `*.a`（多库工程请显式声明）；
  `[build] include_dirs`（相对 build-dir）与 `[build] flags` 可附加。
- `CMakeLists.txt` 不被修改；构建目录内的 `goaloop-build/` 为控制器构建输出。
- 需要系统已装 `cmake`；**要求 `sandbox.required = false`**（cmake 需写构建目录）。

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
goaloop run --source repos/... --function ... --profile sandboxed
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