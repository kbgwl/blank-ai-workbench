# X1 开发交接单

## 任务与范围

- 已实现：
  - `web/src/components/tasks/transition-copy.ts` 补登记后端新增的两个跳转 rule：
    `list_only`（只要清单，直接按工具回执出列表，accent）、
    `ui_repair_reserve`（预算已尽，保留一次界面修复，warn）。
  - 前端单测从 442/443 恢复到 **443/443**，`tsc --noEmit` 退出码 0，
    界面 journey 退出码 0 且结论行正确。
  - B10 验收记录追加「2026-11-23 复跑补记（X1）」，原始失败保留不回填。
- 明确未实现：
  - 194 项未提交改动的按任务归批与提交（盘点命令被中断，未继续）。
  - B10 的最终人工验收签字。

## 修改内容

- `web/src/components/tasks/transition-copy.ts`：补两条 rule 登记，不改跳转逻辑。
- `docs/validation/acceptance-2026-11-21.md`：B10 节追加复跑补记。
- `ops/STATUS.md`：更新 B10 验收状态为「失败项已修复，待人工最终确认」。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `node --test --test-concurrency=2 --import ./test-bootstrap.mjs "src/**/*.test.ts" scripts/csp.test.mjs` | **443 passed / 0 failed** | 退出码 0，原 1 红消除 |
| `node ./node_modules/typescript/bin/tsc --noEmit` | 退出码 0 | 无输出 |
| `python scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html` | 退出码 0 | 结论：通过：打开有东西，动作有变化，刷新还在 |

## 风险与需要审查的点

- 只改前端文案登记表，不改变后端跳转判定与事件载荷；后端 `why` 仍优先显示。
- 两条文案是摘要，若措辞不符合产品口径，可单独调整，不影响判据。
- 未跑完整后端 5000+；本卡未改后端。

## 建议人工验收

1. 打开任一含跳转事件的任务时间线，确认 `list_only` 与 `ui_repair_reserve` 不再标黄。
2. 复跑前端单测与 tsc，确认 443/443、退出码 0。
3. 在 B10 验收记录末尾查看「复跑补记（X1）」，确认原始失败与修复结果都在。
