# T-LAUNCH-ARCH-06b 开发交接单（开启 SQLite 外键 + 测试夹具收口）

## 任务与范围

按 `dev/NEXT-DEV-v5.md` 的 ARCH-06 与 `docs/MASTER-LAUNCH-PLAN.md` 的 SQLite 并发项执行：

- 已实现：
  - `db.py` 的 SQLite connect 事件恢复 `PRAGMA foreign_keys=ON`；WAL /
    `synchronous=NORMAL` / `busy_timeout=10000` 保持不变。
  - 修 18 个测试文件的父子插入顺序与父行缺失：事件、工具调用、运行台账、
    团队消息、审批卡、钩子、worktree、skill 子代理、tool_search、load_balance、
    沙箱网络告警等。
  - `test_artifacts_lifecycle_v152` 的孤儿行用例改用独立 `sqlite3` 连接临时关外键，
    模拟历史库中已存在的孤儿行；内存库下明确 skip，不伪装成通过。
- 全量验证：
  - 首次后台全量被系统以“内存不足”停止（task `bwn64hh09`，日志只到 11%）。
    根因是 browser_check 失败用例遗留 **240 个 headless Chrome**（约 7.7GB）；
    只清理了带 `blank-probe`/`headless` 标记的测试进程（未动用户浏览器）后，
    空闲内存从 1.47GB 回到 3.24GB。
  - 清理后全量 **3 failed / 5163 passed / 24 skipped**（23:54，
    `logs/pytest-fk-b-2026-11-27.log`）；3 项仍为 browser_check 提权临时目录
    ACL 环境红，不是外键回归。
- 明确未完成：
  - 真机创建/删除任务、线程、团队、项目的级联行为验收。
  - `browser_probe.mjs` 的 Windows 进程树回收：`child.kill()` 只杀主进程，
    GPU/renderer/crashpad 会残留；这是内存被吃干的根因，但属于测试工具问题，
    本轮只做了人工清理，未改脚本。

## 修改内容

- `services/api/src/blank_api/db.py`：SQLite connect 事件增加
  `PRAGMA foreign_keys=ON`，注释恢复为“WAL / 外键 / 忙等待”。
- `services/api/tests/test_db_pool_resilience.py`：重新断言
  `PRAGMA foreign_keys == 1`。
- 以下测试补父 `Project`/`Task` 或先 `flush()` 父行：
  - `test_event_usage_lifecycle_v152.py`
  - `test_delivery_snapshots_v153.py`
  - `test_evidence_paging_v152.py`
  - `test_crash_resume_cross_process_v142.py`
  - `test_final_blockers_v112.py`
  - `test_permissions_v10.py`
  - `test_report_findings_v34.py`
  - `test_phase2_team_admin.py`
  - `test_phase2_search.py`
  - `test_sandbox_matrix_v21.py`
  - `test_approval_card_v101.py`
  - `test_hook_events_v52.py`
  - `test_artifacts_lifecycle_v152.py`
  - `test_load_balance.py`
  - `test_tool_search_v13.py`
  - `test_worktree_tools_v32.py`
  - `test_skill_tool_v33.py`
  - `test_sandbox_network_v5.py`

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 已知外键回归子集（13 文件） | **212 passed** | 修复后仅孤儿行用例红 |
| `test_artifacts_lifecycle_v152.py` | **19 passed** | 独立连接造孤儿行后 |
| TypeError 组（8 个用例） | **8 passed** | load_balance / skill 子代理 / tool_search / worktree / sandbox 网络告警 |
| 全量 `pytest -q`（清理残留 Chrome 后） | **3 failed / 5163 passed / 24 skipped** | 23:54；3 项均为 browser_check 环境红；`logs/pytest-fk-b-2026-11-27.log` |
| `git diff --check` | **0** | 无空白/格式错误 |

**结论：** 外键开启后全量除 3 项 browser_check 环境红外 **0 failed**；
ARCH-06b 可以进入人工验收，不再标 BLOCKED。

## 风险与需要审查的点

- **真实级联**：`ON DELETE CASCADE` / `SET NULL` 现在会由数据库执行；路由层若
  已有手动删子表的代码，需要真机确认没有重复删除或意外清空。
- **测试夹具的显式父行**：这是测试侧修正，不代表生产路径都已按同一顺序写；
  全量测试与真机任务都是必要证据。
- **孤儿行用例**：用独立 `sqlite3` 连接关闭外键造数，仅测试用；产品连接始终保持
  `foreign_keys=ON`。内存库下该用例 skip。
- **browser_probe 泄漏（未修）**：Windows 上 `child.kill()` 只杀主进程，
  GPU/renderer/crashpad 会残留；本轮已人工清理 240 个测试 Chrome，但下轮
  browser_check 仍会重复泄漏。已登记为独立待办，不在本卡范围。
- **内存**：清理后全量跑通；不要在残留 Chrome 未清理时反复启动全量。

## 建议人工验收

1. 在非提权终端重跑全量，确认 0 failed（当前唯一红项为 3 个 browser_check
   提权临时目录环境红）：
   `cd services/api && TEMP=E:\projects\kbgai\kbgai\.tmp-test .venv/Scripts/python.exe -m pytest -q`
2. 真机创建/删除一个任务、一个线程、一个项目，确认没有 `FOREIGN KEY constraint failed`，
   且级联删除符合预期（任务删除后事件/工具调用/运行台账一并清理）。
3. 真机确认 `PRAGMA foreign_keys` 为 1，WAL 文件仍正常生成。
4. 团队轮次/审批恢复路径再走一遍，确认没有因为外键改变出现新的 500。
