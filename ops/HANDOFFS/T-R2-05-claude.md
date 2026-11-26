# T-R2-05 开发交接单：v4 任务 1a 工具说明书瘦身

## 任务与范围

- 已实现：
  - 常驻工具从 **34 → 30**：`propose_skills` / `find_skills` /
    `install_market_skill` / `list_curated_skills` 改为按需，`tool_search`
    仍能拉回；不删功能。
  - 常见工具文案压缩：`run_shell`、`request_user_input`、`search_code`、
    `edit_file`、`update_plan`、`set_goal`、`task_update`、`propose_skills`、
    `report_findings`。删除重复句与重复的参数说明，保留行为约束与安全规则。
  - 常驻工具说明书总量：**22,694 → 19,457 字符（-14.3%）**；
    T09 文档任务可见的 16 个工具：**11,291 → 10,354 字符（-8.3%）**。
  - `SCHEMA_BUDGET_CHARS` 从 22,700 收到 **20,000**，把这次收敛钉进预算。
- 明确未实现：
  - 没有跑真机 T09/T01 对照：后端需重启加载本次改动；调用次数与 token
    尚未出数。
  - 没有做 1b（写工具不同路径并发执行）与 1c（进一步压重读）。
  - 没有动 lsp / 质量底线 / 速度 / 全量测试清理。

## 修改内容

- `services/api/src/blank_api/services/tools/registry.py`：Skill 市场四工具加入
  `DEFERRED_TOOLS`；预算降到 20,000；注释记录 v4 1a 的实测数字。
- `services/api/src/blank_api/services/tools/shell.py`：压缩 `run_shell` 文案，
  保留专用工具优先、cmd 语法、git 安全、后台命令与越界审批规则。
- `services/api/src/blank_api/services/tools/ask.py`：压缩 `request_user_input`
  文案与重复参数说明。
- `services/api/src/blank_api/services/tools/filesystem.py`：压缩 `search_code`
  文案与参数说明。
- `services/api/src/blank_api/services/tools/editing.py`：压缩 `edit_file`，
  删除 `edits` 内重复的 old_string/new_string/replace_all 说明。
- `services/api/src/blank_api/services/tools/plan.py`：压缩 `update_plan` /
  `set_goal` 的参数说明。
- `services/api/src/blank_api/services/tools/tasks.py`：压缩 `task_update` 文案。
- `services/api/src/blank_api/services/tools/skill_proposals.py`：压缩
  `propose_skills` 文案。
- `services/api/src/blank_api/services/tools/review.py`：压缩 `report_findings`
  参数说明。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_tool_budget_v427.py tests/test_ask_goal_skills_v43.py tests/test_skill_market_search_v406.py tests/test_claude_loop_parity_v27.py tests/test_background_shell_v31.py tests/test_report_findings_v34.py tests/test_claude_parity_v24.py tests/test_agent_v04.py tests/test_claude_tool_surface_v28.py -q` | **219 passed** | 工具预算/技能/任务/审查/Shell/Agent 集成 |
| 实测常驻工具说明 | **19,457 字符 / 30 工具** | 原 22,694 / 34 |
| T09 文档任务工具说明 | **10,354 字符 / 16 工具** | 原 11,291 / 16 |
| 真机 T09/T01 | 未跑 | 需重启后端加载 |

## 风险与需要审查的点

- Skill 市场四工具改为按需后，普通编程首屏不再显示它们；`tool_search`
  仍可拉回。若某个工作流默认依赖 `propose_skills` 常驻，需要改为先检索。
- 文案压缩只删重复与冗词，保留关键约束；219 个相关测试未发现行为回归。
- 预算降到 20,000 后，新增常驻工具需要先做取舍；这符合 v4“固定成本”目标。
- 1a 只降固定成本，T09 的 611K/22 轮仍主要由调用次数与历史决定；
  需要 1c/1b 才能接近 ≤12 轮、≤340K 的目标。

## 建议人工验收

1. 重启后端，跑 `node evals/compare.mjs --only T09 --agents ours --cleanup`，
   记录 token / 轮次 / 分数；确认分数仍为 100、`minimal=1`。
2. 跑 `--only T01` 确认工具调用正确性未降（尤其 `run_shell` / `edit_file`）。
3. 在聊天里说“找技能/装技能”，确认 `tool_search` 能把 Skill 市场工具拉回来。
