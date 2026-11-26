# B3b 开发交接单

## 任务与范围

- 已实现：
  - `preview.ts`：扩展名判断、JSON 格式化、CSV/TSV 解析（引号、双引号转义、CRLF、200 行/50 列上限）。
  - `file-preview.tsx`：HTML / Markdown / JSON / CSV / 文本五类预览。
  - `panel.tsx` 文件视图接入。
- 明确未实现：
  - B3c 多文件应用预览（启动工作区服务、端口生命周期、审批）。
  - HTML 脚本执行与相对资源加载（刻意不做，见风险）。
  - JSON 树折叠、大文件按需分段加载。

## 修改内容

- `web/src/components/bot/preview.ts`（新增）：纯函数。
- `web/src/components/bot/preview.test.ts`（新增）：6 条判据。
- `web/src/components/bot/file-preview.tsx`（新增）：五类渲染器。
- `web/src/components/bot/panel.tsx`：文件视图改用 `FilePreview`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | **458 passed / 0 failed** | 原 452 + 新增 6 |
| `tsc --noEmit` | 退出码 0 | 无输出 |
| `eslint src --max-warnings=0` | 退出码 0 | 无输出 |
| `scripts/acceptance.py --journey acceptance/kanban.json` | 退出码 0 | 结论：通过：打开有东西，动作有变化，刷新还在 |

## 风险与需要审查的点

- HTML 预览用 `sandbox=""`：脚本不执行、表单不能提交、不能访问父页面存储；相对路径的 CSS/JS/图片不会加载。这是刻意边界，不是缺陷；完整交互留给 B3c。
- CSV 解析是预览级状态机，不覆盖所有 RFC 4180 边界（如极长跨行字段）；超限会标记截断。
- Markdown 复用现有 `MarkdownView`，链接仍走 `safeLinkHref` 白名单。
- 未跑完整后端；本卡未改后端。

## 建议人工验收

1. 打开一个 `.md` 产物，确认标题/列表/代码块正常，链接不可执行 `javascript:`。
2. 打开一个 `.json` 产物，确认格式化；再打开一个坏 JSON，确认警告与原文都显示。
3. 打开一个 `.csv` 产物，确认表格、表头、截断提示。
4. 打开一个 `.html` 产物，确认顶部有「脚本不会执行」提示，页面静态渲染。
5. 打开超大文件，确认顶部「预览仅显示前一部分」。
