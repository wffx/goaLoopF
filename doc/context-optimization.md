# 按需上下文（On-Demand Context）优化设计

> 状态：设计草案（尚未实施）
> 关联：[cli.md 输入长度优化](cli.md)、[architecture.md](architecture.md)、[contracts.md](contracts.md)

## 1. 背景与目标

### 现状问题

对于调用链复杂的被测对象，`preprocess.json` 仍然臃肿：

- 依赖闭包按**整文件**进入上下文（每文件 32 KiB），定义文件按符号窗口进入（64 KiB）；
- 目标函数 → 子函数 → 孙函数的传递闭包让**文件数量**膨胀，即便每个文件有上限，总量仍可能逼近甚至超过模型输入窗口；
- 模型真正需要的往往只是**签名、类型、宏**这些"关键信息"，而不是依赖文件的完整实现体。

### 目标

1. **preprocess 只保留关键信息**：依赖文件降级为"声明提取"（函数原型、typedef、struct/union/enum、宏），深层闭包默认不再进入；
2. **模型通过 goal 驱动自主补充**：模型在工件中声明它需要看什么，控制器从源码中**按需提取关键片段**注入后续轮次；
3. 保持现有安全边界（模型无文件/命令/网络权限）、严格 JSON 契约、断点恢复与确定性测试。

## 2. 约束与前提

- **模型不能读文件**：prompt 明确禁止 read/write/exec/network/delegate。因此"模型自己补充"= 模型**声明需求** → 控制器**提取并注入**的闭环，而非模型直接访问源码。
- **每轮生成使用独立 session**（`<run-id>-gNN`，已有）：模型不跨轮记忆，跨轮状态（含按需上下文池）由**控制器持有**。
- **严格 JSON + 单次格式重试**（已有）：契约扩展必须向后兼容；模型不配合时功能自动降级，不影响主流程。
- **确定性**：提取规则必须确定、可测试；`resume` 必须能恢复按需上下文池。

## 3. 总体流程（闭环）

```
preprocess（精炼：定义文件符号窗口 + 一级依赖声明提取）
        │
        ▼
生成轮 N：prompt = 契约 + run 上下文 + 精炼 PreprocessResult + goal
        + 最新 feedback + 按需上下文池（## Requested context）
        │
        ▼
模型响应 GeneratedArtifactSet（可含 context_requests: [...]）
        │
        ▼
控制器：校验工件 → 提取 context_requests 为片段 → 写入池
        → 编译 + fuzz + 覆盖
        │
        ▼
决策：ACCEPTED → 结束
     NEEDS_REGENERATION → 生成轮 N+1（feedback + 池注入）
     CRASH_CANDIDATE → crash 分析
```

## 4. 变更 1：preprocess 精炼（声明提取）

### 4.1 分层选择（已有，保持不变）

定义文件（64 KiB/符号窗口）→ 一级依赖闭包 + 同 basename 头（32 KiB）→ 构建文件（16 KiB）→ 引用文件（16 KiB，最后）。

### 4.2 依赖文件内容策略：整文件 → 声明提取

**适用对象**：闭包文件、同 basename 头文件、引用文件（T2/T4 层）。

**提取规则（best-effort 行级，确定性）**：

| 类别 | 规则 |
|---|---|
| 函数原型 | 行内 `(`/`)` 数量平衡、以 `;` 结尾、不以控制流关键字（if/for/while/switch/return/else）开头 |
| typedef | 行首 `typedef`，持续到行内括号平衡或 `;`/`{` |
| struct/union/enum | 行首 `struct/union/enum`，持续到行内括号平衡或 `;`/`{`/`}` |
| 宏 | 行首 `#define`、`#include`（记录 include 关系本身也有价值） |
| 多行声明 | 以开括号/开结构体行起，逐行合并直到括号平衡且以 `;` 或 `}` 结尾，单条上限 8 KiB |

**边界处理**：
- 注释/字符串中的假阳性：跳过以 `/*`、`*`、`//` 开头的行；字符串内的括号不特殊处理（best-effort，靠按需补充兜底）；
- 提取结果超出每文件上限（32 KiB）→ 按行截断，`truncated=true`；
- 提取结果为空（纯实现文件）→ 该文件跳过，不产生空 context 项。

