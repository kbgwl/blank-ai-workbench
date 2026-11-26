# C3 开发交接单

## 任务与范围

- 已实现：
  - 执行型任务的验收结论要求 JSON；第一次解析失败、不是 JSON 对象或缺
    `acceptance` 字段时，用更硬的格式要求**重试一次**。
  - 两次都失败时，验收结果记 `covered=null`（未判定），不再记 `covered=false`
    （没覆盖）；证据文本明确写「未判定」。
  - 前端证据面板的验收标题增加「N 条未判定」，不再用 `0/4` 暗示四项都失败。
- 明确未实现：
  - 没有改验收判据的生成方式；模型仍要按 JSON 输出，只是多一次纠正机会。
  - 没有把未判定算进 `acceptance_coverage`（评测口径仍只数 covered=true）。
  - 未跑全量后端测试。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：
  - `_summarize_result_body` 的验收调用改为最多两次；记录两次 usage；
  - 两次失败时 `covered=None`、证据写「未判定」。
- `web/src/components/tasks/evidence-panel.tsx`：验收标题增加未判定条数。
- `services/api/tests/test_acceptance_retry_v491.py`：新增 2 例。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest test_acceptance_retry_v491.py test_delivery_snapshots_v153.py test_delivery_unverified_v479.py test_done_checks_v427.py test_empty_response_zero_delivery.py test_goal_checks_v490.py test_delivery_incomplete_v489.py -q` | `52 passed` | 含新 2 例 |
| 前端全量单测 | `439 passed` | — |
| `tsc --noEmit` | 退出码 0 | — |
| evals T01–T03 基线（修复前） | 3/3 succeeded，`acceptance_coverage=0.636` | T02 四项因验收结论不可解析全部未覆盖 |
| evals T02 重跑（修复后） | succeeded，`acceptance_coverage=1.0` | 验收结论重试生效 |

## 风险与需要审查的点

- 重试只多一次调用，且只在解析失败时发生；正常响应不会多花钱。
- `covered=None` 的语义是「验收没做成」，与「验收没过」分开；评测的
  `acceptance_coverage` 仍只数 true，所以该指标不会因未判定而虚高。
- 前端标题的未判定条数与详情行的 `?` 同源（`item.covered === null`）。

## 建议人工验收

1. 跑一条有验收条件的任务，确认验收结果正常覆盖。
2. 构造一次不可解析的验收响应，确认最多重试一次；两次都失败时显示「未判定」。
3. 确认证据面板标题在未判定时写「N 条未判定」，不是 `0/N` 的误导。
