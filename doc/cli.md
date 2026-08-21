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

## 沙箱选项

默认 Profile 不启用沙箱（`sandbox.required = false`）。如需沙箱隔离（禁用网络、只读绑定源码、仅 run 目录可写）：

```bash
# 需要先安装 bubblewrap，然后：
goaloop doctor --profile sandboxed
goaloop run --source repos/... --function ... --profile sandboxed
```

## 环境变量

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API 凭据（必需） |
| `DEEPSEEK_BASE_URL` | 自定义 API 端点（可选，默认官方端点） |
| `GOALOOP_WORKSPACE` | 工作区根目录（可选，默认 cwd） |
| `DSH_SESSION_ROOT` | 会话持久化根目录（由控制器自动设置，不用户设置） |
| `DSH_CORDIS_CONFIG` | Cordis 配置路径（由控制器自动设置，不用户设置） |