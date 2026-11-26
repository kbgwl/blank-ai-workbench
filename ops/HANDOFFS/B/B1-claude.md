# B1 开发交接单

## 任务与范围

- 已实现：
  - **计划未回写的真相**：任务状态为 `succeeded`（含老数据同义状态 `completed`）、
    计划 `completed=0` 时，计划面板、任务列表和对话页计划卡都显示「计划未更新」，
    并说明“以时间线的实际执行为准，可手动勾选纠正”。失败/取消任务的 `0/4` 保留，
    那是有效信息。
  - **验收 0/0 的真相**：证据库列表徽标在没有验收条件时显示「没有验收条件」，
    不再写 `0/0 验收`。
  - 纯函数 `planIsStale` / `planProgressText` / `criteriaChipText` 集中判定，
    三处界面共用同一口径，避免各写一份。
- 明确未实现：
  - 没有改动后端计划生成/回写逻辑（模型仍可能不调用 `update_plan`）。
    这是 C 主线“自主计划与验证”的范围。
  - 没有做真机浏览器截图；需要人工验收确认视觉位置与文案。

## 修改内容

- `web/src/components/tasks/plan.ts`：新增 `planIsStale`、`planProgressText`。
- `web/src/components/tasks/plan-panel.tsx`：计划面板显示「计划未更新」与说明，隐藏 0% 进度条。
- `web/src/components/workbench/workbench.tsx`：任务列表进度标签使用同一口径。
- `web/src/components/bot/turn.tsx`：对话页计划卡显示「计划未更新」与说明。
- `web/src/components/evidence/format.ts`：新增 `criteriaChipText`。
- `web/src/components/evidence/evidence-view.tsx`：列表徽标改用 `criteriaChipText`。
- 测试：`web/src/components/tasks/plan.test.ts`、`web/src/components/evidence/format.test.ts`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 前端全量单测 | `pass 439`、`fail 0`，退出码 0 | 含新增 3 组判据 |
| `tsc --noEmit` | 退出码 0 | 无类型错误 |
| 界面 journey | 退出码 0 | 结论行通过 |

## 风险与需要审查的点

- 只对 `succeeded` 判定“计划未更新”；`failed` / `cancelled` 的 `0/4` 仍然显示，
  因为它们本来就该说明“没做完多少”。
- 计划面板允许手动勾选纠正；勾选会写回后端（沿用既有 `togglePlanStep`）。
- 未做浏览器截图；请人工确认三个位置的文案与视觉不拥挤。

## 建议人工验收

1. 打开一个已完成但计划全是空圈的任务，确认计划面板显示「计划未更新」和说明，
   不再出现 0% 进度条。
2. 任务列表里同一条任务显示「计划未更新」，不是「计划 0/4」。
3. 对话页同一轮的计划卡显示「计划未更新」，不再显示 0/4 与空白进度条。
4. 打开证据库，找一条没有验收条件的交付，确认徽标写「没有验收条件」而不是 `0/0 验收`。
