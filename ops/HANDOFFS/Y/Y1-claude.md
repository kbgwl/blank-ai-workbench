# Y1 开发交接单

## 任务与范围

- 已实现：
  - `GET/PUT /settings/agent` 返回并接受 `budget`（`cost_limit` + `pricing`）。
  - 设置页「执行与智能体 → 成本与预算」：单任务金额上限、价目表 JSON 编辑与本地校验。
  - 前端纯函数 `parsePricingDraft`：小写化档位名、缺省字段补 0、坏 JSON/负数/非对象给出可定位错误。
- 明确未实现：
  - 任务页时间线显示单次 `budget.exceeded` 的 `estimated_cost`（事件已有，前端展示另卡）。
  - 价目表的表格化编辑与供应商预置；当前用 JSON 文本域。
  - 币种选择：`normalize` 只保留 input/cached_input/output，`estimate_cost` 的 currency 恒为空（既有行为，未改）。

## 修改内容

- `services/api/src/blank_api/schemas/settings.py`：新增 `PriceRow` / `BudgetSettings` / `BudgetPatch`，挂到 `AgentSettings` 与 `AgentSettingsPatch`。
- `services/api/src/blank_api/api/routes/settings.py`：`_render` 纳入 `budget`；`_limits` 给出 `cost_limit` 边界。
- `services/api/tests/test_settings_budget_v54.py`：新增 5 条判据（渲染、只改上限保留价目表、整段替换、patch、默认不限）。
- `web/src/components/settings/api.ts`：补 `budget` 类型与 limits。
- `web/src/components/settings/budget.ts`、`budget.test.ts`：价目表解析与 4 条判据。
- `web/src/components/settings/agent-runtime-panel.tsx`：新增「成本与预算」面板；保存后回读服务端归一化值。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_settings_budget_v54.py tests/test_cost_budget_v427.py tests/test_settings_v13.py -q` | **30 passed** | 退出码 0 |
| `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | **447 passed / 0 failed** | 原 443 + 新增 4 |
| `tsc --noEmit` | 退出码 0 | 无输出 |
| `eslint src --max-warnings=0` | 退出码 0 | 无输出 |
| `scripts/acceptance.py --journey acceptance/kanban.json` | 退出码 0 | 结论行正确 |

## 风险与需要审查的点

- 金额闸默认关：本卡只提供入口，不会自动替用户省钱；没配价目表仍不估价、不拦截。
- JSON 文本域对非技术用户不友好；下一步可做结构化表格与常见供应商预置（不能编价格，需用户确认来源）。
- 价目表超过 40 档会被 `normalize` 截断（既有行为）。
- 只跑相关后端 30 条，未跑完整 5000+。

## 建议人工验收

1. 打开设置 → 执行与智能体 →「成本与预算」，把上限设为 1、价目表填 `{"default":{"input":2,"cached_input":0.5,"output":8}}`，保存。
2. 检查 `.blank/settings.json` 的 `budget` 段与页面回读值一致；把上限改回 0 确认显示「不限」。
3. 跑一个任务并在超限时查看 `budget.exceeded` 事件的 `estimated_cost` / `cost_limit` / `cost_priced`。
4. 点「恢复默认」确认 budget 回到 0 与空价目表。
