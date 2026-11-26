# D9 任务筛选状态进地址栏 开发交接单

## 任务与范围
- 已实现：
  - 任务页的搜索词与状态筛选写入 URL（`?q=…&status=failed|active|done`），
    刷新、收藏、从详情返回后筛选仍在。
  - 使用 `replaceState`，不产生历史记录；`project` 等其他查询参数原样保留。
  - 非法状态值、空白搜索词在读回时忽略（回到「全部 / 空搜索」）。
  - 顺手修任务页左下角的模型显示：改为后端默认（有 Key + is_default），
    不再显示没配 Key 的 anthropic。
- 明确未实现：
  - 不做跨标签页同步（storage / BroadcastChannel）。
  - 不动任务列表的轮询与分组逻辑。

## 修改内容
- 新增 `web/src/components/workbench/filters.ts`：
  `parseTaskFilters()` / `writeTaskFilters()`（纯函数，保留未知参数）。
- `web/src/components/workbench/workbench.tsx`：
  挂载后读回 URL（异步回调，避免 effect 内同步 setState）；筛选变化时
  `replaceState` 写回。
- 新增 `web/src/components/workbench/filters.test.ts`（5 条）。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| 前端 `node --test ...` | 481 passed / 0 failed | 含筛选映射 5 条 |
| 前端 tsc / eslint | 退出码 0 | |
| 浏览器探针（/tasks） | 全部断言通过 | 输入「计划书」+ 点「未成功」→ URL `?q=…&status=failed`；刷新后输入框与选中状态都恢复；console 0 |
| 浏览器探针（左下角模型） | `hasDeepseek=true / hasClaude=false` | 显示实际默认模型 |

截图：`data/e8-task-filters.png`。

## 风险与需要审查的点
- 每次输入都会 `replaceState` 一次；没有历史记录膨胀，但地址栏会随输入变化。
- 读回放在 setTimeout(0) 里：首帧仍是「全部」，随后切到 URL 里的筛选（避免 hydration 不一致）。
- 其他页面若也写 `/tasks?q=`（例如详情返回），会直接生效，不需要额外处理。

## 建议人工验收
1. 任务页搜索「计划书」并切到「未成功」，刷新页面，确认筛选还在。
2. 复制地址栏链接新开标签页，确认打开就是同一筛选。
