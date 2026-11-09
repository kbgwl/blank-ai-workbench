# 变更日志

## v0.11.2 — 收口「说真话」，接通更新源（2026-11-09）

发布结论：本版不是新功能，是把「看起来通过」逐条换成「真的对得上」。安装包按修复后的源码重打，
提交 `2bd45d8`，已同步到远端 `main`。

- **审批身份闭环**：校验通道与跨恢复续跑同样按 `approval_id + tool_call_id + tool_name + arguments_hash`
  四元匹配，错一个即 409，不再留「旧卡复用」的口子。
- **读图不再静默降级**：`deepseek-flash` 被正确认出支持图片，附件随 `/turns` 进上下文；不支持时明说原因。
- **卡住时说清楚**：任务超时一次打印 13 项现场（任务/运行/派发/供应商/最近事件/解释器/租约…）。
- **触发器边界**：暂停/取消后的状态与按钮一致，watch 根目录之外永不放行。
- **自动修复说真话**：修不好时不再报成功，`verdict` 按实际复核结论给。
- **代理变量不再炸掉所有模型调用**（本版新修）：`NO_PROXY` 里带方括号 IPv6（`[::1]`，curl 的写法）时
  httpx 会把它拼成非法 URL，构造客户端即抛 `InvalidURL: Invalid port: ':1]'`——代理一次没用上，
  所有模型/通道调用先全炸。入口处把方括号归一（httpx 会给裸地址补回方括号），绕过语义不变；
  回归用真子进程钉住，摘掉归一那行即红。因 `src/` 有改动，安装包重打。
- **更新源接通**：`pnpm feed:update` 按壳端契约生成 `latest.json`（版本 + http(s) URL + 64 位 sha256），
  壳端纯函数自检 8 项 + HTTP 全链路 10 步验通；默认不联网，须配 `BLANK_UPDATE_FEED_URL`，安装须用户点击。
- **验收读数**：后端 **2325 passed / 0 failed / 23 skipped**（633.50s，`EXIT=0`）；前端 296 passed，
  `tsc`/lint/构建 `EXIT=0`；安装包自检 **44 项全 ok**；版本一致性 6/6；安装包 sha256 `3746586eb260…`。
  逐条工件见 [docs/v0.11.2-release-artifacts.md](docs/v0.11.2-release-artifacts.md)。
- **仍未验证（如实列出）**：CI 未在真 GitHub runner 跑过；#76 第二台机器差异验证未做；
  签名证书未采购（产物 `NotSigned`）；更新源未托管到公网。评测 benchmark 55 例通过率 71%（3 例假完成、1 例未授权高风险）。

## v0.11.1 — 通知通道：「到点了」真的会弹出来（2026-11-08）

- **原生系统通知**：桌面壳直接弹 Windows toast，点一下唤回窗口并跳到那条对话（`?thread=` 深链）。
- **判定仍在后端，壳只负责投递**：要不要打扰由 `agent/notify.py` 判（策略 + 免打扰），结论连同理由写进
  `notify.decided` 事件；`GET /api/v1/notifications` 只吐 `disturb=true` 的那几条，壳不复判。
- **文案按来源分档**：定时起的弹「到点了」，盯梢的弹「有变化了」，审批弹「等你批准」（并出声）。
- **三条降噪**：按 id 去重、窗口正被看着就不弹、一分钟超过 3 条先压着（审批例外）。
- **免打扰入口**：设置 → 执行与智能体 →「通知与免打扰」，填 `22:00-08:00`（跨零点也行），只压「做完/有变化」。
- 验收记录见 [docs/v0.11.1-notifications.md](docs/v0.11.1-notifications.md)。

## v0.11.0 — 让 Super Agent 真的会醒（2026-11-08）

- **定时真的到点**：不再登记一条永远不响的待触发；到点在桌面形态下自己起跑（以前只入队、没有 worker 认领）。
- **「盯着工作区」有真实的事件生产者**：每拍扫一次文件 mtime。
- **消息通道做成配置项**：企业微信/钉钉/飞书在「设置 → 消息通道」填完即用。
- **自动更新接上真实下载与摘要校验**：默认不碰网络，须配 `BLANK_UPDATE_FEED_URL`。
- 配置与边界见 [docs/channels.md](docs/channels.md)，唤醒链路验收见 [docs/v0.11.0-wake-up.md](docs/v0.11.0-wake-up.md)。

> v0.9.1 / v0.10.0 当时未在此文件立条目，说明见 README「更早 / 再上一版」段与 `docs/` 下对应验收文档。

## v0.9.0 — Super Agent Upgrade（2026-11-03）

发布结论：55 项 coding 基线真实跑通（NVIDIA Mistral Large 4.1T，独立评测后端），completionRate 91% / passRate 98% / testPassRate 100% / misEditedFiles 0 / falseDone 0 / falseClaim 0 / unauthorizedHighRisk 0，达全部硬性发布门槛。

- **运行时升级**：Router/Worker 双层架构落地，新增事件触发、后台执行、Watchdog、断点恢复与低打扰交付。
- **触发器与调度快照**：新增 agent 触发器（triggers）与 dispatch snapshot，配套 alembic 迁移 `0031_dispatch_snapshot.py`、`0032_agent_triggers.py`。
- **意图三分法 + 双重预算**：answer/advise/act 判定与步数 + token 双重预算落地，闲聊不再被误判为改代码任务。
- **交付闸门与空转断路器**：写盘前事实闸门（unknowns）与 `_enforce_no_false_claim` 交付闸门上线；空转断路器拦三类浪费回路，拦截用独立 `blocked` 状态、不冒充 `denied`。
- **评测修复**：`run.mjs` 对供应商 429/529 限流归 `provider_failed` 走退避重试（此前静默记 `setup_failed` 漏跑）；评测库 NVIDIA provider 失效 key 已更新。
- **桌面包**：同步 alembic `0031/0032` 迁移，`verify:desktop` 全绿。
- **已知残留**：coding-035 为 fixture 前提与评测口径错位（agent 满足书面 acceptance 但未触碰预期文件），不属能力回退，列入后续整改（修 fixture 或放宽 `expectedFilesTouched` 与 acceptance 的 AND 关系）。
- **未验证项**：真实 Electron GUI 18 项取证因 headless 环境限制待真机补（已如实标注，未冒充）。

## v0.1.0

- 初始 tag（基线）。注：0.1.0–0.8.x 期间为未打 tag 的迭代构建。
