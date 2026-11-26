# D5 液态玻璃皮肤 v1（浅色单皮肤 · 视觉草案）

## 任务与范围

- 已实现：
  - **全站只保留一套浅色液态玻璃皮肤**；深色主题、`data-theme` 切换、主题开关与 `theme-toggle.tsx` 全部移除。
  - 极光背景：高饱和紫/青/粉渐变 + 两团缓慢漂移的光晕 + 中央柔光。
  - 玻璃四层：半透明底、大半径磨砂（blur 34px / saturate 190%）、左上角镜面反光、亮描边 + 外圈暗边 + 软投影。
  - 质感增强：玻璃内细颗粒（SVG noise + soft-light）、内层折射与底部厚度阴影、主操作按钮彩色釉（brand-glass）。
  - 侧栏单独一套 `glass-side`：更实、无噪点，保证 11–13px 小字清楚。
  - **修复 portal 被覆盖**：移除了 `body > * { position:relative; z-index:1 }`，它会把目录选择框/下拉/命令面板等 `fixed z-50` 的 portal 压成普通流，导致「选择目录」点不开。
  - 主框架统一玻璃化：应用外壳、左栏、首页主卡片、输入区、共享 Panel、右侧结果面板、团队/工作台左栏、会话条、待审批区、文件预览、Diff 文件树。
  - 输入框、滚动条、按钮按压反馈同步玻璃化。
- 明确未实现：
  - 所有页面逐个人工走查（技能、监控、证据库、Reverse Lab、头脑风暴、项目页、日常页尚未逐一截图复核）。
  - 弹窗、菜单、Toast、表格等细粒度玻璃重绘；目前主要靠语义令牌继承。
  - 页面「丝滑程度」优化（轮询、滚动、骨架屏、过渡）——下一阶段。
  - 桌面打包与严格 CSP 重新构建；本卡未跑打包。

## 修改内容

- `web/src/app/globals.css`：单套浅色玻璃令牌、极光背景、光晕动画、`glass` / `glass-soft`、输入框、滚动条、按压反馈。
- `web/src/app/layout.tsx`：移除主题引导内联脚本与 `data-theme` 依赖。
- `web/src/components/app-rail.tsx`、`bot/sidebar.tsx`：移除主题开关。
- `web/src/components/theme-toggle.tsx`：**已删除**。
- `web/src/components/app-shell.tsx`、`panel.tsx`、`bot/bot-app.tsx`、`bot/composer.tsx`、`bot/panel.tsx`、`bot/pending-panel.tsx`、`bot/file-preview.tsx`、`bot/change-view.tsx`、`team/team-view.tsx`、`workbench/workbench.tsx`、`tasks/thread-bar.tsx`：玻璃化。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| `tsc --noEmit` / `eslint src --max-warnings=0` | 均退出码 0 | 无输出 |
| 前端单测 | **469 passed / 0 failed** | 与改版前一致 |
| 界面 journey（kanban） | 退出码 0 | 结论行正确 |
| 浏览器探针 · 首页 | console **0** | `data/liquid-glass-light-v7.png` |
| 浏览器探针 · 设置 | console **0** | `data/liquid-glass-light-settings3.png` |
| 浏览器探针 · 会话 | console **0** | `data/liquid-glass-light-thread2.png` |
| 浏览器探针 · 目录选择框 | 断言 **true** | 点「选择目录 → 选择目录…」后出现 `[role=dialog]`；`data/liquid-glass-dir-picker2.png` |

运行中的本地地址：前端 `http://127.0.0.1:3000`，后端 `http://127.0.0.1:8000`。

## 风险与需要审查的点

- `backdrop-filter` 在低端机/长列表上可能掉帧；当前只给主面板与 Panel 加，列表项未加。
- 浅色玻璃的对比度已提高一档，但仍需真实设备/不同显示器复核。
- 删除了 `theme-toggle.tsx`；仓库里已无 `Blank.theme` / `data-theme` 引用，意图之外不再有深色路径。
- **不要给 `body` 的直接子节点统一加 `position/z-index`**：会把 portal 的 `fixed/z-50` 覆盖掉，目录选择框、下拉、命令面板会集体失效。光晕现在用 `z-index:-1`。
- 桌面打包前必须重新构建：内联主题脚本已移除，CSP 哈希会在构建期重算。
- 视觉草案，不代表全部页面已统一走查；下一轮按页面清单逐个过。

## 建议人工验收

1. 打开 `http://127.0.0.1:3000`，确认只有浅色液态玻璃，没有皮肤切换入口。
2. 打开「设置 → 模型与供应商」「安全与权限」「记忆」，确认 Panel、输入框、按钮同一套玻璃。
3. 打开最近会话（如 `shopbench 真实复跑`），确认左栏、主对话区、右侧改动面板、Diff 文件树统一。
4. 打开「任务」「Bot 团队」「项目」页，检查是否还有明显未玻璃化的死白/死灰块。
5. 点输入框下方「选择目录」，再点「选择目录…」，确认目录框能打开、能进出目录并选当前目录。
6. 看截图：`data/liquid-glass-light-v7.png`、`data/liquid-glass-light-thread2.png`、`data/liquid-glass-light-settings3.png`、`data/liquid-glass-dir-picker2.png`。
