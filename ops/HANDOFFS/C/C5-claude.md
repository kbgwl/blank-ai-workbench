# C5 开发交接单

## 任务与范围

- 已实现：
  - **上下文账本把工具调用参数算进去**：`_chars()` / `message_tokens()` 现在包含
    `tool_calls[].function.arguments`。此前只数 `content`，write_file 的正文和
    apply_patch 的补丁完全不在水位里——压缩会以为上下文很小。
  - **旧工具调用的大参数折叠**：`_microcompact` 对最近 8 条之外的
    `write_file / create_file / apply_patch / edit_file / notebook_edit / repl`
    参数做有界折叠：正文/补丁超过 1200 字符时，换成一句“已折叠，需要时用 read_file 查看”。
    不删除消息，`tool_call_id` 配对不变；最近窗口原样保留。
  - `MicroStats` 增加 `args_shrunk` 计数，`context.measured` 事件可解释省了什么。
- 明确未实现：
  - 没有做“当前文件状态快照”那套更大改造；本次只折叠旧调用参数。
  - 没有改工具表/系统提示预算；那属于产品决定。
  - 未跑全量后端测试。

## 修改内容

- `services/api/src/blank_api/services/agent/context.py`：
  - `_chars` / `message_tokens` 计入 tool_calls；
  - 新增 `MICRO_ARG_CHARS`、`MICRO_ARG_FIELDS`、`_shrink_tool_call_args`；
  - `_microcompact` 对旧 assistant 工具调用折参数；
  - `MicroStats.args_shrunk`。
- `services/api/tests/test_context_layers_v12.py`：新增 5 条判据（参数计 token、
  旧写入折叠、最近窗口保留、edit_file 折叠、小参数不动）。
- `docs/capability-baseline-2026-11-21.md`：记录当前能力与成本基线。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest test_context_layers_v12.py test_context.py test_context_compactor.py test_context_pairing_v14.py test_context_watermark_v13.py test_compaction_window_v23.py -q` | `48 passed` | 含新增 5 条 |
| `pytest test_context_overflow_recovery_v403.py test_idle_compaction_v427.py test_context_restart_v427.py test_compact_continue_note_v427.py test_token_trimming.py test_context_layers_v12.py -q` | `44 passed` | 溢出、空闲压缩、裁剪回归 |

## 风险与需要审查的点

- 折叠只动**发给模型的旧 assistant 消息副本**，不改事件、证据或磁盘文件；
  模型需要原文时仍可 `read_file`。
- 最近 8 条消息不折；`tool_call_id` 与工具结果配对不变，OpenAI/DeepSeek 的
  400 形状校验不会命中。
- 参数 JSON 解析失败/不是 dict/不是列表时原样保留，宁多占字符不制造坏调用。
- 水位现在包含 tool_calls，可能比旧版更早触发压缩；这是修正低估，不是收紧预算。

## 建议人工验收

1. 跑一个写大文件后继续多步的任务，确认 `context.measured.micro.args_shrunk > 0`，
   且旧调用参数变成折叠说明、最近调用保持完整。
2. 确认任务仍能正常完成，模型在需要时能 `read_file` 取回内容。
3. 对比同一任务的 prompt tokens 与旧版（预计累计 prompt 明显下降）。
