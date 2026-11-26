# B4 开发交接单

## 任务与范围

- 已实现（基础优先）：
  - **否定式指令成为硬边界**：认出 `no_tools`（只回复/不要读取/不要调用工具）
    与 `read_only`（不要修改/不要执行，只分析）两类明确指令。
    - `no_tools`：这一轮工具表为空，直接作答；任务级 `no_tools` 落库，
      重试/恢复不会重新挂工具。
    - `read_only`：先于模型判定打开只读开关；执行节点只放行读工具，步数收到 8。
      这是**只读执行**，不是“征求意见”：模型判成 act/advise 都会被归一到
      只读执行，交付走正常总结，所以「只列出文件」能给出完整列表而不是判断。
  - **场景策略层**：新增 `ScenarioPolicy(planning, verification, budget_scale)`。
    - 编程：`planning=code`、`verification=code`、预算 100%。
    - 日常：`planning=general`、`verification=general`、预算 80%；系统提示明确
      “简单任务不必凑步骤”，但代码/页面的验证下限不降。
    - `dispatch.route` 按场景策略真正收缩预算；`task.started` 记录
      `scenario_policy`，证据页能回答“这轮按什么策略跑”。
  - **清单输出格式**：用户明确要清单时，系统规则要求 Markdown 列表一项一行、
    文件/路径用反引号，不能用「、」把多项串成一段；`ANSWER_INSTRUCTION`
    同步放开 5 行限制。这是对“横着排一串 inline code”的修正。
  - **只要清单的任务不再读文件内容**：新增 `list_only`；工具表只留
    `list_files` / `glob`，拿到第一份目录回执后直接进收尾；收尾用工具回执
    拼 Markdown 层级列表，不再让模型改写或总结用途。
  - 纯聊天路径保留：无目录时仍按 `chat_only` 直接作答。
- 明确未实现：
  - 未做独立模型路由/按场景换模型；仍以用户选择的供应商为主。
  - 未做场景级评测集与 Claude/Codex 同题对比。
  - 验证口径中的 `general` 目前只进系统提示，硬闸门仍共用同一套安全下限
    （这是有意的，不能借场景放宽）。
  - 复杂否定（“不是不能改，但先别动”）仍可能认不出；认不出时按原链路走。

## 修改内容

- `services/api/src/blank_api/services/agent/directives.py`（新增）：`directive_mode`
  识别 `no_tools` / `read_only` / `none`；`listing_only` 识别“只要文件/目录清单”
  （排除要内容/用途/分析的请求）；先剥掉“不要忘了运行测试”这类反向催办。
- `services/api/src/blank_api/services/agent/intent.py`：`guess_intent` 先看用户原话
  的硬指令（包装前缀不参与，避免把 UI 包装里的“只输出”当用户指令）。
- `services/api/src/blank_api/services/agent/supervisor.py`：`Judgement.no_tools`
  / `read_only`；控制词之后、动作词之前判硬指令；只读指令按只读执行落库。
- `services/api/src/blank_api/services/agent/dispatch.py`：`classify` 先判硬指令；
  `route` 按 `ScenarioPolicy.budget_scale` 收缩预算。
- `services/api/src/blank_api/services/agent/scenarios.py`：`ScenarioPolicy` 与
  编程/日常两套策略；`system_prompt` 追加规划/验证口径；`list_scenarios` 带出策略。
- `services/api/src/blank_api/services/agent/graph.py`：`AgentSession.no_tools`
  / `list_only`；`create_plan` 对 `read_only` 硬指令提前锁 readonly、把 act/advise
  归一到只读执行，并跳过只读任务的无用快照；`list_only` 在第一次目录回执后
  直接进 `summarize_result`；新增 `_list_only_summary` 用工具回执拼列表；
  系统规则新增「清单必须 Markdown 列表」。
- `services/api/src/blank_api/services/agent/transition.py`：补
  `execute_action → summarize_result` 的 `list_only` 路由解释（也修了首次真机
  复现的边映射缺失）。
- `services/api/src/blank_api/services/agent/runtime.py`：`RunRequest.no_tools`
  / `list_only`；`no_tools` 清空工具表；`list_only` 只留 `list_files/glob`；
  两者都跳过 MCP/隔离/worktree；只读任务沿用同一条 `readonly` 闸门；
  `task.started` 写 `scenario_policy` 与 `list_only`。