**元数据**：`SourceContext` 新增 `mode: "full" | "declarations"`（定义文件 = full，其余 = declarations），用于报告与审计。

### 4.3 配置与回退

- `FuzzRunRequest` 新增 `context_mode: Literal["smart", "full"] = "smart"`（CLI `--context-mode`）；
  - `smart`（默认）：上述声明提取；
  - `full`：旧行为（整文件前 32 KiB），作为声明提取质量不佳时的回退开关。
- 注意：`full` 模式不会自动关闭按需补充（第 5、6 节），两者正交。

## 5. 变更 2：契约扩展（context_requests）

### 5.1 字段定义

`GeneratedArtifactSet` 新增**可选**字段：

```json
"context_requests": [
  {"kind": "symbol", "name": "cJSON_AddItemToObject", "reason": "harness needs its signature"},
  {"kind": "file",   "path": "src/dep.c",            "reason": "need the struct layout"}
]
```

校验规则（Pydantic validator + 控制器）：
- `kind` ∈ `{"symbol", "file"}`；
- `symbol.name`：`[A-Za-z_]\w*`，长度 ≤ 128；
- `file.path`：相对路径、`/` 分隔、无 `..`、无 `.` 段（复用 `_validate_relative_path`）；
- 每轮 `context_requests` 数量 ≤ 8；`reason` ≤ 200 字符（仅审计用，不注入源码）；
- 字段缺失 → 默认 `[]`（旧模型/旧 payload 完全兼容）。

### 5.2 语义

- `symbol`：请求某个标识符的**定义/声明片段**（函数签名 + 函数体，或类型/宏定义）；
- `file`：请求某个文件的**声明提取**（复用 4.2，而非整文件——如需整文件，由用户通过 `--context-mode full` 控制）。

### 5.3 防滥用

- 数量/长度上限如上；
- 池总量上限（见 6.3），超限控制器裁决，不阻塞流程；
- 模型请求已提供过的符号 → 去重，不重复注入。

## 6. 变更 3：按需上下文提取器与池

新增 `src/goaloop/context.py`（或并入 preprocess.py，视实现规模而定）。

### 6.1 符号提取（`extract_symbol_fragment(root, files, symbol) -> str | None`）

1. 候选文件：定义文件 ∪ 一级闭包 ∪ 同 basename 头（即当前 context 的 T1/T2 层，复用选择逻辑）；
2. 定位 `\bsymbol\b` 首次出现；
3. 若是函数（`symbol(...)`）：
   - 从签名行起，扫描平衡花括号取函数体（简化：最多 200 行 / 8 KiB）；
   - 仅取签名（无 `{`）时返回声明行；
4. 若是类型/宏（行首 typedef/struct/union/enum/#define）：
   - 定义行 + 后续连续行（直到空行或超过 4 KiB）；
5. 找不到 → 返回 `None`（控制器记事件，不阻塞）。

### 6.2 文件提取

复用 4.2 的声明提取，输出同一 `SourceContext.mode="declarations"` 的内容。

### 6.3 池管理（控制器持有）

- 结构：`requested_context: dict[str, str]`，键 = `"symbol:<name>" | "file:<path>"`，值 = 提取片段（含来源路径头注释）；
- 跨轮累积：轮 N 请求的片段，轮 N+1 起每轮注入，直到 run 结束；
- 总上限：`max(16 KiB, max_context_kb // 3)`，默认 32 KiB；
- 超限：按**轮次新优先**淘汰最旧条目（FIFO），记 `context:pool_overflow` 事件；
- 持久化：随 `RunState`（或 `GenerationGoal`）保存，`resume` 恢复；
- 注入位置：`build_generation_prompt` 追加 `## Requested context` 块（位于 feedback 之后）。

## 7. 变更 4：生成流程接线

### 7.1 driver.py

- `build_generation_prompt(..., requested_context: dict[str, str] | None = None)`；
- `generate_artifacts` 返回后校验 `context_requests`（失败 → 忽略 + 事件，不触发格式重试——它不是"响应无效"，只是需求清单）；
- `DeepSeekHarnessDriver` 通过 controller 传入/取回池（或池由 controller 持有，driver 只透传）。

### 7.2 控制器（workflow/generation.py）

