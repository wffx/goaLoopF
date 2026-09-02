# 待办项

## 将 kRepo 查询迁移为 DSH 原生 Tool

> 状态：已实施<br>
> 优先级：高<br>
> 当前实现：DSH 原生 `query_krepo_symbol` Tool

### 背景

旧 generation 阶段通过提示词告知模型返回 `krepo_query` JSON，再由 Python Driver
识别并执行 kRepo。该方式依赖模型在多轮会话中持续遵守文本协议，不能利用 DSH 的
原生 Tool Schema、参数校验和标准 `tool_call`/`tool_result` 生命周期，现已被原生
`query_krepo_symbol` Tool 替代。

### 推荐方案

实现一个本地 Cordis 插件，通过 `ctx.tools.register(...)` 注册只读工具
`query_krepo_symbol`。DSH 在每个模型步骤中自动提供工具 Schema，模型只需决定是否
查询以及查询哪个符号，不再生成或记忆自定义交互格式。

工具参数：

- `symbol`：必填，非函数符号名；
- `repo`：必填，代码仓路径；
- `function`：必填，目标函数；
- `file`：必填，目标函数实现文件；
- `kind`：可选，限定 macro、typedef、enum、variable、struct、union 等类型；
- repo、function、file 由 prompt 提供，并由 controller session 绑定复核；
- 不提供命令、Python、审计目录等执行参数。

### 安全边界

- 仅调用 kRepo `symbol`，不开放 Bash、任意文件读取、网络或写文件命令；
- 复用 `KRepoQueryService` 的符号/kind/path 白名单，并固定注入目标实现文件；
- 每个 session 不限制查询次数；
- 保留 16 KiB 单结果截断、120 秒超时、持久化缓存和 JSONL 审计；
- kRepo 输出继续按不可信仓库数据处理，不能成为模型指令；
- repo 根目录由 `PreprocessResult.source_root` 绑定，模型不能跨仓查询。

### 实施步骤

1. 已新增 `dsh-plugins/krepo-query/index.mjs` 并加入 `cordis/goaloop*.cordis.yml`；
2. 已新增 `goaloop.krepo_tool_bridge` 到 Python `KRepoQueryService` 的窄接口；
3. 已将 run、repo、目标函数和目标文件绑定到 generation session；
4. 已通过标准 `tool/call`/`tool/result` 接入 `--debug` DSH trace；
5. 已删除 Driver 中对 `krepo_query` 文本响应的解析和二次提示逻辑；
6. 已精简 generation prompt，仅说明何时使用 `query_krepo_symbol`；
7. 已更新单元测试、文档和 prompt version。

### 验收标准

- DSH 每轮都能从原生 Tool Schema 获得稳定的工具名称和参数定义；
- trace 中出现标准工具调用和工具结果事件；
- 模型无需输出 `type: "krepo_query"` 即可查询 dependency；
- 缺失必填参数、非法 symbol/kind、无效目标实现文件和越界 repo 调用均被拒绝；
- 相同查询跨 generation loop 和 resume 命中既有缓存；
- `preprocess.json` 仍只包含目标函数、调用树和参数约束；
- 移除旧文本协议后，现有 generation、格式重试和 resume 测试全部通过。

### 备选方案

若本地 Cordis 插件无法稳定桥接 Python，可将 `KRepoQueryService` 暴露为本地 stdio
MCP Server，并由 DSH MCP Client 注册为原生工具。MCP 方案隔离更清晰，但会增加协议、
进程生命周期和依赖管理成本，因此作为第二选择。
