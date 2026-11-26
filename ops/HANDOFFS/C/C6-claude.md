# C6 开发交接单

## 任务与范围

- 已实现：
  - 用原 shopbench 目标（LR10D 商品资料 PDF）在**独立临时工作区**复跑当前版本。
  - 复跑任务 `7ce98397`：27 步，succeeded，11 个文件，`npm test` 18 项通过；
    原 `a69c01d2` 的 10706 字符 write_file 截断没有重演。
  - 复跑交付自述「前端导航点击无反应，五个视图进不去」——C1 的未完成词表因此
    补上「无反应 / 没反应 / 进不去 / 打不开 / 无法打开 / 不工作 / 不显示 /
    未显示 / 未生效 / 没生效」与「重跑 / 重新跑 / 再跑一遍 / 手动确认 / 手工确认」。
  - 创建修复任务 `37032c7b`：Agent 自己改 `public/app.js` 的 `switchView`，
    32 步 succeeded。
  - 独立复核（`services/api/scripts/browser_probe.mjs`）：
    - `file://`：16 条导航/独占断言全过；14 条控制台错误均为 `file://` 下
      fetch `/api/*` 的 CORS，页面需要自己的服务端；
    - `http://127.0.0.1:4173`：16/16 断言全过，**0 控制台错误、0 失败请求**，
      商品数据正常加载。
- 明确未实现：
  - 没有把「跑一次」加入 C1 阻塞词表；修复任务仍把 file:// 的复核交回用户，
    本轮只记录事实。
  - 生成的 shopbench 应用默认端口 3000 会与主应用冲突；实测用 `PORT=4173`。
  - 未跑全量后端测试。

## 修改内容

- `services/api/src/blank_api/services/agent/verifier.py`：未完成/交接词表扩展
  （无反应、进不去、重跑等）。
- `services/api/tests/test_delivery_incomplete_v489.py`：新增 shopbench 交付文本判据。
- `docs/capability-baseline-2026-11-21.md`：记录复跑、修复与独立浏览器复核。
- 临时产物：`data/_shopbench_rerun`（生成的 app，保留供查看）、
  `data/_shopbench_*` 脚本/截图（交接前清理）。

## 验证结果

| 检查 | 结果 |
|---|---|
| 复跑任务 `7ce98397` | succeeded，27 步，11 文件，18 项测试通过 |
| 修复任务 `37032c7b` | succeeded，32 步，1 文件（public/app.js） |
| 浏览器探针 `file://` | 16/16 导航断言通过；14 条 CORS 控制台错误（页面需服务端） |
| 浏览器探针 `http://127.0.0.1:4173` | 16/16 断言通过；0 控制台错误；数据正常加载 |
| 新增交付判据 | `test_delivery_incomplete_v489.py` 14 passed（含本组） |
| 合并回归 | 后端 16 个测试文件 `128 passed`；前端 `439 passed`；`tsc` 0；journey 0 |

## 风险与需要审查的点

- 复跑任务和修复任务的状态都是 succeeded；但按新词表，复跑交付（“无反应/进不去”）
  应判 failed。历史状态不改写，后续同类交付会按新规则判。
- 生成的 app 需要服务端；`file://` 模式不可用。正确启动方式：
  `cd data/_shopbench_rerun && $env:PORT=4173; node server.js`，浏览器打开
  `http://127.0.0.1:4173`。默认 3000 与主应用冲突。
- 修复任务交付里的「需要你：…跑一次…确认」是否应阻塞，留待下一轮决定。

## 建议人工验收

1. 打开 `http://127.0.0.1:4173`，点击六个导航项，确认视图切换、数据正常。
2. 确认 C1 对 shopbench 交付文本的判据为 failed。
3. 决定是否把「需要你：…跑一次…」列为阻塞，或改成由 Agent 自己跑完 file:// 复核。
