# T-R2-07 开发交接单

## 任务与范围

本单承接 `dev/NEXT-DEV-v4.md` 的任务 3、任务 4，以及任务 5 的一部分；任务 2（lsp）
以「最新全量运行 0 失败」结案，不再改实现。

- 已实现：
  - **任务 3-a：钩子 fail-closed**（上一轮已落代码，本单补测试）。
    `HookSpec.on_failure` 支持 `open`（默认）与 `block`；阻断型事件上
    `on_failure=block` 的失败/超时钩子按拒绝执行处理，非阻断事件不会被改成拒绝。
  - **任务 3-b：`done_when` 判据 blocking**。审查节点把 `goal_spec.done_when`
    逐条写进审查提示，要求审查模型输出同序 `done_when: [{text,status,evidence}]`；
    代码侧确定性对账：任何一条不是 `met` 且带非空证据，强制 `approved=false`
    并生成可执行修复意见（不再只靠审查模型自觉）。审查模型连可解析 JSON 都没
    返回时（原逻辑直接放行），只要存在 `done_when` 就按未通过处理并消耗一轮
    修复预算——不能用一个解析失败换一次伪成功。
  - **任务 3-c：审查确定性范围**。审查前用 git 产出
    `baseSha / headSha / mergeBaseSha` 与 `committed / staged / unstaged / untracked`
    四组路径（对齐 DSH `change-scope`）；`baseSha` 在任务开局取一次并随检查点恢复。
    范围只给路径，不读 diff 内容；审查提示明确「结论不得与 git 硬事实矛盾、只评清单内路径」。
  - **任务 4：推测性只读执行**。模型流式生成参数时，OpenAI 兼容适配器把已拼完整的
    工具调用回调给图；`read_file / glob / search_code` 在 `permission_mode=bypass`
    下提前跑 handler。结果只缓存，不落库、不发事件；执行点仍由网关完整走
    权限/场景/参数校验/记账，参数不一致则丢弃重跑。`read_file` 的读后写状态
    在推测期间进延迟队列，真交付时才补记——被驳回/拦截的预读不会给写操作
    开后门。
  - **任务 5（部分）**：`test_handoff_e2e_v12`、`test_unknown_facts_e2e_v147`、
    `test_plan_mode_v113`、`test_prompt_submit_v123`、`test_unknown_kinds_e2e_v148`
    的审批断言改为 202 + 等待后台恢复后再验状态/产物；
    `test_stop_failure_hooks_v55` 的续跑失败用例改为先 202 + 等恢复，再验
    StopFailure 钩子。
- 明确未实现：
  - 任务 4 的真实延迟对照（首响应/单轮耗时）：需真机流式运行，未跑。
  - 任务 2：未改 lsp 实现。最新可查全量运行
    `logs/pytest-full-2026-11-24b.log` 为 **5100 passed / 28 skipped / 0 failed**，
    `test_lsp_tool_v42.py` 在该次全量中全过；此前 13 个 lsp 失败未能复现，
    需要下一次全量若复现时提供具体用例 ID。

## 修改内容

- `services/api/src/blank_api/services/hooks.py`：`HookSpec.on_failure`（open/block）、
  配置解析与校验、审计字段、阻断型事件失败转 deny。
- `services/api/src/blank_api/services/agent/graph.py`：
  - `AgentState.base_sha`；`_git_text/_git_path_list/_git_base_sha/_scope_relative/
    _review_git_facts/_render_git_scope/_review_scope_detail`；
    `_review_scope` 保持两参数兼容入口。
  - `review_result_node`：审查提示加入 done_when 清单与输出契约；
    `_review_done_when` 确定性对账；`review.completed` 事件带 `scope_facts`。
  - 推测执行：`_SPECULATIVE_TOOLS`、`_DeferredReads`、`_speculative_read`、
    `_start_speculation`、`_take_speculation`、`_clear_speculation`；
    `_call_stream` 传入 `on_tool_call` 并在每次尝试前清理；`_dispatch` 复用提前量；
    `_execute_all` 之后清理未消费的推测。
- `services/api/src/blank_api/services/agent/runtime.py`：初始状态写入
  `base_sha = git rev-parse HEAD`（线程池执行，git 不可用为空串）。
- `services/api/src/blank_api/services/models/base.py`：`ToolCallHandler`、
  `bind_tools_stream(on_tool_call=...)`、`_ready_tool_call`。
