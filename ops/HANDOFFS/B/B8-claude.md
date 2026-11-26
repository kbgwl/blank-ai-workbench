# B8 开发交接单

## 任务与范围

- 目标：把前端交付闸门从“预算一尽就失败”改成“保留一次可自修的 UI 修复”，
  并修复 browser_check 自检写法的提示缺口。
- 已实现：
  - `ui_repair_rounds` 与预算外一次 UI 修复；
  - 保留修复回合允许调用 browser_check/run_shell，而不是“不要再调用工具”；
  - `ui_repair_reserve` 过渡解释与 `reserved_ui_repair` 事件字段；
  - browser_check 的 expect/steps 提示（初始元素 vs 点击后元素）；
  - 前端系统提示要求自带 favicon。
- 明确未实现：
  - 最终提示词下从头复跑完整 B7 仍失败：保留回合没有调用 browser_check；
  - 没有做确定性强制（工具级/自动探测），只加了提示与预算保留；
  - 没有跑完整后端 5000+；B8 相关 116 条通过；
  - 没有 Claude/Codex 对照；UI 自检任务 844K token 仍偏贵。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：
  - `AgentState.ui_repair_rounds`；
  - UI 闸门失败时累加；
  - `route_after_validation` 在预算耗尽时仅对 `ui_check` 失败放行一次；
  - 预算耗尽提示区分“保留 UI 修复”与普通“不要再调用工具”；
  - `budget.exceeded` 增加 `reserved_ui_repair`；
  - 前端系统提示增加 favicon 底线。
- `services/api/src/blank_api/services/agent/transition.py`：
  - `run_validation → execute_action` 的 `ui_repair_reserve` 解释。
- `services/api/src/blank_api/services/agent/planner.py`：
  - UI 修复提示补“expect 只放打开时元素，点击后元素放 steps assert”。
- `services/api/src/blank_api/services/tools/browser.py`：
  - browser_check 工具描述同步这条规则。
- 测试：
  - `tests/test_ui_delivery_gate_v492.py`：预算保留一次、第二次不保留、
    非 UI 失败不保留；
  - `tests/test_output_styles_v47.py`：系统提示哈希同步；
  - 过渡测试随 `ui_repair_reserve` 通过。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| B8 相关后端回归 | **116 passed** | UI 闸门 / 过渡 / 提示哈希 / 指令 / 工具预算 |
| B7 全量复跑（提示最终化前） | **failed** | 29 步 / 34 调用 / 1.69M token；保留修复生效，调用 6 次 browser_check，但断言写错 |
| B7 最终提示词下从头复跑 | **failed** | 28 步 / 33 调用 / 1.67M token；保留回合 `reserved_ui_repair=true`，但模型一次 browser_check 都没调用 |
| UI 自检小任务 | **succeeded** | 19 步 / 21 调用 / 844K token；9 expect、6 动作全过，控制台 0 错误。**预算未耗尽、任务书已明确检查顺序，不代表保留修复路径** |
| 独立浏览器复核 | **10/10** | 商品、指标、快捷巡检、发送消息全部通过，0 控制台错误 |

证据：`data/b8-evidence.json`、`data/b8-ui-repair-evidence.json`、
`data/b8-final-evidence.json`、`data/b8-browser.log`。

## 风险与需要审查的点

- 保留修复只允许一次；第二次 UI 失败仍会按预算收尾，这是有意的防无底洞。
- UI 自检小任务成功不代表“保留修复路径成功”：它预算未耗尽，且任务书明确
  写了 expect/steps 顺序；最终提示词下从头复跑完整 B7 仍失败，保留回合没有
  调用 browser_check。
- 目前只有提示 + 预算保留，没有**确定性强制**（工具级/自动探测）；模型两次
  行为不一致，B8 不能算“已把自修做可靠”。
- 844K token 做一次 UI 自检偏贵；B8 只解决交付“差一步”，不解决大任务成本。
- favicon 是提示词底线，不是生成器硬编码；仍需 browser_check 实测。
- 浏览器检查的断言质量仍依赖模型；提示已补“初始元素 vs 点击后元素”，
  但没有自动生成断言。
- 本轮未跑完整后端回归，合入前需补。

## 建议人工验收

1. 看 `data/b8-evidence.json`：确认 `ui_repair_reserve` 后确实执行了
   browser_check，而不是纯总结。
2. 看 `data/b8-ui-repair-evidence.json`：确认小任务 succeeded、9 expect/6 动作。
3. 在界面发一个前端小改动任务，确认预算耗尽时还能保留一次 UI 修复。
4. 看 `data/b8-final-evidence.json`：确认最终提示词下完整 B7 仍失败，
   保留回合没有调用 browser_check；下一步应做确定性强制，而不是继续加提示。
