# T-R2-03 开发交接单：压调用次数（提示词第一刀）

## 任务与范围

- 已实现：
  - 系统提示工具纪律第 5 条：**不同文件的独立写/改**在权限档自动放行时允许
    同一批发出；同一文件、需要审批或结果有依赖的仍一次一个。
  - 第 6 条：刚写过的文件，编辑时直接用写入的原文做 `old_string`，不要为了
    找锚点重读整个文件；编辑成功后也不要再读一遍验证。
- 依据：`dev/T09-CALL-DIAGNOSIS.md` 的实测分布——19 次调用里，3 次是写完
  重读、3 次 write 分三轮、3 次 edit 分三轮；目标 ≤12 次。
- 明确未实现：
  - 没有跑 T09 同题对照：后端当前虽在监听，但不确定是否为本次改动后的进程；
    需要确认/重启后再跑 `evals/compare.mjs`。
  - 没有做工具说明书瘦身（34 个工具、每轮约 22,467 字符）。
  - 没有给写工具加“不同路径并发执行”的白名单；当前执行层对
    非 `PARALLEL_SAFE_TOOLS` 的调用按顺序跑，同一批只省模型往返，不省磁盘时间。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：`SYSTEM_PROMPT` 工具纪律
  第 5/6 条改为允许独立写/改同批、并禁止为找锚点重读刚写过的文件。
- `services/api/tests/test_claude_parity_v24.py`：系统提示判据新增
  「不同文件的写/改」「直接用你写入的原文做 old_string」。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_claude_parity_v24.py tests/test_agent_v04.py tests/test_context_snapshot_v2.py -q` | **78 passed** | 提示词/集成/快照 |
| 27 文件完整定向回归（同 T-R2-02） | **338 passed** | 无回归 |
| 真机 T09 / `evals/compare.mjs` | 未跑 | 需确认后端进程已加载本次改动 |

## 风险与需要审查的点

- 这是**模型行为**改动，不是确定性执行改动；是否真的把 3+3 合并成两批，
  必须用 T09 实跑确认。执行层已支持一条 assistant 消息里多个 `write_file` /
  `edit_file`，并按顺序落盘；不需要改 `PARALLEL_SAFE_TOOLS`。
- 批量写的前提是“当前权限档自动放行”。默认档下写操作可能逐条审批，提示词
  已把这种情况排除；如果模型误判，审批链仍会逐卡拦住，不会跳过。
- “不要为找锚点重读”可能让模型拿被工具归一化过的内容做 `old_string`
  （CRLF/BOM/append）。`edit_file` 已有引号/空白容错；失败会明确报
  not_found/ambiguous，模型仍可 read_file 后再改。
- 工具表瘦身未做；它影响每轮固定成本，是下一步的低风险杠杆，但要先跑
  `SCHEMA_BUDGET_CHARS` 相关判据。
- 任务一（快照）与任务二提示词第一刀尚未分别出数；这次 T09 对照拿到的是
  组合效果。要归因时先临时回退提示词跑一遍，或先只上任务一跑一遍。

## 建议人工验收

1. 确认后端已加载本次改动后，跑：
   `node evals/compare.mjs --only T09 --agents ours --cleanup`
   记录调用数、prompt tokens、active context、耗时；与 19 / 487,764 /
   25,517 / 74.7s 对照。
2. 观察 T09 工具序列是否出现“同一轮多个 write_file / edit_file”，以及
   写完后是否还重读三个文件。
3. 抽一个真实小任务确认编辑正确性没有下降。
