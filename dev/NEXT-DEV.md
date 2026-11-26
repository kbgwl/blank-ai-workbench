# T-BA 下一步开发计划

> 来源：2026-11-24 Codex 质量验收（`ops/HANDOFFS/T-BA-codex.md`）
> 状态：**已完成（2026-11-24 整改；P2 提示按 A 方案实现）**
> 关联任务：T-BA 会话批量归档
>
> 整改备注：代码核对确认 `DELETE /threads/{id}` 只把 `status` 改成 `archived`，
> 不物理删除；前端单条归档仍改为显式 `PATCH`，物理删除只走
> `DELETE /threads/{id}/permanent`。P0 的「数据被静默销毁」判断不成立。

## 一、背景

T-BA 会话批量归档已完成开发并进入质量验收，验收发现 4 项问题：

| 编号 | 严重度 | 问题 |
|---|---|---|
| 1 | P0 | 单条「归档」误调 `DELETE /threads/{id}` 物理删除会话与全部轮次，与弹窗「已有的轮次全部保留」承诺相悖 |
| 2 | P1 | 交接单与代码注释称「批量归档与单条 DELETE 语义一致」，描述错误（DELETE 是物理删除） |
| 3 | P2 | 后端测试 `test_thread_list_filters_by_status_before_limit` 组合运行时失败（排序不稳定） |
| 4 | P2 | 已归档超过 200 条只展示最近 200 条，页面无提示（已知限制，需产品确认） |

## 二、修复任务（按优先级）

### P0：修复单条「归档」误删数据

**现象**：`web/src/components/bot/bot-app.tsx` 的 `archive` 调用 `archiveThread`
（`DELETE /threads/{id}`），后端该接口**物理删除**会话、全部轮次任务、事件、检查点，
不可恢复；弹窗却承诺「已有的轮次全部保留」。

**修复**：
1. `web/src/components/bot/bot-app.tsx`：`archive` 改为调用
   `updateThread(thread.id, { status: "archived" })`（PATCH），保留轮次、可取消归档，
   与批量归档同语义。
2. `web/src/components/tasks/api.ts`：`archiveThread` 改为 PATCH `/threads/{id}`
   并带 `{ status: "archived" }`，或直接在调用点改用 `updateThread`；
   同步修正函数注释「归档，不是删除」。
3. 保留 `deleteThread`（`DELETE /threads/{id}/permanent`）作为真正删除入口，
   文案「删除后不可恢复」不变。

**验收**：
- 点单条「归档」→ 弹窗确认 → 会话移入「已归档」组，历史轮次完整保留；
- 「取消归档」后可重新开轮次；
- 「删除」入口仍为物理删除，文案不变。

### P1：修正错误语义描述

1. `services/api/src/blank_api/api/routes/threads.py`：`batch_archive_threads`
   docstring「语义与单条 `DELETE /threads/{id}` 完全一致」→ 改为「语义与
   `PATCH /threads/{id}` 改 `status=archived` 一致；物理删除请用
   `DELETE /threads/{id}/permanent`」。
2. `web/src/components/tasks/api.ts`：`archiveThreads` 注释同步修正。
3. `ops/HANDOFFS/T-BA-claude.md`：修正「与单条 DELETE 一致」相关表述，
   在风险点补充「单条归档与批量归档统一走 PATCH 改 status」。

### P2：修复测试排序断言

`services/api/tests/test_threads.py::test_thread_list_filters_by_status_before_limit`：
- 根因：两条活跃会话 `updated_at` 相同，`ORDER BY updated_at DESC, created_at DESC`
  对相等值排序不稳定，组合运行时顺序翻转导致断言失败。
- 修复：给两条活跃会话设置不同的 `updated_at`（如相差 1 分钟），或断言改为集合比较
  （只断言 id 集合与状态，不断言顺序）。
- 复跑：`tests/test_threads.py + test_thread_status_v151.py + test_thread_rewind_v117.py`
  组合必须全过。

### P2（已实现，A 方案）：已归档 200 条上限提示

`web/src/components/bot/sidebar.tsx` 已归档组：达到取数上限时显示
「仅显示最近 200 条已归档会话。」不改接口，不编造「可通过搜索找回」——
当前搜索只过滤已加载的列表，更早记录确实看不到。分页作为后续可选。

## 三、涉及文件

- 修改：`web/src/components/bot/bot-app.tsx`（P0）
- 修改：`web/src/components/tasks/api.ts`（P0/P1）
- 修改：`services/api/src/blank_api/api/routes/threads.py`（P1，docstring）
- 修改：`services/api/tests/test_threads.py`（P2）
- 修改：`ops/HANDOFFS/T-BA-claude.md`（P1，描述）
- 可选修改：`web/src/components/bot/sidebar.tsx`（P2 提示）

## 四、验证命令

```powershell
# 后端定向测试（含组合运行，需全过）
cd E:\projects\kbgai\kbgai\services\api
.venv\Scripts\python.exe -m pytest tests/test_threads.py tests/test_thread_status_v151.py tests/test_thread_rewind_v117.py -q

# 前端单测 + 类型检查
cd E:\projects\kbgai\kbgai\web
E:/env/node/node-v24.20.0-win-x64/node.exe --test --import ./test-bootstrap.mjs "src/**/*.test.ts"
E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit
```

## 五、完成定义

- [ ] P0 修复：单条归档保留轮次、可取消，与批量归档一致
- [ ] P1 文档与注释全部修正
- [ ] P2 测试组合运行全过
- [ ] 上述验证命令全绿
- [ ] 更新 `ops/STATUS.md`、`TODO.md`、`ops/HANDOFFS/T-BA-codex.md`