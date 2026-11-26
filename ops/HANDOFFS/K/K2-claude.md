# K2 开发交接单（T-A2 会话授权撤销）

## 任务与范围

- 已实现：
  - `GET /threads/{id}/grants`：列出本会话有效授权（类别、中文标签、授予时间、到期时间）。
  - `DELETE /threads/{id}/grants/{category}`：撤销单类。
  - `DELETE /threads/{id}/grants`：撤销该会话全部授权。
  - `session_grants.revoke_all`：返回实际撤销条数。
  - 会话条显示「授权 N」，展开后逐条撤销或全部撤销；切换会话按 thread 过滤，不带旧会话的授权。
- 明确未实现：
  - 在界面上**新建**授权（仍只在审批卡里点「本会话允许这类动作」）。
  - 撤销动作写审计事件（当前只改授权账本；审批/执行本身仍有事件）。
  - 自定义单条授权的过期时间（沿用全局 TTL）。

## 修改内容

- `services/api/src/blank_api/services/session_grants.py`：新增 `revoke_all`。
- `services/api/src/blank_api/api/routes/threads.py`：3 个端点。
- `services/api/tests/test_session_grants_v57.py`（新增）：4 条判据。
- `web/src/components/tasks/grants-api.ts`、`grants.ts`、`grants.test.ts`（新增）。
- `web/src/components/tasks/thread-bar.tsx`：授权入口、列表、逐条/全部撤销。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_session_grants_v57.py -q --maxfail=1` | **4 passed** | 列表/单撤/全撤/坏文件 fail-closed |
| `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | **469 passed / 0 failed** | 原 467 + 新增 2 |
| `tsc --noEmit` / `eslint src --max-warnings=0` | 均退出码 0 | 无输出 |
| OpenAPI 检查 | 135 paths | 含 `/threads/{id}/grants` 与 `/{category}` |
| `scripts/acceptance.py --journey acceptance/kanban.json` | 退出码 0 | 结论：通过：打开有东西，动作有变化，刷新还在 |

## 风险与需要审查的点

- 授权列表只在有授权时显示入口；0 条时不占位置。
- 会话条在最后一条轮次状态变化后刷新授权，审批刚放行后可能需要再点开一次或等一次刷新。
- 撤销只影响未来判定；正在执行的调用不受影响（与审批语义一致）。
- 存储损坏时 fail-closed：一条授权都不认，宁可多问。
- 未跑完整后端 5000+；本卡相关 4 条通过。
- 未做真实浏览器点击验收，建议按下方步骤人工走一遍。

## 建议人工验收

1. 在一个会话里触发一次「本会话允许这类动作」的审批，批准后会话条出现「授权 N」。
2. 点开「授权 N」，确认类别、到期时间正确。
3. 点单条「撤销」，确认该条消失；再批准一次后点「全部撤销」，确认入口消失。
4. 换一个会话，确认不显示上一个会话的授权。
5. 等授权过期（或改小 `BLANK_SESSION_GRANT_TTL_SECONDS`），确认列表不再显示。
