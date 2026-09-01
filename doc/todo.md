# 待办项

## 将 kRepo 查询迁移为 DSH 原生 Tool

> 状态：待实施<br>
> 优先级：高<br>
> 当前替代实现：generation prompt 中的 `krepo_query` JSON 协议

### 背景

当前 generation 阶段通过提示词告知模型返回 `krepo_query` JSON，再由 Python Driver
识别并执行 kRepo。该方式依赖模型在多轮会话中持续遵守文本协议，不能利用 DSH 的
原生 Tool Schema、参数校验和标准 `tool_call`/`tool_result` 生命周期。

### 推荐方案

实现一个本地 Cordis 插件，通过 `ctx.tools.register(defineTool(...))` 注册只读工具
`query_krepo_symbol`。DSH 在每个模型步骤中自动提供工具 Schema，模型只需决定是否
查询以及查询哪个符号，不再生成或记忆自定义交互格式。

工具参数：

- `symbol`：必填，非函数符号名；
- `kind`：可选，限定 macro、typedef、enum、variable、struct、union 等类型；
- `file`：不向模型开放，由 controller 固定绑定目标函数实现文件；
- 不提供 `repo`、命令、输出路径等参数，仓库和 run 目录必须由控制器绑定。

### 安全边界

- 仅调用 kRepo `symbol`，不开放 Bash、任意文件读取、网络或写文件命令；
- 复用 `KRepoQueryService` 的符号/kind/path 白名单，并固定注入目标实现文件；
- 保留每轮最多 3 个回合、合计 6 次查询的控制器预算；
- 保留 16 KiB 单结果截断、120 秒超时、持久化缓存和 JSONL 审计；
- kRepo 输出继续按不可信仓库数据处理，不能成为模型指令；
- repo 根目录由 `PreprocessResult.source_root` 绑定，模型不能跨仓查询。

### 实施步骤

1. 新增本地 Cordis Tool 插件并加入 `cordis/goaloop*.cordis.yml`；
2. 建立 Tool executor 到 Python `KRepoQueryService` 的窄接口；
3. 将 run 目录、repo 根目录和查询预算绑定到当前 generation session；
4. 将查询调用、结果和错误接入现有 `--debug` DSH trace；
5. 删除 Driver 中对 `krepo_query` 文本响应的解析和二次提示逻辑；
6. 精简 generation prompt，仅说明何时使用 `query_krepo_symbol`；
7. 更新单元测试、resume 测试、文档和 prompt version。

### 验收标准

- DSH 每轮都能从原生 Tool Schema 获得稳定的工具名称和参数定义；
- trace 中出现标准工具调用和工具结果事件；
- 模型无需输出 `type: "krepo_query"` 即可查询 dependency；
- 非法 symbol/kind、无效目标实现文件、越界 repo 和超预算调用均被拒绝；
- 相同查询跨 generation loop 和 resume 命中既有缓存；
- `preprocess.json` 仍只包含目标函数、调用树和参数约束；
- 移除旧文本协议后，现有 generation、格式重试和 resume 测试全部通过。

### 备选方案

若本地 Cordis 插件无法稳定桥接 Python，可将 `KRepoQueryService` 暴露为本地 stdio
MCP Server，并由 DSH MCP Client 注册为原生工具。MCP 方案隔离更清晰，但会增加协议、
进程生命周期和依赖管理成本，因此作为第二选择。