- 执行决策为 `NEEDS_REGENERATION` 时：提取 `artifacts.context_requests` → 写入池 → 下一轮注入；
- 事件：`context:requested`（模型请求清单）、`context:extracted`（每个片段：符号/文件、来源、字节数）、`context:pool_overflow`、`context:not_found`（符号找不到）；
- 报告（report.py）：统计请求数、提取片段数、池最终大小。

### 7.3 与格式重试的关系

`context_requests` 提取失败（如 path 非法）只记录事件并忽略该项；**不**计入格式重试（格式重试只针对 JSON/契约级错误）。

## 8. 回退策略

| 场景 | 行为 |
|---|---|
| 模型不输出 `context_requests` | 字段缺省 `[]`，功能静默降级为"纯精炼 preprocess"，流程不受影响 |
| 声明提取质量差导致编译反复失败 | 用户 `--context-mode full` 回退旧整文件行为；或模型改用 `file` 请求精确补片段 |
| 符号/文件提取不到 | `context:not_found` 事件，跳过该项，不阻塞 |
| 池超限 | FIFO 淘汰 + 事件，不阻塞 |
| resume 旧 run | 旧 state 无池字段 → 空池，行为不变 |
| 契约版本 | 新增可选字段不影响 `schema_version`（"1.0" 保持）；`ARTIFACT_SCHEMA_HINT` 同步更新 |

## 9. 兼容性与迁移

- **向后兼容**：`context_requests` 可选；`context_mode` 有默认值；旧 `state.json`/旧 payload 全部可加载；
- **token 影响评估**（实施后需实测）：
  - 每轮：依赖文件从整文件 → 声明（普通头约省 40–60%，宏密集头约省 20–30%）；
  - 成本：池 ≤ 32 KiB ≈ 10K token/轮；第一轮可能多迭代 1–2 轮；
  - 净效果：调用链复杂时总 token 通常下降；简单目标可能持平或略升（需 benchmark 验证，纳入 `evaluate` 对比）。

## 10. 测试计划

| 模块 | 用例 |
|---|---|
| 声明提取 | 单行/多行函数原型、typedef、struct/union/enum、宏、注释/字符串假阳性、括号跨行、超限截断、空结果跳过 |
| 契约 | `context_requests` 校验：非法 kind、非法 symbol 名、非法 path（`..`/绝对路径）、超量（>8）、缺省为空 |
| 符号提取 | 函数定义（花括号平衡）、仅声明、类型/宏、多候选文件、找不到返回 None、大小上限 |
| 池 | 跨轮累积、去重、FIFO 超限淘汰、resume 恢复 |
| 闭环 | ScriptedGenerationDriver 端到端：模型请求 → 提取 → 下一轮 prompt 含 `## Requested context` |
| 回退 | 无 `context_requests` 字段的 payload 正常通过；`--context-mode full` 恢复旧行为 |

## 11. 风险与开放问题

1. **C++ 精度**：模板、重载、命名空间的声明提取粗糙 → 依赖 `file` 请求 + `full` 模式兜底；symbol 请求对 C++ 符号（含 `::`）需扩展白名单；
2. **第一轮成功率**：精炼后首轮信息更少，可能多迭代；需实测 `max_generation_loops` 默认 5 是否足够（不够则提示用户调大）；
3. **模型请求质量**：模型可能请求无意义符号 → reason 字段辅助审计 + 上限约束；不阻塞主流程；
4. **池的语义**：池跨轮注入近似"模型记忆"；若模型请求量接近上限，是否支持"轮 N 的请求仅注入轮 N+1 一次"（不累积）？默认累积，留作后续可配置项；
5. **是否允许 `file` 请求整文件**：默认 `file` 也走声明提取；如确需整文件（如纯数据/查表文件），可后续增加 `"whole": true`（上限 16 KiB，显式审计）。

## 12. 实施里程碑

- **M1** preprocess 声明提取 + `context_mode` 配置 + 单元测试
- **M2** 契约 `context_requests` + 校验 + `ARTIFACT_SCHEMA_HINT` 更新
- **M3** 提取器（context.py）+ 池 + prompt 注入 + 事件
- **M4** 池持久化/resume + 报告指标 + 端到端闭环测试
- **M5** 文档 + 复杂工程实测对比（preprocess 大小、总 token、迭代轮数）
