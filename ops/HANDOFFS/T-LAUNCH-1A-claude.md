# T-LAUNCH-1A 开发交接单（MASTER-LAUNCH-PLAN Step 1 止血批次）

## 任务与范围

按 `docs/MASTER-LAUNCH-PLAN.md` 第九节 Step 1 执行，先对账再动手：

- 已实现：
  - **BUG-02 输出预算扩容在多供应商下静默失效**：`FallbackAdapter` 增加
    `max_tokens_override` 的读写透传，设置时广播给所有候选适配器，读取以主适配器为真源。
  - **BUG-03 只读场景仍暴露写工具**：`WRITE_TOOLS` 补齐 `write_file` / `edit_file` /
    `notebook_edit`，`brainstorm` 等 `allow_write=False` 场景不再下发这些工具的 schema。
  - **BUG-04 AppContainer 属性号错误**：`boundary.py` 的
    `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 由 `0x00020008` 改为
    `0x00020009`，失败回执也改为按常量打印，并补测试钉住它与
    `windows_appcontainer.py` 的权威常量一致。
  - **ARCH-06（部分）SQLite 并发**：每条 SQLite 连接建立时执行
    `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=10000`。
    `foreign_keys=ON` **没有开启**，原因见下。
  - **连带修复 1：会话/任务插入顺序**：`create_task` 在 `db.add(thread)` 后先
    `flush()`，再创建任务。`tasks.thread_id` 有外键，SQLAlchemy 不会仅凭裸
    `ForeignKey` 保证父子插入顺序；不 flush 时 SQLite 打开外键会报
    `FOREIGN KEY constraint failed`。
  - **连带修复 2：空会话工厂告警**：`runtime._prepare` 在
    `session_factory is None` 时不再写 `sandbox.warning`；对应测试
    `test_runtime_warns_when_network_is_not_enforced` 改用真实会话工厂。
- 已核对、无需重做：
  - **UP-01 写工具只回执**：`editing.py` 已只返回路径与 `added/removed/hunks` 计数。
  - **UP-02 speculative 兼容**：`graph.py` 已用 `getattr(session, "speculative", None)`。
  - **UP-06 `done_when` 阻塞**、**UP-07 Hook 强阻断**：v5 已实现，本轮不重复。
- 明确未实现：
  - **BUG-01 隔离/broker 下读图**：`fs_broker.py` 只有 `read_text_file`，需要新增只读
    二进制/图片通道；留批次 1B。
  - **BUG-05 先起进程再挂 Job**：实际位置是 `sandbox/runner.py`（计划写的
    `sandbox/local.py` 不存在）；需要新的 `CREATE_SUSPENDED → 按句柄挂 Job →
    ResumeThread` 启动路径，`windows_restricted._spawn` 是受限令牌专用，不能直接复用。
    留批次 1B。
  - **ARCH-06 的 `foreign_keys=ON`**：首轮全量开启后 **63 failed**，其中 45 项是测试夹具
    只造 `agent_events` / `tool_calls` / `agent_runs` 子行、不造父任务；另有 14 项是
    `_prepare` 空 `session_factory` 告警（已单独修）。为避免用“测试全绿”掩盖数据完整性
    问题，本轮回退外键开关，转 **ARCH-06b** 单独审计测试夹具后再开。
  - **UP-03 测试归档**：当前全量已基本归零，移动 392 个测试文件属于纯整理，本轮不做。
  - **MASTER-LAUNCH-PLAN Phase 2–4**：架构拆分、前端统一、IM/日常生态等，均不在本批。

## 修改内容

- `services/api/src/blank_api/services/models/routing.py`：
  `FallbackAdapter` 新增 `max_tokens_override` 属性，设置时广播到所有候选。
- `services/api/src/blank_api/services/agent/scenarios.py`：
  `WRITE_TOOLS` 补 `write_file` / `edit_file` / `notebook_edit`。
- `services/api/src/blank_api/services/sandbox/boundary.py`：
  AppContainer 属性号改为 `0x00020009`；失败回执按常量打印。
- `services/api/src/blank_api/db.py`：
  新增 `install_sqlite_pragmas()` 与 connect 事件；SQLite 开 WAL /
  `synchronous=NORMAL` / `busy_timeout=10000`。`foreign_keys` 暂不开启，注释里写明原因。
- `services/api/src/blank_api/api/routes/tasks.py`：
  `create_task` 在创建任务前 `flush()` 会话行，保证父子插入顺序。
- `services/api/src/blank_api/services/agent/runtime.py`：
  无会话工厂时不写 `sandbox.warning`。
- `services/api/tests/test_model_routing.py`：新增预算覆盖透传用例。
- `services/api/tests/test_scenarios_v153.py`：只读场景显式断言三个写工具被禁用。
- `services/api/tests/test_appcontainer_v22.py`：钉住 boundary 与 AC 后端属性号一致。
- `services/api/tests/test_db_pool_resilience.py`：新增 WAL / busy_timeout / synchronous 用例。
- `services/api/tests/test_sandbox_network_v5.py`：告警用例改用真实会话工厂。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 定向 7 文件（改动后首跑） | **105 passed** | 场景、AC、路由、预算、DB |
| 定向 13 文件（回退外键、补空工厂保护后） | **349 passed** | 含此前 14 个 TypeError 用例 |
| 全量第 1 次（开启外键） | **63 failed / 5102 passed / 24 skipped** | 45 个 FK 夹具红项 + 14 个空工厂告警；日志 `logs/pytest-1a-2026-11-26.log` |
| 全量第 2 次（回退外键、补保护后） | **3 failed / 5162 passed / 24 skipped** | 15:56；日志 `logs/pytest-1a-b-2026-11-26.log` |
| `git diff --check` | **0** | 无空白/格式错误 |

**3 个红项（环境限制，非本批代码回归）：**
- `tests/test_browser_check_v427.py` 的 3 个真跑用例：
  `test_a_clean_page_passes_and_leaves_a_screenshot`、
  `test_a_broken_page_is_a_failed_check_not_a_broken_tool`、
  `test_steps_prove_the_page_actually_works`。
- 根因：本机 pytest 以管理员权限运行，pytest 会把 `tmp_path` 收紧为
  owner=Administrators 的保护 DACL；Chrome 以非提权沙箱运行，读不到该临时文件，
  报 `net::ERR_FILE_NOT_FOUND`。
- 对照证据：
  - 同一 `browser_probe.mjs` 打开仓库内 `acceptance/fixtures/kanban.html` 返回
    `ok:true`；
  - 打开 `E:\env\blank-tmp\probe.html` 返回 `ok:true`；
  - 只有 pytest 临时目录下的文件失败；ACL 实测为
    `protected=True`、仅 `OWNER RIGHTS/SYSTEM/Administrators`。
- 结论：`browser_check` 工具本身没坏；这是提权测试环境下 pytest 临时目录 ACL 的限制。
  最终验收建议在非提权终端复跑全量，或接受这 3 项作为已记录环境红。

## 风险与需要审查的点

- **外键后置**：`foreign_keys=ON` 一开就暴露 45 个测试夹具只造子行的问题；生产代码的
  常规路径已有父任务，但测试体系的假数据会先红。ARCH-06b 要逐类补父行，不能简单
  把外键关掉当“已修”。
- **`create_task` 多一次 flush**：会话行在同一个事务里提前落库，不提前提交；任务创建
  失败仍会一起回滚。每建一个任务多一次数据库往返，量级可忽略。
- **`FallbackAdapter` 仍是共享可变属性**：本次只修“设置不生效”，没有把输出预算改成
  每次请求的 `CallOptions`；并发下仍理论上互相影响，MASTER-LAUNCH-PLAN Phase 2 的
  CallOptions 重构仍需做。
- **AppContainer 常量修正会改变真实探测结果**：以前探针恒假，修好后在有 AC 能力的机器上
  可能真的选中 AppContainer；这属于预期行为，但需要真机确认沙箱档位与提示文案。
- **空工厂保护**：只在 `session_factory is None` 时跳过 UI 告警事件；生产路径始终有
  会话工厂，不影响正常事件时间线。
- **浏览器 3 项环境红**：不是代码回归，但会让全量门禁显示 3 failed；如果 CI 以
  “0 failed”为硬门禁，需要在 CI 环境确认是否同样提权，或单独标注。

## 建议人工验收

1. 在**非提权**终端复跑全量：
   `cd services/api && TEMP=E:\projects\kbgai\kbgai\.tmp-test .venv/Scripts/python.exe -m pytest -q`
   预期 0 failed；若仍为 3 项 browser_check，按环境红记录。
2. 真机检查 `brainstorm` 场景：模型工具列表不应出现
   `write_file` / `edit_file` / `notebook_edit`。
3. 多供应商配置下跑一次截断重试，确认 `model.budget_raised` 事件真实生效，
   且备用适配器也拿到放大后的预算。
4. SQLite 并发：确认数据库旁出现 `-wal` 文件；并发写入不再报 `database is locked`。
5. 有 AppContainer 能力的机器上确认 `boundary` 探测不再恒假；没有该能力的机器不要宣称已隔离。
6. 进入批次 1B 前，先确认 ARCH-06b（外键审计）与 BUG-01/BUG-05 的优先级。
