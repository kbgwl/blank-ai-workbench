# T-LAUNCH-ARCH-05 开发交接单（worker 有界并发）

## 任务与范围

按 `docs/MASTER-LAUNCH-PLAN.md` Phase 2 的 `ARCH-05` 执行：

- 已实现：
  - `worker.run_once()` 从串行 `for` 循环改为
    `asyncio.Semaphore(BLANK_WORKER_CONCURRENCY)` 有界并发（默认 2）；
    每条 run 的认领、`run.started`、入参重建、执行、失败记账仍走原函数，
    只改变调度。
  - `_run_candidate()` 抽出单条候选的完整处理；认领成功即计入 `handled`，
    即使后续重建入参/执行失败也不会被当成“没发生过”。
  - 同一轮中一条候选的意外异常被 `guarded` 捕获并记录，不会取消同一轮的
    其他 run；下一拍仍按 attempts/租约重新判定。
  - 环境变量 `BLANK_WORKER_CONCURRENCY` 可调并发数；非法值回落 2。
- 附带修复（测试工具，独立提交）：
  - `browser_probe.mjs` 在 Windows 上按本轮唯一的 `--user-data-dir` 清理
    所有 Chrome 进程（GPU/renderer/crashpad），不再依赖 `child.kill()` /
    `taskkill /T` 的进程树快照。此前一次失败的全量可留下 240 个 headless
    Chrome、约 7.7GB 内存。
- 明确未实现：
  - `ARCH-04` AgentRun 1:N、`ARCH-01/02` 拆分、`ARCH-03` 统一入口、
    `ARCH-07` 团队任务入库。
  - 3 项 `browser_check` 环境红（提权 pytest 临时目录 ACL）。

## 修改内容

- `services/api/src/blank_api/worker.py`：
  新增 `_concurrency()` / `CONCURRENCY`、`_run_candidate()`；`run_once()`
  改用 semaphore + `asyncio.gather`，并加候选异常隔离。
- `services/api/tests/test_worker_v90.py`：
  新增 `test_two_candidates_run_concurrently`，断言两条 run 在第一条结束前
  都已开始。
- `services/api/scripts/browser_probe.mjs`：
  Windows 下按 `profile` 路径过滤并清理 Chrome 进程。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `test_worker_v90 + test_worker_recovery_v152 + test_run_lease_v102` | **22 passed** | 认领/续跑/租约/并发 |
| worker 相邻调度 5 文件 | **53 passed** | dispatch/trigger/watchdog/v163 |
| worker + 调度 8 文件（加异常保护后） | **75 passed** | 最终代码 |
| 全量（排除 3 项 browser 环境红） | **5164 passed / 24 skipped / 3 deselected** | 20:12，`logs/pytest-worker-2026-11-27.log` |
| browser_check 单跑 + 进程残留计数 | **12 passed / 3 env failed；残留 0** | 泄漏修复生效 |
| `git diff --check` | **0** | 无空白/格式错误 |

全量排除的 3 项是已记录的 browser_check 提权临时目录 ACL 环境红；与 worker
并发无关。全量运行的是加异常保护前的代码；保护只影响异常路径，之后 75 项
worker 定向测试全绿。

## 风险与需要审查的点

- **默认并发 2**：这会改变生产 worker 的同时执行数。SQLite 已开 WAL /
  busy_timeout；如果某供应商有严格并发限制，可用 `BLANK_WORKER_CONCURRENCY=1`
  恢复串行。
- **候选展开**：一次 `run_once` 会为所有候选创建协程，semaphore 控制同时执行；
  队列极长时会多占一点内存，但不会同时启动全部 run。后续可限制单轮候选数。
- **共享状态**：`_execute_claimed_run`/run_manager 是否完全并发安全，全量通过是
  当前证据；真机多任务并发仍需观察任务状态、事件序号与审批恢复。
- **browser_probe 清理**：用 PowerShell `Get-CimInstance` 按 profile 过滤，启动
  约几百毫秒；只杀带本轮 profile 的 Chrome，不动用户浏览器。清理失败不影响
  本次检查结论，下一次按新 profile 再清。
- **3 项 browser 环境红**：提权运行 pytest 时临时目录 ACL 收紧，Chrome 非提权
  沙箱读不到页面；非提权终端复跑应转绿。

## 建议人工验收

1. 真机同时提交两条普通任务，确认两条都在跑、互不阻塞，且都能写终态。
2. 一条长任务 + 一条短任务：短任务不应等长任务结束。
3. 把 `BLANK_WORKER_CONCURRENCY=1` 复跑一次，确认行为回到串行。
4. 连续跑几次 browser_check（失败也可以），用
   `Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"` 确认没有
   `blank-probe`/`headless` 残留。
5. 非提权终端复跑全量，确认 3 项 browser_check 环境红转绿。
