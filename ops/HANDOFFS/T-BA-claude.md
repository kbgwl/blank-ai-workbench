# T-BA 开发交接单：会话批量归档

## 任务与范围

- 已实现：
  - 会话侧栏「管理」选择态：活跃会话复选框、整批一次确认、底部操作条
    （已选 N 条 / 全选 / 取消 / 归档所选（N））。
  - `POST /api/v1/threads/batch-archive`，返回 `archived / unchanged / missing`；
    去重、空数组与超过 50 条 422、幂等。
  - `GET /threads` 与 `GET /projects/{id}/threads` 支持
    `status=all|active|archived`，先过滤再 limit，默认 all。
  - 侧栏「已归档 N」折叠组、搜索自动展开、已归档行菜单「取消归档」。
  - 已归档列表读取失败只影响已归档组，活跃列表照常显示。
  - 首页、全站左栏、任务页会话选择器只取活跃会话。
  - 修复 `updateThread` 缺 `content-type: application/json` 导致
    PATCH 取消归档/重命名 422 的既有缺陷。
- 明确未实现：
  - 不做批量取消归档、批量重命名、批量删除、归档中心页面。
  - 不归档任务（任务没有 archived 状态），不停止/取消运行中任务。
  - 不改数据库结构、不加迁移、不改权限/安全/沙箱。
  - 已归档超过 200 条只展示最近 200 条，不解决分页。

## 修改内容

- `services/api/src/blank_api/schemas/thread.py`：新增 `ThreadBatchArchiveIn`
  （`ids` 1–50 条）。
- `services/api/src/blank_api/api/routes/threads.py`：两个列表接口新增 `status`
  筛选（先过滤后 limit）；新增 `POST /threads/batch-archive`，一次查询一次提交。
- `services/api/tests/test_threads.py`：新增 6 条测试，覆盖先过滤后 limit、
  三桶结果、幂等、去重、空/超限 422、归档不停止运行中任务。
- `web/src/components/tasks/api.ts`：`fetchAllThreads({limit,status})`、
  `fetchThreads(projectId,{limit,status})`、`archiveThreads(ids)`、
  `BatchArchiveResult`；`updateThread` 补 JSON content-type。
- `web/src/components/bot/bot-api.ts`：`loadConversations()` 并行取活跃 50 条与
  已归档 200 条，合并去重排序；活跃失败才抛错，已归档失败返回 `archivedError`。
- `web/src/components/bot/batch-archive.ts`（新）：分区、可选判定、切换选择、
  全选、未结束计数、确认文案、结果汇总等纯函数。
- `web/src/components/bot/batch-archive.test.ts`（新）：9 条纯逻辑测试。
- `web/src/components/bot/sidebar.tsx`：管理态、复选框、操作条、已归档折叠组、
  搜索联动、Escape/收起退出、取消归档菜单、可访问性标签。
- `web/src/components/bot/bot-app.tsx`：`batchArchive` 接线；成功提示、失败抛回
  侧栏保留管理态；当前会话被归档时切新对话；只刷新一次 `threadsNonce`。
- `web/src/components/app-rail.tsx`、`web/src/components/tasks/task-list-pane.tsx`：
  会话列表只取 `status=active`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `.venv/Scripts/python.exe -m pytest tests/test_threads.py -q` | 通过 | 28 passed |
| `.venv/Scripts/python.exe -m pytest tests/test_thread_status_v151.py tests/test_thread_rewind_v117.py -q` | 通过 | 14 passed |
| `node ./node_modules/typescript/bin/tsc --noEmit` | 通过 | 退出码 0 |
| `node ./node_modules/eslint/bin/eslint.js src --max-warnings=0` | 通过 | 退出码 0 |
| `bash check.sh --no-smoke` | 通过 | 前端 **495 passed**、tsc/eslint 0、journey 通过 |
| `scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html` | 通过 | 断言全过，`结论：通过：打开有东西，动作有变化，刷新还在`，exit=0 |
| 浏览器探针 1440×900 | 通过 | 26 步骤全过，console 0、失败请求 0 |
| 浏览器探针 1024×768 | 通过 | 9 步骤全过，侧栏无横向溢出，console 0 |

