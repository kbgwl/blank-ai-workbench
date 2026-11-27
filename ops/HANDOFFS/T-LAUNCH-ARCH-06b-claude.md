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
- 明确未完成：
  - **全量验证**：后台全量 `pytest -q` 启动后被系统以“内存不足”停止
    （task `bwn64hh09`，日志 `logs/pytest-fk-2026-11-27.log` 只到 11%，期间
    0 个 FAILED）。这不是测试失败，但也不足以宣称全量已通过。
  - 真机创建/删除任务、线程、团队、项目的级联行为验收。

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
| 全量 `pytest -q` | **被系统停止（内存不足）** | 只到 11%，0 个 FAILED；`logs/pytest-fk-2026-11-27.log` |
| `git diff --check` | **0** | 无空白/格式错误 |

**结论：** 定向验证覆盖了首轮 63 failed 中所有已知外键红项；全量尚未完成，
不能宣称“外键开启后全量归零”。

## 风险与需要审查的点

- **未全量验证**：这是当前最大风险。外键开启会改变 SQLite 的插入/删除行为，
  定向通过不代表没有其他路径受影响；全量重跑前不要把 ARCH-06b 标成 DONE。
- **真实级联**：`ON DELETE CASCADE` / `SET NULL` 现在会由数据库执行；路由层若
  已有手动删子表的代码，需要真机确认没有重复删除或意外清空。
- **测试夹具的显式父行**：这是测试侧修正，不代表生产路径都已按同一顺序写；
  全量测试与真机任务都是必要证据。
- **孤儿行用例**：用独立 `sqlite3` 连接关闭外键造数，仅测试用；产品连接始终保持
  `foreign_keys=ON`。内存库下该用例 skip。
- **内存**：本机当前内存紧张，后台全量被系统回收；不要在内存未恢复时反复启动全量。

## 建议人工验收

1. 内存恢复后，在非提权终端重跑全量：
   `cd services/api && TEMP=E:\projects\kbgai\kbgai\.tmp-test .venv/Scripts/python.exe -m pytest -q`
   预期除 3 项 browser_check 环境红外 0 failed。
2. 真机创建/删除一个任务、一个线程、一个项目，确认没有 `FOREIGN KEY constraint failed`，
   且级联删除符合预期（任务删除后事件/工具调用/运行台账一并清理）。
3. 真机确认 `PRAGMA foreign_keys` 为 1，WAL 文件仍正常生成。
4. 团队轮次/审批恢复路径再走一遍，确认没有因为外键改变出现新的 500。
