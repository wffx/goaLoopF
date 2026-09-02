# goaloop kRepo query Tool

该 Cordis 插件注册 DSH 原生只读 Tool `query_krepo_symbol`。模型参数包含：

- `symbol`：必填的非函数 C/C++ 标识符；
- `repo`：必填的代码仓路径；
- `function`：必填的目标函数；
- `file`：必填的目标函数实现文件；
- `kind`：可选的符号类型。

插件从 `GOALOOP_KREPO_BINDINGS_DIR` 按当前 DSH session id 读取 Controller 生成的绑定，
并验证模型传入的 repo、目标函数和目标文件与绑定完全一致，因此不能跨目标查询。
Python 和审计目录不进入 Tool Schema。插件以无 shell
`execFile` 调用固定的 `goaloop.krepo_tool_bridge` module，并对白名单环境、输出大小、
超时和取消进行约束。Python bridge 复用 `KRepoQueryService` 的参数校验、缓存、JSONL
审计。每个 session 不限制查询次数，单次调用仍受超时和输出大小限制。

默认 SDK Cordis 组合通过相对路径加载该插件：

```yaml
- id: tool-krepo-query
  name: '../dsh-plugins/krepo-query/index.mjs'
```

Web Profile 后续接入时也应加载同一插件，并由 `goaloop_start` 一类 Controller Tool
为 Web session 创建目标绑定，并把 repo、function、file 的精确值提供给模型调用。