截图：

- `data/batch-archive-managing-1440.png`：管理态复选框与底部操作条。
- `data/batch-archive-archived-1440.png`：已归档组展开与成功提示。
- `data/batch-archive-1440.png`：搜索 + 全选后只剩匹配会话。
- `data/batch-archive-1024.png`：1024×768 视口。

验收记录：`docs/validation/acceptance-2026-11-21.md` 末尾「会话批量归档（T-BA）」。

## 风险与需要审查的点

- `updateThread` 补 content-type 属范围外但必要的修复：不补则「取消归档」和
  「重命名」都会 422；原 `onUnarchive` 是死代码，此前没有暴露。
- 已归档组计数按最近 200 条计算；更多归档会话不可见。达到上限时页面会提示
  「仅显示最近 200 条已归档会话」，但搜索仍只覆盖已加载的这 200 条。
- 归档会刷新 `updated_at`；取消归档后会话回到活跃列表顶部，属已知产品行为。
- 单条归档与批量归档统一走 PATCH 改 `status=archived`；`DELETE /threads/{id}`
  只是兼容旧客户端的归档别名（后端也只改 status），不删任何行。物理删除只有
  `DELETE /threads/{id}/permanent` 一个入口。
- 批量接口按 id 归档，不额外限制 `kind`，与单条 PATCH 归档一致。
- 失败时错误由 `bot-app` 的 `actionError` 显示，侧栏保留管理态与选择；
  探针未模拟 500/429 的界面行为，只有逻辑分支与单测覆盖。

## 建议人工验收

1. 打开首页，确认侧栏「最近」右侧有「管理」。
2. 点「管理」→ 勾选 2–3 条 → 确认框只弹一次且说明未结束任务不会停止 →
   确认后只发一次 `POST /threads/batch-archive`。
3. 刷新页面，确认这几条在「已归档」组，`GET /threads?status=active` 不返回它们。
4. 已归档行菜单点「取消归档」，确认回到活跃分组，并可继续发送一轮。
5. 在 1440×900 与 1024×768 下检查操作条按钮完整可见；用 Tab/Escape 检查键盘操作。

## 质量验收整改（2026-11-24）

### 事实核对

Codex 验收单的 P0 结论「单条归档走 `DELETE /threads/{id}` 会物理删除」与当前
代码不符：该接口在 `threads.py` 里只把 `status` 改成 `archived`，不删线程、
不删轮次任务、不删事件/检查点；物理删除是独立的
`DELETE /threads/{id}/permanent`（前端 `deleteThread` 才走它）。因此本次整改
不涉及数据恢复，也没有发生「归档即销毁历史」。

### 已修改

- `web/src/components/tasks/api.ts`：`archiveThread` 从 `DELETE /threads/{id}`
  改为显式 `PATCH /threads/{id}` 带 `{status:"archived"}`；清理注释，明确物理
  删除只走 `deleteThread`。
- `web/src/components/bot/bot-app.tsx`：单条归档直接调用
  `updateThread(thread.id, {status:"archived"})`，与批量归档同语义。
- `services/api/src/blank_api/api/routes/threads.py`：`batch_archive_threads`
  docstring 改为「与 PATCH 改 status 一致；物理删除走 /permanent」。
- `services/api/tests/test_threads.py`：给两条活跃会话设置不同 `updated_at`，
  消除同秒 `created_at` 导致的排序不稳定。
- `web/src/components/bot/batch-archive.ts` / `sidebar.tsx`：已归档列表达到
  取数上限（200）时，在「已归档 N」下显示「仅显示最近 200 条已归档会话。」。
  产品确认选 A，不改接口；分页留作后续。
- 明确不做：已归档分页；搜索也仍然只过滤已加载的 200 条，不跨服务端搜更早记录。

### 复跑

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_threads.py tests/test_thread_status_v151.py tests/test_thread_rewind_v117.py -q` | 通过 | 42 passed（整改前偶发 1 failed；整改后稳定） |
| `tsc --noEmit` | 通过 | 退出码 0 |
| 前端单测 | 通过 | 492 passed / 0 failed |
| `eslint src --max-warnings=0` | 通过 | 退出码 0 |
