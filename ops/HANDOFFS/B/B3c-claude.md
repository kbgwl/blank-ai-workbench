# B3c 开发交接单

## 任务与范围

- 已实现：
  - 后端 `services/preview.py`：按项目启动/查询/停止长驻 `node <文件>` 服务；
    随机回环端口、环境变量白名单、Windows Job Object、10 分钟 TTL、进程树回收、日志尾部。
  - `POST/GET/DELETE /api/v1/projects/{id}/preview`，复用项目鉴权与 `mask_path` 脱敏。
  - App 关闭时统一回收预览进程。
  - 前端「预览」Tab：启动/停止、隔离档位、到期时间、沙箱 iframe、日志尾部。
- 明确未实现：
  - Docker 容器长驻 + 端口转发（`BLANK_SANDBOX_ALLOW_LOCAL=0` 时直接 409，不降级）。
  - 跨项目共享预览、预览进程的文件/网络边界内核强制。
  - 真实浏览器点击验收（建议人工按下方步骤走一遍）。

## 修改内容

- `services/api/src/blank_api/services/preview.py`（新增）：会话管理。
- `services/api/src/blank_api/api/routes/preview.py`（新增）：三个端点。
- `services/api/src/blank_api/api/routes/__init__.py`：注册路由。
- `services/api/src/blank_api/app.py`：shutdown 回收。
- `services/api/tests/test_preview_v55.py`（新增）：5 条判据（含真实 node 子进程）。
- `web/src/components/bot/preview-api.ts`、`preview-copy.ts`、`preview-copy.test.ts`（新增）。
- `web/src/components/bot/panel.tsx`：预览 Tab 接真实会话。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_preview_v55.py tests/test_ui_delivery_gate_v492.py -q` | **22 passed** | 含真实 node 启动/复用/停止 |
| `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | **461 passed / 0 failed** | 原 458 + 新增 3 |
| `tsc --noEmit` / `eslint src --max-warnings=0` | 均退出码 0 | 无输出 |
| OpenAPI 检查 | 129 paths | 含 `/api/v1/projects/{project_id}/preview` |
| `scripts/acceptance.py --journey acceptance/kanban.json` | 退出码 0 | 结论：通过：打开有东西，动作有变化，刷新还在 |

## 风险与需要审查的点

- 只支持 `package.json` 的 `scripts.start` 为 `node <文件>`；不执行 npm/任意 shell。
- 只支持读取 `PORT` 环境变量并绑定 `127.0.0.1` 的服务；不读 PORT 会在 12 秒后判启动失败并附日志尾部。
- Job Object 只强制内存/进程数/生命周期；文件与网络边界未由系统强制，界面如实标注。
- `BLANK_SANDBOX_ALLOW_LOCAL=0` 时返回 409 `preview_isolation_unavailable`，不会退回宿主裸跑。
- 预览会话按项目复用；10 分钟 TTL 自动停止；关闭面板不会立即停止（有停止按钮）。
- iframe 用 `allow-scripts allow-same-origin allow-forms`，预览源是独立端口，脚本碰不到主应用存储。
- 未跑完整后端 5000+；本卡相关 22 条通过。

## 建议人工验收

1. 打开 B7 工作区项目，在结果区切到「预览」Tab，点「启动预览」。
2. 确认顶部显示「本机 + Job Object」与到期时间，iframe 能操作页面。
3. 点「新标签打开」，确认同一地址可访问；点「停止」后 iframe 消失。
4. 对一个 `start` 不是 `node <文件>` 的项目点启动，确认返回可读错误，不弹进程。
5. 等 10 分钟（或改小 TTL 测试），确认会话自动回收、端口不再监听。
