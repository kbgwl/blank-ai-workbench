# T-A1 开发交接单

## 任务与范围

- 已实现：
  - **会话级审批授权**：审批卡新增 `scope=session`（「本会话允许这类动作（类别）」）。
    用户当面批准后，同一会话（`thread_ref`）内同类动作不再逐条挂卡。
  - **按会话落盘**：`services/api/src/blank_api/services/session_grants.py`，按
    `thread_id + 动作类别` 记账，默认 24 小时过期（`BLANK_SESSION_GRANT_TTL_SECONDS`
    可改，最小 60 秒），损坏文件 fail-closed；删除会话时清理。
  - **bypass 不产生授权**：`bypass` 档本身只免低风险；会话授权必须由人当面点过。
  - **类别隔离且按命令位置识别**：`permissions.session_grant_category()` 只认命令位置，
    不按正文关键词；删除、推送、迁移按“是否不可逆”拆类；复合命令要求每段同类或无害
    （`pip install x | tee log` 可授权，`pip install x && rm -rf build` 不提供会话档）。
  - **越界执行不提供会话档**：`host_bypass` 是沙箱能力边界，只能逐条批；D3 全量测试
    发现它曾被误加进会话档，已移除并补了回归（见 `test_sandbox_bypass_v115`）。
  - **修驳回反复问**：精确驳回的调用检查前移到不可逆/越界闸门之前，驳回过的同一调用
    直接拒绝，不再反复弹卡。
  - **前端**：`ApprovalScope` 增加 `session`；审批卡警告前缀区分命令档/会话档/工具档；
    「完全访问」文案改为“首次各问一次，批准后本会话同类不再逐条问（默认 24 小时过期）”。
  - **规则同步**：`AGENTS.md` 内核红线第 1、2 条按新语义改写，明确 bypass 不产生授权。
- 明确未实现：
  - 会话授权的撤销 UI / HTTP 端点（存储层已提供 `revoke` / `purge_thread`，删除会话会清理）。
  - 子代理不继承父会话的 session grant（更严，不构成放宽）。
  - 全量 API 测试（约 20 分钟）未跑；只跑了权限/审批相关 216 例。

## 修改内容

- `services/api/src/blank_api/services/permissions.py`：新增 `SCOPE_SESSION`、类别映射、
  `PermissionPolicy.session_grants` 与判定顺序调整。
- `services/api/src/blank_api/services/session_grants.py`：新增会话授权落盘。
- `services/api/src/blank_api/services/agent/runtime.py`：按 `thread_ref` 装载授权；
  `_rehydrate` 回放 `approval.granted` 时恢复类别。
- `services/api/src/blank_api/services/agent/graph.py`：审批卡档位、批准落盘与事件字段。
- `services/api/src/blank_api/services/tools/gateway.py`：网关发卡点同样提供会话档。
- `services/api/src/blank_api/schemas/task.py`、`schemas/team.py`：`scope` 接受 `session`。
- `services/api/src/blank_api/api/routes/threads.py`：删除会话时清理授权。
- `web/src/components/tasks/api.ts`、`web/src/components/team/api.ts`：类型增加 `session`。
- `web/src/components/bot/composer.tsx`、`bot/turn.tsx`、`tasks/timeline-blocks.tsx`、
  `tasks/tasks-view.tsx`：文案与警示前缀。
- `AGENTS.md`：内核红线按新语义改写。
- 测试：`services/api/tests/test_session_approval_grants.py`（新增 20 例）、
  `services/api/tests/test_permission_modes_v11.py`（新增端到端会话授权 1 例，更新卡面断言）、
  `web/src/components/tasks/approval-options.test.ts`、`bot/permission-copy.test.ts`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_session_approval_grants.py ...（共 11 个权限/审批文件）-q` | `216 passed`，退出码 0 | 含新增 20 例；覆盖类别隔离、落盘、过期、损坏、驳回、端到端恢复 |
| `node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"` | `pass 437`、`fail 0`，退出码 0 | 前端全量单测 |
| `node ./node_modules/typescript/bin/tsc --noEmit` | 退出码 0 | 无类型错误 |
| `python scripts/acceptance.py --journey acceptance/kanban.json --page acceptance/fixtures/kanban.html` | 退出码 0 | 打开 3 条断言、12 条动作/持久化记录；结论行通过 |

## 风险与需要审查的点

- **安全边界变化**：会话授权可让不可逆删除、强制推送、破坏性数据库操作在本会话内
  同类免问。第一次仍必须人工批准，按类别隔离、默认 24 小时过期、每次执行仍写
  `permission_rule=session_grant:<category>`。请重点审查这个边界是否可接受。
- **复合命令**：只给“每段同类或无害”的命令发会话档；未知形状一律不发（fail-closed，
  多问一次而不是少问一次）。
- **落盘失败**：本次运行仍生效，`approval.granted` 事件里 `session_grant_persisted=false`，
  下一轮会重新问。
- **全量 API 测试未跑**：已知 `fs_broker` 1 条 + `sandbox_restricted` 3 条在改前就是红的，
  本次未处理。

## 建议人工验收

1. 选「完全访问」，触发一次装依赖，确认卡上有「本会话允许这类动作（安装依赖）」；
   批准后再触发同类安装，确认不再弹卡。
2. 同一会话内分别触发删除、`git push`、数据库迁移，确认类别隔离：批准删除不会
   解锁 `git push`；批准 `git push` 不会解锁 `git push --force`。
3. 新开一条会话，确认之前的授权不生效。
4. 驳回一个不可逆命令，确认模型再次提交同一命令时直接被拒绝，而不是再次弹卡。
