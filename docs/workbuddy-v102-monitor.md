# v0.10.2 监控（Monitor）

> 版本边界：v0.10.0 的「持续监控聊天」= Chat Supervisor 监听**本产品内**的对话，
> 属于必做项且已实现。**监控外部聊天软件（企业微信 / 钉钉 / 飞书）**在
> `workbuddy-v165-autonomous-super-agent.md` §1.2 里属**可选实现**，本版仍未接通。
> 本文说明这一版做到哪、卡在哪、接下去需要什么。

## 1. 为什么要有这一页

v0.10.0 的监督者其实每收到一条消息都会判一次（answer / execute / continue / approve…），
但判定只落在 `agent_events` 里。用户在界面上看不到，于是「持续监控聊天」这条能力
等于隐形——问「在哪监控」时只能回答「没有这个入口」。

这一页把四件事摆到一屏：

| 区块 | 回答的问题 | 数据来源 |
| --- | --- | --- |
| 它把每句话判成了什么 | 这句话被当成什么、要不要动手、有没有建任务 | `tasks.goal_spec.supervisor` |
| 正在跑 / 等你处理 | 现在有几件事没结束、卡在审批的有几条 | `tasks.status` |
| 待触发 | 说了「盯着 X」之后登记了什么、它在等什么 | `agent_triggers` |
| 触发源 | 到底能收到哪些事件，聊天软件通没通 | 静态清单 + `is_enabled` |

## 2. 接口

`GET /api/v1/monitor?limit=30`

```json
{
  "now": "2026-11-08T04:04:11",
  "watching": [{ "task_id": "…", "intent": "watch", "reason": "监控意图", "needs_execution": false, "…": "…" }],
  "running":  [{ "task_id": "…", "status": "running", "…": "…" }],
  "pending":  [{ "trigger_id": "…", "waiting_for": "等一个不会到来的事件…", "never_fires": true }],
  "sources":  [{ "id": "wecom", "kind": "im", "connected": false, "…": "…" }],
  "counts":   { "watching": 8, "running": 1, "waiting_approval": 0, "pending": 1, "never_fires": 1 },
  "background_execution": true
}
```

两条口径：

- `watching` 只收**带 supervisor 段**的任务。v0.9.1 的旧轮次没有这段，混进来会让
  「判成什么」一列大面积空着，看着像判定失败。
- `pending[].never_fires` 为 true = `fire_at` 为空且没有对应的事件生产者。
  `triggers.due()` 只捞 `fire_at <= now`，所以这类永远等不到。界面必须说破：
  「登记了却永远等不到」比假装在盯更糟。

`POST /api/v1/triggers/{id}/fire` 可以把一条 pending 现在放行（走 worker 到点放行的同一段逻辑）。

## 3. 本版修掉的两个断点

「盯着 X，一旦有变化就告诉我」这句话在真机上是**完全无效**的，断在两处：

1. `threads.create_turn` 的自动启动条件是 `auto_start and judgement.needs_execution`，
   而 watch / schedule 的判定恰恰是 `needs_execution=False`（语义是「现在别动手」）。
   于是 defer 分支根本不进——任务既不跑，也不登记。
2. 即使进了 `_defer_if_needed`，它只读 `task.dispatch_snapshot`；而对话轮次从不写这个
   字段，只写 `goal_spec.dispatch`。读到 `None` 就直接放行。

修法（`api/routes/tasks.py::_defer_intent` + `api/routes/threads.py`）：

- 新增 `_defer_intent(task)`：有快照走快照，没有就看 `goal_spec.dispatch.intent`，
  **只认 `watch` / `schedule`**，不放宽其它意图；
- 自动启动条件改成 `auto_start and (needs_execution or _defer_intent(task))`。

修完的真机读数：任务停在 `draft`，`agent_triggers` 里留下
`event_type=schedule / fire_at=NULL / status=pending`，监控页显示
「永远等不到：没有到点时间，也没有对应的事件源」。

## 4. 触发源：现在能收到什么

| 源 | 状态 | 说明 |
| --- | --- | --- |
| `user_task` | 已接通 | 监督者每收到一条就判一次 |
| `task_failed` | 已接通 | 失败可自动开一轮修复 |
| `approval_expired` | 已接通 | 超时未批回到对话 |
| `workspace_changed` | 已接通 | 需要 watch 任务指定路径 |
| `schedule` | 已接通 | **只有带 `fire_at` 的才会被扫到** |
| 企业微信 / 钉钉 / 飞书 | **未接通** | 只留了触发源位置，读不到任何消息 |

## 5. 接 IM 的真实门槛

桌面版没有公网地址，**入站消息只能靠应用主动连出去**。三个渠道的可行性差别很大：

| 渠道 | 入站方式 | 桌面版可行性 | 需要的凭据 |
| --- | --- | --- | --- |
| 钉钉 | Stream 模式（长连接，主动连出去） | ✅ 可行 | `appKey` / `appSecret` + 机器人订阅 |
| 飞书 | 事件订阅「长连接」方式 | ✅ 可行 | `app_id` / `app_secret` + 订阅 `im.message.receive_v1` |
| 企业微信 | 自建应用「接收消息」需要**公网回调 URL** | ⚠️ 只能做出站（主动发消息），收消息需要公网或中转 | `corpId` / `agentId` / `secret` + 公网 https 回调 |

所以真要接，建议顺序是 **钉钉 / 飞书 → 企业微信**。每接一个都要落三件事：
凭据存储（加密）、事件适配（把 IM 消息转成 `user_task` 事件）、真机联调
（发一条真实消息走完整链路）。只有前两件没有第三件，等于没接。

## 6. 这一版没做

1. **IM 三个渠道的适配器**：只有清单上的「未接通」，没有任何收发代码。
2. **`fire_at` 的写入**：定时任务（「每天九点」）目前没有真正把时间写进触发器，
   登记出来的永远是 `fire_at=NULL`。
3. **轮询之外的推送**：页面 5 秒拉一次，没有 WebSocket。
4. **桌面安装版未重新打包**：本版改动在源码与 web 构建里，`D:\Softwares\kbg\…`
   里仍是 0.10.1 的包。

## 7. 验证读数

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest -q` | **2225 passed / 23 skipped**（881s，新增 9 例） |
| 后端监控接口 | `pytest tests/test_monitor_v102.py` | 9 passed |
| 派发相关子集 | `pytest tests/test_backend_autostart_v101.py tests/test_dispatch_v162.py tests/test_triggers_v162.py tests/test_v165_supervisor.py tests/test_v163_integration.py` | 65 passed |
| 前端单测 | `pnpm test:web` | 296 passed |
| 类型 / lint | `pnpm typecheck:web` / `pnpm lint:web` | 0 错 |
| 构建 | `pnpm build:web` | 14 路由（含 `/monitor`） |

截图：`docs/acceptance/v102/`（亮色、深色、左栏入口、从入口进入）。