- `services/api/src/blank_api/services/agent/chat.py`：新增 `NO_TOOLS_SYSTEM_RULE`
  与 `LIST_ONLY_TOOLS` / `LIST_ONLY_SYSTEM_RULE`。
- `services/api/src/blank_api/schemas/task.py` / `schemas/thread.py`：`no_tools`
  / `read_only` / `list_only` 字段；`api/routes/tasks.py` / `threads.py`：
  传递并持久化。
- `services/api/tests/test_directives_v496.py`（新增）：指令识别、`listing_only`、
  意图、supervisor、只读硬边界、`list_only` 列表拼装、no_tools 落库，11 条。
- `services/api/tests/test_scenarios_v153.py`：场景策略差异、预算收缩、非法策略拒绝。

## 验证结果

| 检查 | 结果 |
|---|---|
| 新增指令测试 | **11 passed** |
| 内核相关后端回归（意图/调度/场景/会话/鉴权/纯聊天/指令） | **176 passed** |
| 前端 `check.sh --no-smoke --no-acceptance` | 全部通过（tsc / eslint / 443 单测） |
| 真机：否定式指令 | 选目录后发「只回复 OK，不要读取或修改任何文件」→ 约 9 秒内返回 `OK`，0 工具动作，任务 succeeded，0 控制台错误 |
| 真机：只读指令 | 发「不要修改任何文件，只列出这个目录里有哪些文件」→ `task.started.readonly=true`、intent 归一到只读执行、`max_steps=8`；工具仅 `list_files` / `glob`；任务 succeeded，无写操作 |
| 真机：清单收敛 | 同一条请求命中 `list_only`：工具表只有 `list_files/glob`，第一次目录回执后 `execute_action → summarize_result`；结果由 `_list_only_summary` 直接拼成「工作区文件：」+ 层级 Markdown 列表，不再读文件内容、不再让模型写总结 |
| 真机：纯聊天清单 | 「列出三种常见排序算法，每种一行」→ 三条 Markdown 列表，界面渲染为纵向项目符号；0 工具动作 |
| 真机：场景策略 | `task.started.scenario_policy` 按场景记录；日常预算按 80% 收缩（单测） |
| 测试会话清理 | 本轮探针会话已永久删除；用户原有的 `ok` 会话保留 |

## 风险与需要审查的点

- 规则式否定识别覆盖“明确说出来的边界”，不覆盖复杂语义否定；认不出时按原
  链路走，仍可能过度执行。下一步需要场景级评测与更多真实语料，而不是继续加正则。
- `read_only` 任务仍可能花 20–30 秒做只读勘察（真机列目录例），说明“读得太多”
  仍是成本/速度问题；B5 对标评测要把它计入。
- `read_only` 任务的工具表仍会向模型列出写工具，执行层会硬拒；下一步可以在
  装配层收窄成只读可见表，减少模型先看到再被拒的浪费。
- `list_only` 的层级列表来自第一次 `list_files` 回执；模型若传的 depth 很浅，
  更深层文件不会出现。下一步可固定默认 depth 或用 `glob **/*` 补全。
- 真机首次复现这次改动的 `execute_action → summarize_result` 边映射缺失，
  已修复并补路由测试；修复前产生的失败轮次不会自动重跑，需要新开一轮。
- 场景策略目前只有规划提示、验证提示、预算比例三项真实生效；模型路由、
  上下文策略、日常应用集成还没做，不能对外宣称两场景已完全分化。
- `general` 验证口径没有放宽硬闸门；若后续想调整，必须先证明不会把未验证
  交付判成功。

## 建议人工验收

1. 选一个目录，发送「只回复 OK，不要读取或修改任何文件」：应在几秒内只回
   `OK`，不出现工具活动。
2. 发送「不要修改任何文件，只列出这个目录里有哪些文件」：应只读列目录，
   不出现写文件/改代码；活动里只应有读工具；输出必须是**纵向 Markdown 列表**，
   不能把多个路径用「、」串成一段。
3. 切到「日常」场景跑一个多步骤任务：确认能正常完成，且系统提示/预算按日常
   策略执行；代码/页面改动仍要真跑验证。
4. 打开任务证据，确认 `task.started.scenario_policy` 与
   `intent.classification` / `readonly.started` 能对上。
