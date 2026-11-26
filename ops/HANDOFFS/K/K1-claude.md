# K1 开发交接单

## 任务与范围

- 已实现：
  - 后端 `services/learning.py`：经验/偏好快照（含状态、置信度、命中/未中、过期、体检告警），路径脱敏。
  - 后端 4 个端点：`GET /projects/{id}/learning`、经验退役、偏好退役、偏好采用。
  - `PersonaStore.retire` / `PersonaStore.promote`：按条目所在层写回（项目层优先，全局层单独写）。
  - 设置页「记忆」分区新增「自学习」面板：统计、经验列表、偏好列表（含原话）、体检告警、
    逐条退役与「采用」。
- 明确未实现：
  - 同一 thread 的后续追问自动学习（事件形态未摸清，见 self-learning-design）。
  - 跨项目偏好自动升为全局（需人确认，未做自动路径）。
  - 语义矛盾检测（仍只用字符二元组近似）。
  - 技能提案自动采用（保持人工）。
  - 退役的「恢复」按钮（存储里条目仍在，可手工改回）。

## 修改内容

- `services/api/src/blank_api/services/learning.py`（新增）：快照与操作。
- `services/api/src/blank_api/api/routes/learning.py`（新增）：4 个端点。
- `services/api/src/blank_api/api/routes/__init__.py`：注册路由。
- `services/api/src/blank_api/services/agent/persona.py`：新增 `_mutate` / `retire` / `promote`。
- `services/api/tests/test_learning_v56.py`（新增）：5 条判据。
- `web/src/components/settings/learning-api.ts`、`learning.ts`、`learning.test.ts`、`learning-panel.tsx`（新增）。
- `web/src/components/settings/settings-view.tsx`：记忆分区挂载自学习面板。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_learning_v56.py -q --maxfail=1` | **5 passed** | 快照/退役/提升/全局层/冲突不可提升 |
| `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | **467 passed / 0 failed** | 原 464 + 新增 3 |
| `tsc --noEmit` / `eslint src --max-warnings=0` | 均退出码 0 | 无输出 |
| OpenAPI 检查 | 133 paths | 含 4 个 learning 端点 |
| `scripts/acceptance.py --journey acceptance/kanban.json` | 退出码 0 | 结论：通过：打开有东西，动作有变化，刷新还在 |

## 风险与需要审查的点

- 退役是逐条操作，没有「一键清空」；这是刻意的（信任崩了就全删是错误做法）。
- 「采用」按钮只对 `origin=harvested` 且 `candidate` 的偏好显示；显式来源的候选偏好暂不在界面提升。
- 快照会返回经验/偏好的完整字段（含证据与原话），路径已脱敏；不含宿主绝对路径。
- 偏好的项目层/全局层写回规则：项目层同指纹优先，不会在全局层留下改不到的副本。
- 未跑完整后端 5000+；本卡相关 5 条通过。
- 未做真实浏览器点击验收，建议按下方步骤人工走一遍。

## 建议人工验收

1. 打开设置 → 记忆 →「自学习」，确认统计、经验、偏好、体检四块都出现。
2. 对一条经验点「退役」，确认它从列表消失（勾「显示已退役」能看到，状态为已退役）。
3. 对一条 `从任务描述摘的` 候选偏好点「采用」，确认状态变为「生效中」。
4. 对一条偏好点「退役」，确认状态变为「已退役」。
5. 换一个项目，确认数据随项目切换；没有工作区时提示先绑定工作区。
