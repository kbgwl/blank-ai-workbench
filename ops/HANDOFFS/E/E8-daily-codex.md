# E8 日常场景对标 Codex 开发交接单

## 任务与范围
- 已实现：
  - 日常场景提示词按 Codex 的通用 agent 口径收紧四条：直接动手少问（一次只问一个）、
    先用最简单可靠的做法（标准库优先、不反复装依赖）、做完必须核对结果、
    交付先给结论再给清单；涉及代码/页面时验证下限不降。
  - 日常场景默认用简体中文交付。
  - 新对话默认场景从「编程」改为「日常」：首屏与输入框工具条直接进入日常；
    用户存过的场景选择仍然优先（切到编程会持久化）。
- 明确未实现：
  - 后端 `DEFAULT_SCENARIO` 仍是 `dev`（API/历史任务回落不变）；只有产品 UI 默认改为日常。
  - 没有跑 Codex 同题对照（Codex `exec` 入口在 Windows 上仍不稳定，见
    `docs/capability-benchmark-2026-11-22.md`）。

## 修改内容
- `services/api/src/blank_api/services/agent/scenarios.py`：DAILY.system_prompt 扩写。
- `web/src/components/bot/bot-app.tsx`：`scenarioKey` 初始值 `dev` → `daily`。
- `services/api/tests/test_scenarios_v153.py`：新增四条 Codex 纪律断言。

## 验证结果
| 命令 / 操作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_scenarios_v153.py tests/test_doing_tasks_prompt_v167.py -q` | 24 passed | 场景与提示词 |
| 前端单测 | 481 passed / 0 failed | |
| 前端 tsc / eslint | 退出码 0 | |
| 浏览器探针（新会话） | 断言通过 | 场景选择器显示「日常」，console 0 |

截图：`data/e11-daily-default.png`。

## 风险与需要审查的点
- 老用户若曾手动切到「编程」，localStorage 里存着 `dev`，会继续用编程；
  想回到日常需在输入框切换一次。
- 提示词改动从下一轮任务生效；已跑完的任务不受影响。
- 默认场景只改 UI；通过 API 未指定场景的任务仍按 `dev` 跑。

## 建议人工验收
1. 刷新后新开对话，确认输入框左侧场景显示「日常」。
2. 发一个日常任务（如「把这个目录里的文件按类型整理一下」），确认它直接动手、
   做完给结论，而不是先列一堆计划。
