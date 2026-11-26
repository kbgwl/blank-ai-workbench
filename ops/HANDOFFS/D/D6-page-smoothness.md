# D6 全页面玻璃一致性 + 页面丝滑 v1

## 任务与范围

- 已实现：
  - 12 个页面逐页浏览器审计（首页、任务、设置、团队、技能、证据库、监控、项目、Reverse Lab、日常、头脑风暴、功能中心）：console 0、无失败请求。
  - 任务页补齐外层玻璃卡片（此前 `/tasks` 是 bare 布局，内容直接铺在背景上）。
  - 会话页：向上翻阅后出现「回到底部」，点击平滑滚回底部并恢复自动贴底。
  - 会话加载态从一行「读取对话…」换成 3 个骨架块。
  - 全局页面入场动画（`page-in`，尊重 `prefers-reduced-motion`）。
- 明确未实现：
  - 页面级数据轮询/SSE 重构；目前审批等态仍是 2.5 秒拉一次。
  - 其他页面的骨架屏与空态细化。
  - 弹窗/菜单/Toast 的逐项视觉走查。
  - 真机低端设备上的 backdrop-filter 性能测量。

## 修改内容

- `web/src/components/workbench/workbench.tsx`：任务页 main 改为 `glass` 卡片（m-3 + 圆角 + overflow-hidden）。
- `web/src/components/bot/bot-app.tsx`：滚动状态、`showJump`、`jumpToBottom`、骨架屏、main 相对定位。
- `web/src/app/globals.css`：`page-in` 动画与 reduced-motion 分支。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| 12 页面浏览器审计 | **console 0 / netErr 0** | 截图在 `data/glass-audit/` |
| 任务页复截 | console 0 | `data/glass-audit/tasks2.png` |
| 回到底部 | 断言 **true** | 1280×600 视口：滚动到顶出现按钮，点击后回到底部；`data/liquid-jump-bottom2.png` |
| 前端单测 | **469 passed / 0 failed** | 与改版前一致 |
| `tsc --noEmit` / `eslint` | 均退出码 0 | 无输出 |
| 界面 journey | 退出码 0 | 结论行正确 |

## 风险与需要审查的点

- `main` 的入场动画是全局规则；如果将来有页面需要无动画，用局部样式覆盖。
- 「回到底部」阈值 80px；贴着底部 80px 内视为已贴底，不显示按钮。
- 任务页加了 12px 外边距，和首页/设置页的外壳间距一致；窄屏下需确认内容不挤压。
- 未做真实低端设备性能测量；`backdrop-filter` 仍是主要成本。
- 未跑完整后端；本卡未改后端。

## 建议人工验收

1. 打开 `/tasks`，确认任务列表现在包在一块圆角玻璃卡片里。
2. 打开一条长会话，向上翻，确认底部出现「回到底部 ↓」；点一下平滑回到底部。
3. 打开另一条会话，确认加载时是骨架块而不是一行文字。
4. 切换几个页面，确认入场过渡轻、不闪。
