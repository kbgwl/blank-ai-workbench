# T-R2-08 开发交接单（NEXT-DEV-v5）

## 任务与范围

按 `dev/NEXT-DEV-v5.md` 与 `ops/HANDOFFS/T-R2-06-07-codex.md` 退回项开发。

- 已实现：
  - **P0-1 推测执行回归**：`_clear_speculation` / `_take_speculation` /
    `_start_speculation` 对缺 `speculative` 属性的轻量会话（`object.__new__`）
    做 `getattr` 兜底；`test_skill_model_v52` 的源码断言同步为新的
    `bind_tools_stream` 参数形状。6 个 stream 回归全绿。
  - **P1-2 lsp 真实根因**：QA 的全量失败由 **TEMP 落在仓库内** 触发——
    `.tmp-test` 被项目 `.gitignore` 忽略，`git check-ignore` 把临时工作区里的
    **每个文件**都判成 ignored，LSP 定义/引用结果被整批过滤，看起来是
    “No definition found”。修复两层：
    1. 产品层 `_git_ignored` 把工作区根一起送进 `check-ignore`；根自身被忽略时
       不再用上层仓库的 ignore 规则过滤（嵌套被忽略的工作区是合法场景）。
    2. 测试层 `conftest` 设置 `GIT_CEILING_DIRECTORIES=TEMP`，git 不再从
       tmp 目录向上找到项目仓库；「非 git 目录」类用例恢复真实语义。
    同根因的 10 个 worktree 失败一并归零。
  - **P1-3 调用次数**：提示词再收紧——列目录后一轮读完、临时样例与实现同轮写、
    多用例/退出码合并成一条命令、验证出结果后不再人工复核、update_plan 不
    单独占一轮、简单任务 12 轮预算。真机 T09 **12 轮 / 283K / 100 分**。
  - **P1-4 延迟对照**：真机跑对照组。推测执行开/关的 T09 墙钟时间
    **46s vs 47s**，差异在供应商延迟噪声内；未发现回归，也未能证明墙钟下降
    （T09 的只读工具耗时远小于模型流式时间）。
  - **P2-5/6 审批 202**：`test_handoff_e2e_v12`、`test_unknown_facts_e2e_v147`
    的 KeyError 根因是「审批分支与不审批分支响应形状不同」；统一改为
    `settle()` 后 GET 任务终态。`test_unknown_kinds_e2e_v148`、
    `test_stop_failure_hooks_v55` 同批同步。
  - **P2-7 worktree 环境**：见 P1-2 的 `GIT_CEILING_DIRECTORIES`；9 个文件
    **128 passed**（修复前 10 failed）。
  - **P2-8 output_styles**：1 个失败是 1c 提示词扩充后未更新
    `SYSTEM_PROMPT` 哈希；已更新并随 v5 提示词再更新。
  - **P2-11 快照摘要与变更计数**：`FileState` 增加 `summary / writes / added /
    removed`；`read_file` 记录首行摘要，写工具累计本次写入次数与 +/- 行数，
    `render_snapshot` 一并展示并随检查点导出/恢复。快照单测 **7 passed**。
  - **P3-12 全量**：`pytest tests/ -q`（TEMP 在 E 盘 `.tmp-test`）**5154 passed /
    28 skipped / 0 failed**（35m28s）；最终提示词版本的全量复跑见文末。
- 已核对、无需重做：
  - **T-A2 会话授权撤销**：后端 `GET/DELETE /threads/{id}/grants` 三端点、
    `thread-bar.tsx` 的「本会话授权」列表/单条撤销/全部撤销均已存在（K2 已验收）。
  - **B3a/b/c 与产物归集**：右侧结果区、单文件/多文件预览、产物搜索/分组/导出、
    一轮产物条均已在既有验收记录中；v5 未给出新的具体缺口，本轮不重写。
- 明确未实现：
  - B3「按新交付/审查/计数口径重做」缺少可验收的具体范围，待用户给出。
  - 对标 Claude / Codex / 国内 harness 的同批对照（P3-13）需外部 CLI 与配额，
    本轮只跑 ours。
  - 质量底线端到端（P3-14）由既有定向单测覆盖，未另建真机任务。

## 修改内容

- `services/api/src/blank_api/services/agent/graph.py`：
  - 推测执行三函数对 `speculative` 缺属性兼容；提示词新增 1c 批处理/验证/预算句。
- `services/api/src/blank_api/services/tools/lsp.py`：
  - `_git_ignored` 先探测工作区根是否被上层仓库忽略；是则不过滤。
- `services/api/src/blank_api/services/tools/file_state.py`：
  - `FileState` 新增 `summary/writes/added/removed`；`content_summary()`；
    `record_read/record_write` 合并计数；快照渲染/导出/恢复同步。
- `services/api/src/blank_api/services/tools/filesystem.py`：
  - `read_file` 记录内容摘要。
- `services/api/src/blank_api/services/tools/editing.py`：
  - `edit_file/write_file` 把回执计数传给 `record_write`。