- `services/api/src/blank_api/services/models/streaming.py`：
  `ToolCallAccumulator.complete(index)`（参数完整才返回）。
- `services/api/src/blank_api/services/models/openai_compat.py`：流式分片里
  每条调用首次拼完整时回调一次；整包回退路径同样回调。
- `services/api/src/blank_api/services/tools/gateway.py`：`execute(prefetched=...)`；
  只在最终放行路径复用提前量，ASK 路径绝不使用。
- `services/api/src/blank_api/services/features.py`：新增 `speculative_tools`
  （STABLE，默认开；仅 bypass 生效）。
- `services/api/tests/test_hook_contract_v111.py`：两个 fail-closed 用例。
- `services/api/tests/test_review_scope_v427.py`：`done_when` 对账与范围归一化用例。
- `services/api/tests/test_delivery_changed_paths_v488.py`：四组路径与 baseSha 的真实
  git 仓库用例（工作区子目录前缀、committed 基点）。
- `services/api/tests/test_stream_output.py`：参数完整才可推测的聚合器用例。
- `services/api/tests/test_token_trimming.py`：有 `done_when` 时审查不可解析也判未通过。
- `services/api/tests/test_handoff_e2e_v12.py` / `test_unknown_facts_e2e_v147.py` /
  `test_plan_mode_v113.py` / `test_prompt_submit_v123.py` /
  `test_unknown_kinds_e2e_v148.py` / `test_stop_failure_hooks_v55.py`：
  审批 202 与后台等待。

## 验证结果

按用户指示，本轮**未执行 pytest**，测试由另外的 runner 负责。只做了两项静态检查：

| 命令 | 结果 | 摘要 |
|---|---|---|
| `python -I -c "ast.parse(...)"`（本轮全部 19 个 Python 改动） | **AST OK 19** | 语法可解析 |
| `git diff --check`（同批文件） | **exit 0** | 无空白错误 |

全量失败清单以最新可查的 `logs/pytest-full-2026-11-24b.log` 为准：**5100 passed /
28 skipped / 0 failed**（worktree、output_styles、prompt_submit、skill_scan、
stop_failure_hooks 等当次均未失败）。本轮改动后未复跑。

上一轮已知数字（本轮改动前）：

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_parallel_writes_v4.py ...` | 110 passed | 1b |
| 审批 5 文件 + lsp | 123 passed | 202 第一批 |
| `pytest tests/test_lsp_tool_v42.py` | 40 passed | 单独运行 |
| 全量（2026-11-24b） | 5100 passed / 28 skipped | 0 failed |

## 风险与需要审查的点

- `done_when` 强制 blocking 后，带人话验收条件（尤其 `set_goal`）的任务可能多出
  1-2 轮修复；需要确认这是质量目标要的行为，而不是回归。
- 推测执行只在 `permission_mode=bypass` + 真实流式（OpenAI 兼容增量）时生效；
  非流式整包回退也会触发，但无延迟收益。Anthropic / Responses / Ollama 暂未接
  增量回调。
- 推测期间 `read_file` 使用 `_DeferredReads` 代理 `file_state`；需重点审查
  「驳回/拦截后不补记」与「真交付补记」两条路径。
- 审查每次多跑 4-5 条只读 git 命令（15s 超时），非 git 仓库自动降级为原有回执口径。
- 审批 202 适配已覆盖本轮清单，但全部未执行；`settle()` 默认等待 15s，
  后台恢复若比这个慢（如 StopFailure 用例），需要按失败输出调大。

## 建议人工验收

1. 由测试 runner 跑：`pytest tests/test_hook_contract_v111.py tests/test_review_scope_v427.py tests/test_delivery_changed_paths_v488.py tests/test_handoff_e2e_v12.py tests/test_unknown_facts_e2e_v147.py tests/test_unknown_kinds_e2e_v148.py tests/test_plan_mode_v113.py tests/test_prompt_submit_v123.py tests/test_stop_failure_hooks_v55.py tests/test_stream_output.py tests/test_token_trimming.py tests/test_parallel_writes_v4.py -q`。
2. 真机跑一次 bypass 档、带 `read_file` 的任务，确认事件流与文件内容不变，
   再对照首响应/单轮耗时；关 `speculative_tools` 复测一次做对照。
3. 全量复跑；若 lsp 13 失败再次出现，抓具体用例 ID 与 `wire.log` 再定位。
