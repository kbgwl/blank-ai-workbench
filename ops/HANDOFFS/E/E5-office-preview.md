# E5 文档产物预览 开发交接单

## 任务与范围
- 已实现：
  - 结果面板打开 `.docx / .xlsx / .pptx` 时，后端抽出文本层（标题、段落、表格、
    工作表、幻灯片文字）并按 markdown 返回；前端用现有 Markdown 渲染器展示。
  - 预览顶部写明「Word 文档 · 94 段 · 12 个表格；文本预览，样式与图表已简化」。
  - 抽不出来（坏文件 / 加密 / 超限）时保持原「二进制文件」回执，不伪装成空文件。
  - 抽取上限：20,000 字、3 个工作表、30 行 × 12 列、40 页、2,000 段。
- 明确未实现：
  - 不还原样式、图表、公式、图片；要看原貌仍需用系统应用打开。
  - 未改 `read_file` 工具：模型读 Office 文件仍是二进制回执，本卡只服务结果面板。

## 修改内容
- 新增 `services/api/src/blank_api/services/office_preview.py`：
  `extract_office()`（docx/xlsx/pptx）+ `is_office()`；表格包进 `text` 代码块。
- `services/api/src/blank_api/api/routes/files.py`：`GET /projects/{id}/file`
  在二进制分支尝试抽取，成功则返回 `preview: "office"` 与 markdown content。
- `web/src/components/bot/preview.ts`：office 扩展名 → markdown。
- `web/src/components/bot/file-preview.tsx`：office 预览提示条。
- `web/src/components/projects/api.ts`：`FileContent.preview?`。
- 新增 `tests/test_office_preview_v413.py`（5 条）；`preview.test.ts` 补 office 断言。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_office_preview_v413.py -q` | 5 passed | docx/xlsx/pptx/坏文件 |
| 前端 `node --test ...` | 473 passed / 0 failed | 含 office 类型判断 |
| 前端 tsc / eslint | 退出码 0 | 类型与风格 |
| `GET /projects/29044bb7.../file?path=数据智能分析平台-项目计划书.docx` | 200 | `binary=false`、`preview=office`、**15,277 字** |
| 浏览器探针（1440×900） | 全部断言通过 | 面板显示「Word 文档」提示条与正文，无「二进制文件」提示；console 0 |

截图：`data/e5-office-preview.png`（真实任务产物，94 段 / 12 个表格）。

## 风险与需要审查的点
- 抽取是只读、有上限的；20k 字以上的文档只显示前段，顶部会标「预览已截断」。
- 首次 import lxml/Pillow 可能较慢（约 1 秒），预览请求会等这一下。
- 表格以代码块呈现（前端 Markdown 渲染器不支持表格语法），列宽不保证对齐。
- 图片与图表不显示，这是文本层预览的边界。

## 建议人工验收
1. 打开「你好」会话右侧结果面板 → 文件 → `数据智能分析平台-项目计划书.docx`，
   确认能看到标题与正文、顶部有说明条。
2. 打开任一 `.xlsx` / `.pptx` 产物，确认工作表/幻灯片文字可见。