- `services/api/tests/conftest.py`：
  - `GIT_CEILING_DIRECTORIES=TEMP`，隔离 tmp 与宿主仓库。
- `services/api/tests/test_agent_v04.py`：
  - `settle()` 默认等待 15s→60s（全量负载下 handoff 恢复会超过 15s）。
- `services/api/tests/test_permission_modes_v11.py`：
  - `_approve_and_wait` 的 drain 等待 15s→60s；全量 d 的两个失败就是读到
    尚未执行完的 ToolCall 账。
- `services/api/tests/test_lsp_tool_v42.py`：
  - 夹具默认超时 5s→15s（专用超时用例仍为 1s）。
- `services/api/tests/test_approval_card_v101.py`：
  - 只读 `pwsh` 命令按产品的 `readonly:command` 规则放行，不再要求审批；
    运行时发卡用例改用高风险的 `PS_IEX`。
- `services/api/tests/test_skill_model_v52.py`：`bind_tools_stream` 参数断言更新。
- `services/api/tests/test_output_styles_v47.py`：提示词哈希更新。
- `services/api/tests/test_handoff_e2e_v12.py` / `test_unknown_facts_e2e_v147.py` /
  `test_unknown_kinds_e2e_v148.py` / `test_stop_failure_hooks_v55.py`：
  202 后统一 `settle()` + GET 终态。
- `services/api/tests/test_delivery_changed_paths_v488.py`：四组路径/baseSha 用例。
- `services/api/tests/test_review_scope_v427.py`：`done_when` 对账用例。
- `services/api/tests/test_stream_output.py`：参数完整才可推测的聚合器用例。
- `services/api/tests/test_token_trimming.py`：有 `done_when` 时审查不可解析判未通过。
- `evals/compare.mjs`：校验进程固定 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`。
  修复前真机 T09 出现一次假红：agent 内 6 passed，校验机 GBK 解码子进程输出失败
  导致 2 failed、分数 55。修复后用同一份交付复核为 **100 分**。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 定向 17 文件 | **270 passed** | 本轮相关全部 |
| worktree 9 文件 | **128 passed** | 修复前 10 failed |
| `test_lsp_tool_v42.py` | **40 passed** | 修复前 10 failed（E 盘 TEMP） |
| 审批 13 文件 | **208 passed** | 含 202 / worktree / handoff |
| `test_context_snapshot_v2.py` | **7 passed** | 摘要/计数 |
| 全量 `pytest tests/ -q`（c） | **5154 passed / 28 skipped / 0 failed** | 35m28s，TEMP 在 E 盘 |
| 全量（d，最终提示词） | 5152 passed / 2 failed | 2 个失败均为 `permission_modes` 15s 等待竞争，已修 |
| 全量（e，修复后，最终 HEAD） | **5154 passed / 28 skipped / 0 failed** | 20m28s |
| 真机 T09（最终提示词） | **100 分 / 12 轮 / 283K / 46s** | 目标 ≤12 轮、≤340K 达成 |
| 真机 T09（推测执行关） | **100 分 / 14 轮 / 362K / 47s** | 对照组 |
| 真机 T01 | **100 分 / 8 轮 / 155K / 19s** | 正确性未降 |

T09 同提示词多次采样的轮次分布：9 / 12 / 14 / 19（19 为加入 12 轮预算前）；
token 201K / 283K / 375K / 513K。模型行为方差明显，最终采样达成目标，
但不能保证每一次都在 12 轮内。

## 风险与需要审查的点

- `GIT_CEILING_DIRECTORIES` 是**测试进程级**设置：它让 tmp 内的 git 不再向上
  找到宿主仓库。若某个用例故意验证「子目录属于上层仓库」，会被这条改变语义；
  全量 5154 passed 表明当前没有这种用例。
- `_git_ignored` 的根探测增加一次 argv 项，不增加子进程；工作区根被忽略时
  不再过滤任何 LSP 结果，这是对嵌套工作区的有意放宽。
- `settle()` / `_approve_and_wait` 的 60s 只在 run 未收尾时生效；全量总时长
  从 25min（C 盘 TEMP）变为约 30-35min，主要是 E 盘 TEMP 与等待，属测试装置成本。
  全量 d 证明 15s 在负载下确实不够：两个用例读到了尚未落库的 ToolCall。
- `compare.mjs` 固定 UTF-8 只影响校验进程环境，不改变产品行为。
- T09 轮次方差仍大；若验收要求稳定 ≤12，需要进一步改执行层（例如合并
  create_plan 与首轮执行、抑制单调用 update_plan 轮），本轮未做。

## 建议人工验收

1. 全量复跑：`TEMP=E:\projects\kbgai\kbgai\.tmp-test pytest tests/ -q`，
   预期 0 failed。
2. 真机 T09 连续跑 2 次，记录轮次/token；若一次 >12，按方差处理或要求
   执行层合并首轮。
3. 真机 T01 一次确认正确性。
4. 浏览一次会话授权撤销与右侧结果区，确认 T-A2/B3 无需重做。
