# 任务 API 契约（v0.4 起）

前端一律通过 `/api/v1` 前缀访问；所有错误响应形如 `{"error": {"code": "...", "message": "..."}}`，
`message` 为中文，界面直接展示，不要展示 HTTP 原始错误。

## 任务（后端已落地，字段以 services/api/src/blank_api/schemas/task.py 为准）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/projects/{project_id}/tasks` | 创建任务，请求体 `{title, goal, acceptance_criteria: string[], provider_id?: string, max_steps?: number}`（注意是 `goal`，不是 `prompt`） |
| GET | `/tasks?project_id=` | 任务列表，返回 `TaskOut[]` |
| GET | `/tasks/{task_id}` | 任务详情 `TaskOut` |
| POST | `/tasks/{task_id}/run` | 运行，请求体 `{provider_id?: string, approve_high_risk?: boolean, approved_tools?: string[]}`（注意是 `approve_high_risk`；高风险工具一律走图中断审批，`approve_high_risk=true` 表示预先放行） |
| POST | `/tasks/{task_id}/approval` | 审批待确认的高风险操作，请求体 `{approve: boolean, provider_id?: string}`；返回 `{task: TaskOut, evidence: object}`。仅 `waiting_approval` 可调用，其他状态返回 409 `task_not_waiting_approval`（重复审批幂等）。批准 = 从断点继续执行该步骤并补记 `approval.granted`；驳回 = 该步骤不执行，任务置为 `paused` 并写入 `failure_reason` |
| POST | `/tasks/{task_id}/cancel` | 取消，返回 `TaskOut`；取消后不得再产生新的工具调用 |
| GET | `/tasks/{task_id}/events?after_seq=` | **直接返回 `EventOut[]` 数组**（不是 `{events: [...]}`），`EventOut = {seq, node, type, payload}` |
| GET | `/tasks/{task_id}/evidence` | 交付证明包 |
| GET | `/tasks/{task_id}/diff` | 文件变更 Diff |

`TaskOut = {id, project_id, provider_id, title, goal, status, acceptance_criteria: string[],
acceptance_results: object[], max_steps, steps_used, result_summary, failure_reason, created_at, updated_at}`。

任务状态：`draft` `running` `waiting_approval` `paused` `succeeded` `failed` `cancelled`。

`AgentEvent = {seq: number, node: string, type: string, payload: object, created_at: string}`。
事件类型按前缀区分：`plan*`（计划）、`tool_call*` / `tool_result*`（工具调用）、`shell*`（终端输出）、
`approval*`（审批）、`diff*`（文件变更）、`result*` / `error*`（最终结果与失败）。
payload 字段名可能存在别名（如 `tool` / `name`、`args` / `arguments`、`output` / `result`），
渲染时必须做多 key 兜底，缺失字段不显示。

`证据包 = {acceptance: string[], changes: [{path, diff}], test: {command, exit_code, output}, usage: {prompt_tokens, completion_tokens, latency_ms}, status}`。
同样允许字段别名，缺失时显示「暂无」。

## 项目与工作区（已实现）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET / POST | `/projects` | 列表 / 创建（`{name, description?}`） |
| GET / PATCH / DELETE | `/projects/{id}` | 详情 / 更新 / 删除 |
| POST / DELETE | `/projects/{id}/workspaces` | 绑定绝对路径 / 解绑 |
| GET | `/projects/{id}/tree?path=&depth=` | 文件树，路径已掩码为 `<workspace:name>/...` |
| GET | `/projects/{id}/file?path=` | 文本文件内容，二进制返回 `{binary: true, message}` |

## 模型供应商（已实现）

`GET /providers`、`POST /providers`、`GET|PATCH|DELETE /providers/{id}`、
`POST /providers/{id}/test`、`POST /providers/{id}/chat`、`POST /providers/{id}/chat/stream`（SSE，
`event: delta` 的 `data` 是 `{"text": "..."}`）、`GET /usage/summary`。
