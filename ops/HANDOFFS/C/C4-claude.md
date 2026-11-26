# C4 开发交接单

## 任务与范围

- 已实现：
  - 审批卡默认有效期从 30 分钟放宽到 **24 小时**（`approval_ttl_seconds()`），
    仍夹在 60 秒 ~ 24 小时之间；`BLANK_APPROVAL_TTL_SECONDS` 可调短。
  - worker 的过期清理跟随同一函数；注释同步更新。
- 明确未实现：
  - 没有做“续期卡片”接口；超过 24 小时仍会收敛为 failed，用户可重试。
  - 没有做“过期前通知”；通知链路目前服务完成/审批出现，不服务临期提醒。
  - 未跑全量后端测试。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：`approval_ttl_seconds()` 默认值 1800 → 86400。
- `services/api/src/blank_api/worker.py`：过期清理注释同步。
- `services/api/tests/test_v163_super_agent.py`：默认值断言更新为 86400。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest test_v163_super_agent.py test_validation_approval_identity_v101.py test_approval_v06.py -q` | `45 passed`，退出码 0 | 含有效期默认值、环境变量夹取、过期判定与端到端审批 |

## 风险与需要审查的点

- 高风险卡片最长可挂 24 小时；部署方可用环境变量调短。
- 24 小时内模型/代码可能变化，审批恢复时仍按四元身份匹配，不匹配会 409。
- 24 小时后的收敛仍是 failed；要“无限等待”需要另做续期/暂停语义。

## 建议人工验收

1. 创建一张高风险审批卡，确认 `expires_at` 是 24 小时后。
2. 设 `BLANK_APPROVAL_TTL_SECONDS=60`，确认卡片 60 秒后过期、worker 收敛。
3. 确认过期后任务给出可读原因和重试入口。
