# D2 开发交接单

## 任务与范围

- 已实现（都是 harness 自身，不是手工改产物）：
  - **前端交付闸门**：本轮改了 `.html/.css/.jsx/.tsx/.vue/.svelte`（或任务像前端时
    的 `.js/.mjs/.cjs`），验证节点要求最后一次前端写入之后有 `browser_check` 回执，
    且必须：控制台 0 错误、0 个失败请求、至少 1 条通过的断言、至少 1 条通过的动作
    （`reload`/`click`/`type`…）。不满足就判验证失败、回执行节点，并注入
    「打开 → 断言 → 动作 → 控制台零错误」的修复指令。
  - 浏览器不可用（没有 node / Chrome / 探针缺失）记 `ui_check=unavailable`，
    不把环境问题误判成页面坏了。
  - **审查清单**：`review_result` 的提示新增五条底线——反 AI 仪表盘、前端必须真验、
    路径穿越/请求体上限/输入校验/http(s)/密钥/CSP/回环绑定、代码规范、结论与事实一致。
  - **并行工具图片消息顺序修复**：一批 tool_calls 里如果多个调用带图，带图 user
    消息原先插在 tool 消息之间，OpenAI 兼容网关下一轮直接 400
    `insufficient tool messages following tool_calls`。现在先攒所有截图，整批 tool
    消息写完再统一追加。
  - **browser_check 的 file:// 边界**：原先 `file:///C:/任意路径` 直接放行，等于让
    浏览器读工作区外任意文件；现在解析后必须落在工作区内，拒绝远程 host。
  - **交付文本**：没有自动化测试但浏览器自检通过时，交付不再只剩「未跑校验」，
    而是写「未跑校验：已做浏览器自检（browser_check 通过：<url>，N 个断言 / M 个动作）；
    <原跳过原因>」。不冒充带退出码的「验证：」行。
  - `CODING_INSTRUCTIONS` 明确写出「前端不跑 browser_check 会被验证节点拦截」。
- 明确未实现：
  - 没有加静态「反 AI 样式扫描器」（渐变/阴影/圆角计数）——硬阈值容易误伤正常设计，
    沿用「提示 + 审查清单 + 浏览器证据」三件套。
  - 没有对主仓库做全量安全审计；本轮只修 harness 生成链路。
  - 没有做像素级视觉验收。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：前端闸门、浏览器不可用分支、
  交付自检说明、审查清单、并行图片消息延迟追加、提示词一句。
- `services/api/src/blank_api/services/agent/planner.py`：`ui_unverified` 修复指令。
- `services/api/src/blank_api/services/tools/browser.py`：`file://` 工作区边界。
- `services/api/tests/test_ui_delivery_gate_v492.py`（新）：14 个闸门用例。
- `services/api/tests/test_image_passthrough_v427.py`：并行图片顺序回归。
- `services/api/tests/test_browser_check_v427.py`：file:// 内/外/远程 host 三例。
- `services/api/tests/test_delivery_unverified_v479.py`：浏览器自检进交付文本。
- `services/api/tests/test_doing_tasks_prompt_v167.py`、`test_autorepair_verdict_v112.py`：
  既有验证测试配合闸门（补浏览器回执 / 改用非前端文件）。
- `services/api/tests/test_output_styles_v47.py`：`SYSTEM_PROMPT` 哈希同步。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 19 个相关测试文件 | **272 passed** | 闸门 14、图片 18、浏览器 15、交付/验收/提示词全过 |
| 真机任务 `5a814453` | succeeded / 7 步 | browser_check 带断言 + 动作，控制台 0 错误 |
| 真机任务 `c0ceba72` | succeeded / 7 步 | 交付出现「已做浏览器自检（browser_check 通过：…1 个断言 / 6 个动作）」；事件 `ui.check_passed` |
| 真机任务 `f31d0504` | failed（修复前） | 三个并行 browser_check 触发 400；复现并定位为图片消息顺序 bug，已修复 |

- 三个真机任务都在 `data/_ui_gate_probe*`；前两个是临时验证工作区，保留作证据。
- 后端已重启并加载新代码，`/api/v1/health` 返回 `status=ok / database=connected`。

## 风险与需要审查的点

- 闸门对 `.html/.css/.jsx/.tsx/.vue/.svelte` 一律生效；改一个静态 CSS 也会要求浏览器
  回执。任务确实不需要浏览器时，用 `browser_check` 打开文件加一条 reload 即可。
- 需要本机服务的页面，Agent 必须先 `run_shell` 起服务；没起起来时闸门会打回，这是
  预期行为（宁可判未验，不假装验过）。
- 修复预算与既有验证/审查共用；连续 3 轮拿不到浏览器证据会停，不再空转。
- 真机验证在 `data/` 下留了两个临时项目和三个任务记录，供你查看；要清理需要删项目。

## 建议人工验收

1. 打开任务 `c0ceba72` 的交付与事件流，确认「已做浏览器自检」与 `ui.check_passed`。
2. 新建一个前端任务，故意不提醒它用 browser_check，看验证节点是否打回并把它拉回浏览器。
3. 检查 `file:///C:/Windows/win.ini` 这类地址现在是否被 `browser_check` 拒绝。
