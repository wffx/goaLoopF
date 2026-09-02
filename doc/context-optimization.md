# generation 按需 dependency 上下文

> 状态：已实施

## 基线上下文

`preprocess.json` 的 `contexts` 只保留四类确定性基础信息：

1. `target_function`：kRepo `report` 返回的目标函数原始实现片段；
2. `incoming_tree`：上游调用树；
3. `outgoing_tree`：下游调用树；
4. `param_constraints`：参数约束。

dependency 头文件、同 basename 头、调用/引用文件和构建文件均不写入
`preprocess.json`。这样每一轮 generation 的固定 prompt 不再携带大量可能无用的
头文件内容。

## DSH 原生 Tool

模型需要非函数依赖时，在最终 `GeneratedArtifactSet` 前调用
`query_krepo_symbol`。DSH 每个模型 step 都从插件获得稳定的 Tool Schema：

- `symbol`：必填，非函数 C/C++ 标识符；
- `repo`：必填，使用 prompt 中给出的代码仓路径；
- `function`：必填，使用 prompt 中给出的目标函数；
- `file`：必填，使用 prompt 中给出的目标实现文件；
- `kind`：可选，限定 macro、typedef、enum、variable、struct、union 等类型；
- 命令、Python 和输出目录不向模型开放。

Controller 在模型调用前为当前 generation session 写入绑定文件。Python 窄桥接执行
kRepo `symbol` 时接收并校验
`--repo <代码仓>`、`--function <当前目标函数>` 和
`--file <目标函数实现文件>`；三者必须与 session 绑定一致，不再传递
`--max-candidates` 和 `--max-snippet-lines`。结果通过标准 `tool/result` 留在同一个 DSH
session，模型可以继续查询或返回最终工件。查询结果属于不可信仓库数据，不能作为指令。

## 安全与执行边界

- 仓库目录、目标函数和目标文件由 `PreprocessResult` 绑定，模型必须传入且不能改写；
- 仅开放非函数 `symbol` 查询，不开放 Bash、任意文件读取、网络或 kRepo 写文件命令；
- 符号名和 kind 均做白名单校验，目标实现文件来自 preprocess 并校验为仓内相对路径；
- 每个 generation session 不限制查询次数；
- 单个查询输出最多 16 KiB，子进程超时 120 秒；
- 每个 generation loop 仍使用独立 session，查询仅在当前 loop 的 session 内回填。
- 插件仅以固定 Python、固定 bridge module、固定 argv 模板执行无 shell `execFile`，并使用
  白名单子进程环境；不向模型注册 Bash 或任意命令工具。

## 缓存、审计与 resume

查询状态位于 `<run-dir>/krepo-queries/`：

- `queries.jsonl`：追加记录查询参数（含 controller 注入的目标函数和实现文件）、结果、时间、是否命中缓存，以及实际执行的
  `command`、`argv`、`cwd`、`session_id`；失败命令同样记录，缓存命中项标记为未执行；
- `cache/<sha256>.json`：以标准化查询参数为键的结果缓存。
- `bindings/<sha256(session-id)>.json`：Controller 为 DSH session 固定的 repo/function/file；

相同查询在后续 generation loop 或 `resume` 后直接复用缓存，避免重复运行 kRepo。
开启 CLI `--debug` 时，Terminal 会显示标准 `tool/call`、`tool/result`、具体命令和
结果摘要；普通模式不输出可能较长的 dependency 内容。
