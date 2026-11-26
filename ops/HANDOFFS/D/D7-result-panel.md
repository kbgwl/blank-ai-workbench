# D7 结果面板重设计 + 无产出隐藏 + 横向滚动 开发交接单

## 任务与范围
- 已实现：
  - 普通对话没有产物/改动/证据时，右侧结果面板与顶部面板入口整体不显示。
  - 右侧结果面板重设计：默认 620px、四视图固定入口（产物/改动/文件/预览）、
    文件标签独立一行、工具栏精简（只留路径/文件数/状态/复制）、移除无效的后退/前进和下拉菜单。
  - 代码 diff、文本/JSON 预览支持横向滚动（不再强制换行），左右拉动可看全，无需全屏。
- 明确未实现：
  - 未改后端；未改面板数据来源与轮询逻辑。
  - 未做面板内搜索状态持久化、未做视图偏好记忆。

## 修改内容
- `web/src/components/bot/bot-app.tsx`：新增 `panelHasContent`，面板按钮和 `<TaskPanel>` 仅在存在产物/改动/证据/验收时渲染。
- `web/src/components/bot/panel.tsx`：重设计工具栏与布局；默认宽 620（420–900 可拖、双击复位）；移除无用的视图菜单与导航按钮；文件标签独立一行。
- `web/src/components/bot/change-view.tsx`：diff 行改 `whitespace-pre`，滚动容器加 `data-diff-scroll`；
  行容器 `w-max min-w-full`、行本身 `w-full`（GitHub 式），行底色铺满整个可滚动宽度；
  路径栏加 `left-0` 横向粘住，拖到右边仍能看到当前文件。
- `web/src/components/tasks/diff-panel.tsx`：同样改为横向可滚动 + `w-max min-w-full` 行容器，加 `data-diff-scroll`。
- `web/src/components/bot/file-preview.tsx`：文本/JSON 预览改 `whitespace-pre` + 横向滚动。

## 验证结果
| 命令 | 结果 | 摘要 |
|---|---|---|
| `node ./node_modules/typescript/bin/tsc --noEmit` | 退出码 0 | 无类型错误 |
| `node ./node_modules/eslint/bin/eslint.js src --max-warnings=0` | 退出码 0 | 无告警 |
| `node --test --test-concurrency=2 --import ./test-bootstrap.mjs "src/**/*.test.ts" scripts/csp.test.mjs` | 469 passed / 0 failed | 全量前端单测 |
| `python scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html` | 退出码 0 | 旅程通过 |

浏览器探针（CDP，本机 Chrome）：
- 无产出会话（「你好」）：面板 separator 不存在断言 **true**，console 0；
  截图 `data/panel-no-output.png`。
- 有产出会话（shopbench 真实复跑）：面板存在断言 **true**；
  `[data-diff-scroll]` 实测 `scrollWidth 961 / clientWidth 444 / scrollLeft 200`
  且横向滚动断言 **true**，console 0；截图 `data/panel-redesign-thread-clean.png`。
- 横向拖动后底色复验（补记）：行宽全部 **961px**（= scrollWidth）、
  `scrollLeft 400` 处命中行 div 且带新增/删除底色，断言 **true**，console 0；
  截图 `data/d7-bg-fixed.png`。修复前同坐标无底色（`data/d7-bg-scrolled2.png`）。

## 风险与需要审查的点
- 面板是否显示取决于本轮/证据里是否真有内容；若后端未来把“仅文件路径”也算产物，
  面板会更常出现，属预期。
- 横向滚动基于 `whitespace-pre`，超长单行完全靠拖动查看，未加行号列。
- 行底色/文字按最宽行铺满（961px）；极短行拖动后底色仍在，属预期。
- 面板宽度已持久化（`blank:bot-panel-width`，拖动/双击/键盘调整后写入）；
  视图与标签页偏好刷新后不保留。

## 建议人工验收
1. 打开普通「你好」对话，确认右侧没有面板、顶部也没有面板按钮。
2. 打开 shopbench 真实复跑会话，确认面板布局（视图 pill、文件标签、工具栏）顺眼。
3. 在改动/文件视图中把 diff 左右拖动，确认长行可看全；再点全屏/收起确认可用。
