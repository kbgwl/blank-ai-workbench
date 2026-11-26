# B10 开发交接单

## 任务与范围

- 目标：修 browser_check 自身的假失败：长页面点击不滚动、动作后不等异步、
  `assert {selector,min}` 不读 selector。
- 已实现：
  - browser_probe 自动 settle + scrollIntoView + selector/min 断言；
  - browser_check 增加 settle_ms（默认 600）；
  - 工具预算从 22,500 调到 22,700；
  - 单测保持通过。
- 明确未实现：
  - AI 巡检的独立慢接口复核（默认 settle 600ms 可能不够，显式 wait 仍可用）；
  - 完整后端 5000+ 未在 B10 后重跑；
  - 没有 Claude/Codex 对照。

## 修改内容

- `services/api/scripts/browser_probe.mjs`：settle、scrollIntoView、assert selector/min。
- `services/api/src/blank_api/services/tools/browser.py`：settle_ms 参数与 schema。
- `services/api/src/blank_api/services/tools/registry.py`：工具预算 22,700。
- `docs/b10-browser-check-2026-11-23.md`（新增报告）。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| 模型同款无 wait 步骤 | **ok:true** | 5 expect / 7 动作 / 0 console |
| 完整 B7 从头复跑 | **succeeded** | 28 步 / 32 调用 / 1,757,693 token / 16 变更 |
| `npm test` | **17 passed** | 生成 app |
| browser_check | 2 次 | 页面与控制台通过 |
| 验收 | **3/3 covered=true** | 工作台、启动方式、代码完整 |

## 风险与需要审查的点

- 自动 settle 只 600ms；慢接口（AI 巡检）仍可能需要显式 wait，或更高 settle。
- 最终页面独立通用探测点击 AI 巡检后结果未在 600ms 内更新；任务自己的
  browser_check 通过的是页面/主交互，AI 交互的独立复核仍待补。
- 完整 B7 1.76M token，成本高；B10 不解决成本。
- 完整后端未重跑。
- 只验证 Node + Chromium。

## 建议人工验收

1. 打开 `_bench_workspaces/b7-shopbench-ours`，`PORT=4178 node server.js`，
   确认页面、商品档案、实时状态、参数表、AI 巡检。
2. 复跑模型同款无 wait 的 browser_check 步骤，确认 5 expect/7 动作通过。
3. 看 `data/b7-run.json` / `data/b7-evidence.json`，确认 28 步 succeeded、
   17 测试、3/3 验收。
4. 如果要验 AI 巡检，给它更长的 wait（例如 3000ms），不要用 600ms 默认值。
