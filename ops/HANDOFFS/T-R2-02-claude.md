# T-R2-02 开发交接单：文件状态快照 + 低于水位瘦身

## 任务与范围

- 已实现：
  - `FileStateCache` 记录每个已访问文件的 **行数** 与 **最近一次操作**
    （read / write），随检查点导出/恢复；新增 `render_snapshot()` 生成
    「路径 + 行数/大小 + 最近读写 + 短指纹」状态块（最多 20 个文件）。
  - `context._microcompact` 在拿到文件路径集合时，把**旧 `read_file` 结果**
    换成「看快照 / 需要时重新 read_file」的指针，完整正文仍在磁盘。
  - `ContextCompactor.build` 在 token 模式、**低于水位线**时也会先跑一次
    确定性瘦身：只要旧写参数/读取结果确实可折就生效，报告
    `triggered_by="micro:below"`；没有可折内容时行为与原来一致。
  - `AgentSession.ask` 把文件路径集合交给压缩层，并在压缩/折叠发生后把文件
    状态快照插到对话尾部（最后一条 user 之前），模型仍能按需回读。
- 明确未实现：
  - 没有跑真机 T09 对照：主后端当前虽在监听，但未确认它已加载本次改动；
    需要重启或确认后再跑 `evals/compare.mjs`。
  - 没有做任务二「压调用次数」；诊断清单见 `dev/T09-CALL-DIAGNOSIS.md`。
  - 快照没有放文件内容摘要/变更计数，只有行数、大小、最近操作、短指纹；
    需要更强摘要时再加。
  - `apply_patch` / `notebook_edit` 的回执仍带 diff（T-R2-01 的范围外项，未变）。

## 修改内容

- `services/api/src/blank_api/services/tools/file_state.py`：`FileState` 增加
  `lines` / `last_op`；`record_read` / `record_write` 记录行数与操作；
  `export` / `restore` 兼容旧检查点；新增 `render_snapshot()`。
- `services/api/src/blank_api/services/tools/filesystem.py`：`read_file` 把
  `total_lines` 传给 `record_read`。
- `services/api/src/blank_api/services/agent/context.py`：`MicroStats` 增加
  `reads_folded`；`_microcompact` 支持 `snapshot_paths`；`build` 增加
  `snapshot_paths` 并在低于水位时做确定性瘦身（`micro:below`）。
- `services/api/src/blank_api/services/agent/graph.py`：`AgentSession.ask`
  传入文件路径并在压缩/折叠后插入状态快照。
- `services/api/tests/test_context_snapshot_v2.py`（新）：快照行数/操作/
  恢复、列表上限、旧读取折叠、低于水位瘦身。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_context_snapshot_v2.py tests/test_context_layers_v12.py tests/test_context_watermark_v13.py tests/test_context_compactor.py tests/test_context_pairing_v14.py tests/test_claude_parity_v24.py tests/test_protocol_cache_v14.py tests/test_retention_v25.py tests/test_edit_batch_v400.py tests/test_edit_tolerance_v424.py -q` | **151 passed** | 快照、压缩、配对、读后写 |
| `pytest tests/test_agent_continuation_v25.py tests/test_async_hooks_v119.py tests/test_agent_remediation_v24.py tests/test_agent_v04.py tests/test_compaction_window_v23.py tests/test_idle_compaction_v427.py tests/test_context_restart_v427.py -q` | **77 passed** | Agent 会话与压缩集成 |
| 最终合并定向（27 个文件，含 T-R2-01 的编辑/交付/审查/回执与 Agent 集成） | **338 passed** | 无回归 |
| `git diff --check` | 通过 | 0 |
| 真机 T09 / `evals/compare.mjs` | 未跑 | 后端在监听但未确认已加载；需重启/确认后对照 |

## 风险与需要审查的点

- **行为变化**：token 模式下低于水位也会折叠旧写参数/旧读取结果。最近 8 条
  消息保持原样；更早的正文由快照 + 磁盘回读兜底。需要真机对照确认编辑正确性
  与隐藏验收不降。
- 快照按文件 mtime 排序，不是按访问时间；同一 mtime 下按路径排序，输出可复现。
- 快照插入在最后一条 user 之前（与交接笔记同一位置），只改尾部，尽量不破坏
  前缀缓存；但每次 `micro:below` 生效都会改变尾部一条系统消息。
- 低于水位瘦身会在 1M 窗口的小任务上生效，可能让模型更早失去旧工具正文；
  这是本任务要换来的 token 收益，必须用 T09/T01/T25 同批对照确认。
- `reads_folded` 只折 `read_file`，不折 `search_code`/`list_files`；后者通常
  是短结果，收益小。
- 旧检查点没有 `lines`/`last_op`，恢复后快照显示字节数与「最近读写」缺失；
  不影响写入闸门（guard 仍只用 size/mtime/sha）。

## 建议人工验收

1. 重启后端，跑 T09 同题，记录调用数、prompt tokens、active context、耗时；
   与基线 19 / 487,764 / 25,517 / 74.7s 对照。
2. 在真实任务里修改两个文件：确认 `context.microcompacted` 事件出现
   `micro:below` 与 `reads_folded`/`args_shrunk`；模型仍能正确编辑
   （没有因为旧读取被折而用错锚点）。
3. 打开任务证据页，确认完整 diff 仍来自 `FileChange`，没有被快照影响。
