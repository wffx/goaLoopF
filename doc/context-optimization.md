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

## 查询协议

模型需要非函数依赖时，在最终 `GeneratedArtifactSet` 前返回：

```json
{
  "type": "krepo_query",
  "reason": "need the packet layout",
  "queries": [
    {
      "operation": "symbol",
      "symbol": "packet_t",
      "kind": "typedef",
      "file": "include/packet.h"
    }
  ]
}
```

`kind` 和 `file` 可省略。控制器执行 kRepo `symbol` 后，在同一个 DSH session 中回填
结果；模型可以继续查询或返回最终工件。查询结果属于不可信仓库数据，不能作为指令。

## 安全与预算

- 仓库目录由 `PreprocessResult.source_root` 绑定，模型不能指定其他 repo；
- 仅开放非函数 `symbol` 查询，不开放 Bash、任意文件读取、网络或 kRepo 写文件命令；
- 符号名、kind 和相对 file filter 均做白名单校验；
- 每个请求最多 3 个查询，每轮 generation 最多 3 个查询回合、合计 6 个查询；
- 单个查询输出最多 16 KiB，子进程超时 120 秒；
- 每个 generation loop 仍使用独立 session，查询仅在当前 loop 的 session 内回填。

## 缓存、审计与 resume

查询状态位于 `<run-dir>/krepo-queries/`：

- `queries.jsonl`：追加记录查询参数、结果、时间和是否命中缓存；
- `cache/<sha256>.json`：以标准化查询参数为键的结果缓存。

相同查询在后续 generation loop 或 `resume` 后直接复用缓存，避免重复运行 kRepo。
开启 CLI `--debug` 时，Terminal 会显示 `goaloop.krepo_query.started`、具体命令和
完成事件；普通模式不输出可能较长的 dependency 内容。
