# B7 开发交接单

## 任务与范围

- 目标：用真实多文件任务（shopbench LR10D 商品资料 PDF）在干净工作区复跑，
  记录成功/失败、步数、介入、token、测试、浏览器证据与失败点。
- 已实现：
  - 第一轮复现 0 改动，定位并修复“工作区之外”范围约束误判只读；
  - 上传 PDF 附件，创建并运行 `0f0e423f-1afe-47a1-8367-3c61e6ef6c1e`；
  - 保留工作区 `_bench_workspaces/b7-shopbench-ours` 与 evidence；
  - 独立启动生成 app，用 `browser_probe.mjs` 复核 13 个动作/断言。
- 明确未实现：
  - 除 `directives.py` 作用域误判外，未改其他主仓库产品代码；
  - 没有跑 Claude/Codex 同题对照；
  - 没有修 B7 暴露的 UI 闸门/预算问题（留给 B8）；
  - 0 次人工审批（bypass 自动化，不代表正常审批体验）。

## 结果

- 第一轮（修复前）：**succeeded / 0 改动 / 2 步 / 103,978 token**；
  范围约束被误判只读，已修并补回归。
- 第二轮任务状态：**failed**
- 失败原因：`测试未通过：ui_check`
- 步数：26 / 200
- 模型调用：29 次
- token：**1,741,293**（prompt 1,687,905 / completion 53,388 / cached 1,632,256）
- 文件：18 次变更事件 / 12 个不同文件
- 测试：`node --test test/api.test.js` **9 pass / 0 fail**
- 验收：3/3 `covered=true`
- 独立浏览器：13/13 动作/断言通过；唯一控制台错误 `/favicon.ico` 404
- 关键事件：`ui.check_failed` → `repair.started` → `budget_exhausted` →
  `review_result` → `task.failed`

## 修改内容

- 主仓库产品代码（B7 第一轮暴露）：
  - `services/api/src/blank_api/services/agent/directives.py`：新增
    `_OUT_OF_SCOPE`，把“不要修改工作区之外的文件”从事务约束里剔除，
    避免整轮被锁只读；
  - `services/api/tests/test_directives_v496.py`：新增范围约束不是只读的
    回归测试。
- 评测/取证文件：
  - `data/b7_run.mjs`：B7 任务创建与轮询脚本（本轮因 fetch headers timeout
    退出；任务继续跑完，evidence 另行拉取）；
  - `data/b7-evidence.json` / `data/b7-evidence-early.json`；
  - `data/b7-steps.json`、`data/b7-browser.log`、`data/b7-open.png`、
    `data/b7-shot.png`；
  - `docs/b7-shopbench-2026-11-22.md`；
  - `_bench_workspaces/b7-shopbench-ours`（生成物，保留供查看）。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| 第一轮（修复前） | **succeeded / 0 改动** | 2 步 / 104K token；范围约束被误判只读 |
| 指令回归（修复后） | **98 passed** | test_directives 等 6 个文件 |
| B7 第二轮 | **failed** | 26 步 / 29 调用 / 1.74M token |
| `node --test test/api.test.js` | **9 passed** | 生成 app 的接口测试全过 |
| 独立浏览器探针 | 13/13 动作断言通过 | 商品、指标、模式、故障、巡检均可用 |
| 控制台 | **1 error** | `/favicon.ico` 404 |
| UI 闸门 | 失败 | 前端改动后未调用 browser_check；修复被预算截断 |

## 风险与需要审查的点

- 1.5M token 预算不足以支撑这个 12 文件任务；1.74M 实际用量，
  `budget_exhausted` 在 UI 修复前截断。不要简单说“提高预算”就完；要区分
  真实任务预算和 UI 修复保留额度。
- 模型有 `browser_check` 工具却没调用；D2 闸门按设计拦住。下一步应让闸门
  能自修/自动探测，而不是只判失败。
- favicon 404 是独立的零错误闸门问题；生成页面应自带 favicon。
- B7 只有一次运行、一个模型；没有人工审批、没有 Claude/Codex 对照。
- `data/b7_run.mjs` 的 fetch 轮询在服务端长响应时会 headers timeout；
  证据已手动保存，但下一轮应加重试/超时。
- 生成 app 未纳入主仓库，保留在 `_bench_workspaces`。

## 建议人工验收

1. 打开 `_bench_workspaces/b7-shopbench-ours`，`PORT=4174 node server.js`，
   浏览器打开 `http://127.0.0.1:4174/`：确认商品、指标、模式、故障、巡检。
2. 看 `data/b7-evidence.json` 中 `ui.check_failed` 与 `budget_exhausted`
   的顺序，确认失败是“没跑 browser_check + 没预算修”，不是“不会写”。
3. 讨论 B8：UI 闸门自修、favicon、真实任务预算保留额度。

## B10 后续复跑

B8/B9/B10 修复后，B7 同一任务从头复跑
`c328c027-1e79-4d4e-aef3-6605b1ef4719`：**succeeded**，
28 步 / 32 调用 / 1,757,693 token / 16 变更 / `npm test` 17 passed /
browser_check 2 次 / 验收 3/3。B7 的失败分析保留为历史，最新结论以本节和
`docs/b10-browser-check-2026-11-23.md` 为准。
