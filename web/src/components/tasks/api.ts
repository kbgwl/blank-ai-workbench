import {
  API_V1,
  ApiError,
  drainSse,
  errorCode,
  errorMessage,
  jsonInit,
  parseJson,
  request,
  toApiError,
} from "@/lib/http";

export { ApiError, errorCode, errorMessage };

const api = API_V1;

// ---------------------------------------------------------------------------
// 兜底读取：后端 payload 字段名可能存在别名（tool/name、args/arguments、output/result），
// 所有渲染都走这几个 helper，缺失的键返回空值而不是崩溃。
// ---------------------------------------------------------------------------

export function pick<T = unknown>(source: unknown, ...keys: string[]): T | undefined {
  if (!source || typeof source !== "object") return undefined;
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null) return value as T;
  }
  return undefined;
}

/** 字段别名版 pick：`alias(obj, ["tool", "name"])`。 */
export function alias<T = unknown>(source: unknown, keys: readonly string[]): T | undefined {
  return pick<T>(source, ...keys);
}

export function textOf(source: unknown, ...keys: string[]): string {
  for (const key of keys) {
    const value = pick<unknown>(source, key);
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
    if (Array.isArray(value)) {
      const joined = value.map((item) => stringify(item)).filter(Boolean).join("\n");
      if (joined) return joined;
    }
    if (value && typeof value === "object") return stringify(value);
  }
  return "";
}

export function arrayOf(source: unknown, ...keys: string[]): unknown[] {
  for (const key of keys) {
    const value = pick<unknown>(source, key);
    if (Array.isArray(value)) return value;
  }
  return [];
}

export function recordOf(source: unknown, ...keys: string[]): Record<string, unknown> {
  for (const key of keys) {
    const value = pick<unknown>(source, key);
    if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  }
  return {};
}

export function numOf(source: unknown, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = pick<unknown>(source, key);
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) return Number(value);
  }
  return null;
}

export function boolOf(source: unknown, ...keys: string[]): boolean | null {
  for (const key of keys) {
    const value = pick<unknown>(source, key);
    if (typeof value === "boolean") return value;
    if (typeof value === "string") {
      const lower = value.toLowerCase();
      if (["true", "yes", "ok", "passed", "covered", "1"].includes(lower)) return true;
      if (["false", "no", "failed", "error", "0"].includes(lower)) return false;
    }
    if (typeof value === "number") return value !== 0;
  }
  return null;
}

export function stringify(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map((item) => stringify(item)).filter(Boolean).join("\n");
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// ---------------------------------------------------------------------------
// 任务
// ---------------------------------------------------------------------------

export const TERMINAL_STATUSES = ["succeeded", "failed", "cancelled", "canceled", "completed", "error"];

const STATUS_ALIASES: Record<string, string> = {
  done: "succeeded",
  success: "succeeded",
  completed: "succeeded",
  ok: "succeeded",
  canceled: "cancelled",
  error: "failed",
  awaiting_approval: "waiting_approval",
  waiting_for_user: "waiting_approval",
  waiting_approval: "waiting_approval",
};

export function normalizeStatus(value: unknown): string {
  const raw = String(value ?? "").toLowerCase().replace(/\s+/g, "_");
  if (!raw) return "draft";
  return STATUS_ALIASES[raw] ?? raw;
}

export function isTerminalStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return TERMINAL_STATUSES.includes(normalizeStatus(status));
}

export type Task = {
  id: string;
  projectId: string;
  title: string;
  prompt: string;
  status: string;
  providerId: string;
  providerName: string;
  acceptanceCriteria: string[];
  resultSummary: string;
  failureReason: string;
  stepsUsed: number | null;
  maxSteps: number | null;
  createdAt: string;
  updatedAt: string;
  raw: Record<string, unknown>;
};

export function normalizeTask(source: unknown): Task | null {
  if (!source || typeof source !== "object") return null;
  const raw = source as Record<string, unknown>;
  const id = textOf(raw, "id", "task_id", "uuid");
  if (!id) return null;
  return {
    id,
    projectId: textOf(raw, "project_id", "project"),
    title: textOf(raw, "title", "name", "summary") || "未命名任务",
    prompt: textOf(raw, "prompt", "goal", "description", "instruction", "text"),
    status: normalizeStatus(pick(raw, "status", "state")),
    providerId: textOf(raw, "provider_id", "providerId"),
    providerName: textOf(raw, "provider_name", "provider"),
    acceptanceCriteria: arrayOf(raw, "acceptance_criteria", "acceptance", "criteria").map((item) =>
      typeof item === "string" ? item : textOf(item, "criterion", "text", "title"),
    ),
    resultSummary: textOf(raw, "result_summary", "summary", "result"),
    failureReason: textOf(raw, "failure_reason", "reason", "error"),
    stepsUsed: numOf(raw, "steps_used", "steps"),
    maxSteps: numOf(raw, "max_steps"),
    createdAt: textOf(raw, "created_at", "createdAt"),
    updatedAt: textOf(raw, "updated_at", "updatedAt"),
    raw,
  };
}

export type TaskDraft = {
  projectId: string;
  title: string;
  prompt: string;
  acceptanceCriteria: string[];
  providerId?: string;
};

// ---------------------------------------------------------------------------
// 事件时间线
// ---------------------------------------------------------------------------

export type AgentEvent = {
  seq: number;
  node: string;
  type: string;
  payload: Record<string, unknown>;
  createdAt: string;
};

export function normalizeEvent(source: unknown, fallbackSeq = 0): AgentEvent | null {
  if (!source || typeof source !== "object") return null;
  const raw = source as Record<string, unknown>;
  const type = textOf(raw, "type", "event", "event_type", "kind");
  if (!type) return null;
  return {
    seq: numOf(raw, "seq", "index", "id") ?? fallbackSeq,
    node: textOf(raw, "node", "step", "from"),
    type,
    payload: recordOf(raw, "payload", "data", "body"),
    createdAt: textOf(raw, "created_at", "createdAt", "timestamp", "ts"),
  };
}

/** 兼容三种返回形态：事件数组、{events:[…]}、单条事件对象。 */
export function normalizeEvents(body: unknown): AgentEvent[] {
  const rows = Array.isArray(body) ? body : arrayOf(body, "events", "items", "results", "rows");
  const list = rows.length > 0 || !body ? rows : [body];
  const events: AgentEvent[] = [];
  list.forEach((item, index) => {
    const event = normalizeEvent(item, index + 1);
    if (event) events.push(event);
  });
  events.sort((a, b) => a.seq - b.seq);
  return events;
}

export function eventToolName(event: AgentEvent): string {
  return textOf(event.payload, "tool", "name", "tool_name", "action") || event.node || "tool";
}

export function eventToolArgs(event: AgentEvent): Record<string, unknown> {
  const direct = recordOf(event.payload, "arguments", "args", "input", "params", "payload");
  return direct;
}

export function eventToolOutput(event: AgentEvent): string {
  return textOf(
    event.payload,
    "output",
    "result",
    "summary",
    "stdout",
    "message",
    "detail",
    "text",
    "data",
  );
}

// ---------------------------------------------------------------------------
// 交付证明包
// ---------------------------------------------------------------------------

export type AcceptanceItem = { criterion: string; covered: boolean | null; note: string };
export type ChangeItem = { path: string; diff: string; changeType: string };
export type TestInfo = {
  command: string;
  exitCode: number | null;
  output: string;
  passed: boolean | null;
  status: string;
};
export type UsageInfo = {
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  latencyMs: number | null;
  calls: number | null;
  models: string[];
};
/** 审批作用域：仅本次 / 本会话该命令前缀 / 本会话该工具。 */
export type ApprovalScope = "once" | "prefix" | "tool";

export type PendingApproval = {
  toolCallId: string;
  tool: string;
  args: Record<string, unknown>;
  risk: string;
  message: string;
  /** 后端给出的前缀签名（"git push"），非命令类工具为空。 */
  prefix: string;
  /** 后端下发的可选作用域，前端不自己编造档位。 */
  scopes: { scope: ApprovalScope; label: string }[];
  mode: string;
};
export type ToolRecord = {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: string;
  risk: string;
  durationMs: number | null;
  output: string;
};

/** Agent 主动提问：与审批共用同一条中断通道，答案回填后任务继续。 */
export type PendingQuestion = {
  id: string;
  question: string;
  header: string;
  options: { label: string; description: string }[];
  multiSelect: boolean;
  allowFreeText: boolean;
};

export type Evidence = {
  status: string;
  acceptance: AcceptanceItem[];
  changes: ChangeItem[];
  test: TestInfo | null;
  usage: UsageInfo;
  pendingApprovals: PendingApproval[];
  pendingQuestions: PendingQuestion[];
  tools: ToolRecord[];
  timeline: AgentEvent[];
  unverified: string[];
  raw: Record<string, unknown>;
};

const EMPTY_EVIDENCE: Evidence = {
  status: "",
  acceptance: [],
  changes: [],
  test: null,
  usage: {
    promptTokens: null,
    completionTokens: null,
    totalTokens: null,
    latencyMs: null,
    calls: null,
    models: [],
  },
  pendingApprovals: [],
  pendingQuestions: [],
  tools: [],
  timeline: [],
  unverified: [],
  raw: {},
};

function normalizeAcceptance(source: unknown): AcceptanceItem[] {
  return arrayOf(source, "acceptance", "acceptance_results", "acceptance_criteria", "criteria", "items").map(
    (item) => {
      if (typeof item === "string") return { criterion: item, covered: null, note: "" };
      return {
        criterion: textOf(item, "criterion", "text", "title", "name", "requirement") || "未命名验收条件",
        covered: boolOf(item, "covered", "passed", "met", "done", "ok", "satisfied"),
        note: textOf(item, "evidence", "note", "reason", "message", "detail", "summary"),
      };
    },
  );
}

/** 后端可能给 changes:[{path,diff}]，也可能给 changes:{files:[…], diff:"合并后的整段 diff"}。 */
function normalizeChanges(container: unknown): ChangeItem[] {
  const nested = (container as Record<string, unknown>)?.changes;
  const scope =
    nested && typeof nested === "object" && !Array.isArray(nested) ? (nested as Record<string, unknown>) : container;
  const list = arrayOf(scope, "changes", "diffs", "items", "file_changes", "files");
  const result: ChangeItem[] = list
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      path: textOf(item, "path", "file", "file_path", "name") || "未知文件",
      diff: textOf(item, "diff", "patch", "unified_diff", "content"),
      changeType: textOf(item, "change_type", "type", "status", "kind"),
    }));
  const merged = textOf(scope, "diff", "full_diff", "patch") || textOf(container, "diff", "full_diff", "patch");
  // 后端只在 changes 层给整段 diff，items 里只有路径：单文件时直接挂到那一条上。
  if (merged && result.length === 1 && !result[0].diff) {
    return [{ ...result[0], diff: merged }];
  }
  if (merged && result.every((item) => !item.diff)) {
    const parsed = splitUnifiedDiff(merged);
    if (parsed.length > 0) return parsed;
    return [{ path: `${result.length} 个文件`, diff: merged, changeType: "" }];
  }
  return result;
}

export function splitUnifiedDiff(text: string): ChangeItem[] {
  const chunks = text.split(/(?=^(?:diff --git |--- a\/))/m);
  const files: ChangeItem[] = [];
  for (const chunk of chunks) {
    if (!chunk.trim()) continue;
    const gitHeader = /^diff --git a\/(\S+) b\/(\S+)/m.exec(chunk);
    const minus = /^--- (?:a\/)?(\S+)/m.exec(chunk);
    const plus = /^\+\+\+ (?:b\/)?(\S+)/m.exec(chunk);
    const path =
      (gitHeader?.[2] ?? "") ||
      (plus?.[1] && plus[1] !== "/dev/null" ? plus[1] : "") ||
      (minus?.[1] && minus[1] !== "/dev/null" ? minus[1] : "") ||
      "未知文件";
    let changeType = "";
    if (/^new file mode /m.test(chunk)) changeType = "created";
    else if (/^deleted file mode /m.test(chunk)) changeType = "deleted";
    else if (minus?.[1] === "/dev/null") changeType = "created";
    else if (plus?.[1] === "/dev/null") changeType = "deleted";
    else changeType = "modified";
    files.push({ path, diff: chunk.trimEnd(), changeType });
  }
  return files;
}

function normalizeTest(container: unknown): TestInfo | null {
  const source = pick(container, "test", "validation", "tests", "verify");
  if (!source) return null;
  if (typeof source === "string") {
    return { command: source, exitCode: null, output: "", passed: null, status: "" };
  }
  const record = source as Record<string, unknown>;
  const exitCode = numOf(record, "exit_code", "exitCode", "code", "returncode");
  const passed = boolOf(record, "passed", "ok", "success", "pass");
  return {
    command: textOf(record, "command", "cmd", "script", "name") || "暂无",
    exitCode,
    output: textOf(record, "output", "stdout", "logs", "result", "text", "output_tail"),
    passed: passed ?? (exitCode === null ? null : exitCode === 0),
    status: textOf(record, "status", "state"),
  };
}

function normalizeUsage(container: unknown): UsageInfo {
  const record = recordOf(container, "usage", "token_usage", "tokens");
  const prompt = numOf(record, "prompt_tokens", "input_tokens", "promptTokens");
  const completion = numOf(record, "completion_tokens", "output_tokens", "completionTokens");
  const total = numOf(record, "total_tokens", "totalTokens") ?? (prompt !== null || completion !== null ? (prompt ?? 0) + (completion ?? 0) : null);
  return {
    promptTokens: prompt,
    completionTokens: completion,
    totalTokens: total,
    latencyMs: numOf(record, "latency_ms", "latency", "duration_ms", "total_latency_ms"),
    calls: numOf(record, "calls", "request_count"),
    models: arrayOf(record, "models", "model").map((item) => String(item)),
  };
}

function normalizeApprovalScopes(source: unknown): { scope: ApprovalScope; label: string }[] {
  return arrayOf(source, "approval_options", "scopes", "options")
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      scope: (textOf(item, "scope", "value") || "once") as ApprovalScope,
      label: textOf(item, "label", "title"),
    }))
    .filter((item) => item.label !== "" || item.scope !== "once");
}

export function normalizeQuestions(container: unknown): PendingQuestion[] {
  return arrayOf(container, "pending_questions", "questions")
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item, index) => ({
      id: textOf(item, "id", "question_id", "key") || `q${index + 1}`,
      question: textOf(item, "question", "text", "prompt") || "需要你确认一件事",
      header: textOf(item, "header", "title"),
      options: arrayOf(item, "options", "choices")
        .map((option) => {
          if (typeof option === "string") return { label: option, description: "" };
          return {
            label: textOf(option, "label", "value", "title"),
            description: textOf(option, "description", "detail", "note"),
          };
        })
        .filter((option) => option.label !== ""),
      multiSelect: boolOf(item, "multiSelect", "multi_select") === true,
      // 后端不写这个字段时按"允许自由输入"处理：反问本来就是为了拿到模型猜不到的信息
      allowFreeText: boolOf(item, "allow_free_text", "allowFreeText") !== false,
    }));
}

function normalizeApprovals(container: unknown): PendingApproval[] {
  const rows = arrayOf(container, "pending_approvals", "approvals", "awaiting_approval");
  return rows
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      toolCallId: textOf(item, "tool_call_id", "id", "approval_id"),
      tool: textOf(item, "tool", "name", "action") || "未知操作",
      args: recordOf(item, "arguments", "args", "input", "params"),
      risk: textOf(item, "risk_level", "risk", "level"),
      message: textOf(item, "message", "reason", "summary", "approval_state"),
      prefix: textOf(item, "suggested_prefix", "prefix"),
      scopes: normalizeApprovalScopes(item),
      mode: textOf(item, "permission_mode", "mode"),
    }));
}

function normalizeToolRecords(container: unknown): ToolRecord[] {
  const rows = arrayOf(container, "tools", "tool_calls", "calls");
  return rows
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      id: textOf(item, "id", "tool_call_id"),
      name: textOf(item, "name", "tool") || "未知工具",
      args: recordOf(item, "arguments", "args", "input"),
      status: normalizeStatus(pick(item, "status", "state")),
      risk: textOf(item, "risk_level", "risk"),
      durationMs: numOf(item, "duration_ms", "latency_ms"),
      output: textOf(item, "summary", "output", "result", "message"),
    }));
}

export function normalizeEvidence(source: unknown): Evidence {
  if (!source || typeof source !== "object") return { ...EMPTY_EVIDENCE };
  const raw = source as Record<string, unknown>;
  const inner = recordOf(raw, "evidence", "data");
  // 后端把任务字段放在 evidence.task 里，状态与摘要都要从那儿取。
  const task = recordOf(raw, "task");
  const container: Record<string, unknown> = { ...task, ...inner, ...raw };
  return {
    status: normalizeStatus(pick(container, "status", "state", "task_status")),
    acceptance: normalizeAcceptance(container),
    changes: normalizeChanges(container),
    test: normalizeTest(container),
    usage: normalizeUsage(container),
    pendingApprovals: normalizeApprovals(container),
    pendingQuestions: normalizeQuestions(container),
    tools: normalizeToolRecords(container),
    timeline: normalizeEvents(pick(container, "timeline", "events")),
    unverified: arrayOf(container, "unverified_risks", "unverified", "warnings").map((item) =>
      typeof item === "string" ? item : textOf(item, "message", "reason", "criterion"),
    ),
    raw: container,
  };
}

export const emptyEvidence = (): Evidence => ({
  ...EMPTY_EVIDENCE,
  acceptance: [],
  changes: [],
  tools: [],
  pendingApprovals: [],
  timeline: [],
  unverified: [],
  usage: { ...EMPTY_EVIDENCE.usage, models: [] },
  raw: {},
});

// ---------------------------------------------------------------------------
// 项目 / 工作区 / 文件树 / 供应商
// ---------------------------------------------------------------------------

export type Workspace = { id: string; label: string; path: string; is_active: boolean };
export type Project = { id: string; name: string; description: string; workspaces: Workspace[] };
export type Provider = { id: string; name: string; model: string; kind: string; enabled: boolean };

/** 供应商列表拿不到时，模型选择器退化为「跟随默认供应商」。 */
export const DEFAULT_PROVIDER = "";

// ---------------------------------------------------------------------------
// 请求
// ---------------------------------------------------------------------------

export function isMissingEndpoint(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return error.status === 404 || error.status === 405 || /^http_(404|405)$/.test(error.code);
}

export function fetchProjects(): Promise<Project[]> {
  return request<unknown[]>(`${api}/projects`, { cache: "no-store" }).then((rows) =>
    rows
      .map((row) => {
        if (!row || typeof row !== "object") return null;
        const raw = row as Record<string, unknown>;
        const id = textOf(raw, "id", "project_id");
        if (!id) return null;
        return {
          id,
          name: textOf(raw, "name", "title") || "未命名项目",
          description: textOf(raw, "description"),
          workspaces: arrayOf(raw, "workspaces").map((item) => ({
            id: textOf(item, "id", "workspace_id"),
            label: textOf(item, "label", "name"),
            path: textOf(item, "path"),
            is_active: boolOf(item, "is_active", "active") ?? false,
          })),
        } satisfies Project;
      })
      .filter((project): project is Project => project !== null),
  );
}

export function fetchProviders(): Promise<Provider[]> {
  return request<unknown[]>(`${api}/providers`, { cache: "no-store" }).then((rows) =>
    rows
      .map((row) => {
        if (!row || typeof row !== "object") return null;
        const raw = row as Record<string, unknown>;
        const id = textOf(raw, "id", "provider_id");
        if (!id) return null;
        return {
          id,
          name: textOf(raw, "name") || id,
          model: textOf(raw, "model"),
          kind: textOf(raw, "kind"),
          enabled: boolOf(raw, "enabled") ?? true,
        } satisfies Provider;
      })
      .filter((provider): provider is Provider => provider !== null),
  );
}

export function fetchTasks(projectId?: string): Promise<Task[]> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return request<unknown>(`${api}/tasks${query}`, { cache: "no-store" }).then((body) =>
    normalizeTasks(body),
  );
}

export function normalizeTasks(body: unknown): Task[] {
  const rows = Array.isArray(body) ? body : arrayOf(body, "tasks", "items", "results");
  return rows
    .map((row) => normalizeTask(row))
    .filter((task): task is Task => task !== null);
}

export function fetchTask(taskId: string): Promise<Task | null> {
  return request<unknown>(`${api}/tasks/${taskId}`, { cache: "no-store" }).then((body) => {
    const direct = normalizeTask(body);
    if (direct) return direct;
    return normalizeTask(pick(body, "task", "data"));
  });
}

export function createTask(projectId: string, draft: TaskDraft): Promise<Task> {
  const body: Record<string, unknown> = {
    title: draft.title,
    prompt: draft.prompt,
    goal: draft.prompt,
    acceptance_criteria: draft.acceptanceCriteria,
  };
  if (draft.providerId) {
    body.provider_id = draft.providerId;
  }
  return request<unknown>(`${api}/projects/${projectId}/tasks`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(body),
  }).then((payload) => {
    const task = normalizeTask(payload) ?? normalizeTask(pick(payload, "task", "data"));
    if (!task) throw new ApiError("后端未返回任务信息", "task_missing_in_response", 502);
    return task;
  });
}

export function runTask(taskId: string, input: { approveHigh?: boolean } = {}): Promise<unknown> {
  const body: Record<string, unknown> = {};
  if (input.approveHigh) body.approve_high_risk = true;
  if (typeof window !== "undefined") {
    try {
      const roles = JSON.parse(window.localStorage.getItem("blank.model.roles") ?? "{}") as Record<string, string>;
      if (Object.keys(roles).length > 0) body.role_provider_ids = roles;
    } catch {
      // Ignore malformed local preferences and use the primary model.
    }
  }
  return request<unknown>(`${api}/tasks/${taskId}/start`, { method: "POST", ...jsonInit, body: JSON.stringify(body) });
}

export function cancelTask(taskId: string): Promise<unknown> {
  return request<unknown>(`${api}/tasks/${taskId}/cancel`, { method: "POST" });
}

/** DELETE /tasks/{id}：连事件、运行记录、检查点、改动记录一起删，不可恢复。
 *  运行中的任务后端会拒绝（409），前端先取消再删。 */
export function deleteTask(taskId: string): Promise<void> {
  return request<void>(`${api}/tasks/${taskId}`, { method: "DELETE" });
}

/** POST /tasks/{id}/steer：运行中插话，入队后从下一次模型调用开始生效。
 *  只改方向，不撤回已经执行的动作——所以这里只返回排队结果，不假装任务已改变。 */
export function steerTask(taskId: string, message: string): Promise<{ queued: boolean; queuedAt: number | null }> {
  return request<{ queued?: boolean; queued_at?: number }>(`${api}/tasks/${taskId}/steer`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ message }),
  }).then((body) => ({ queued: body?.queued !== false, queuedAt: body?.queued_at ?? null }));
}

/** POST /tasks/{id}/approval：批准或驳回待审批的高风险操作，返回 {task, evidence}。 */
export function decideApproval(
  taskId: string,
  input: { decision: "approve" | "deny"; scope?: ApprovalScope },
): Promise<{ used: string }> {
  const body: Record<string, unknown> = { approve: input.decision === "approve" };
  if (input.decision === "approve" && input.scope) body.scope = input.scope;
  return request<unknown>(`${api}/tasks/${taskId}/approval`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(body),
  }).then(() => ({ used: `${api}/tasks/${taskId}/approval` }));
}

/** POST /tasks/{id}/answer：回答 Agent 的提问，答案回填后从断点继续。 */
export function answerTask(
  taskId: string,
  answers: Record<string, string>,
): Promise<{ used: string }> {
  return request<unknown>(`${api}/tasks/${taskId}/answer`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ answers }),
  }).then(() => ({ used: `${api}/tasks/${taskId}/answer` }));
}

export function fetchEvents(taskId: string, since?: number): Promise<AgentEvent[]> {
  const query = typeof since === "number" ? `?after_seq=${since}` : "";
  return request<unknown>(`${api}/tasks/${taskId}/events${query}`, { cache: "no-store" }).then(normalizeEvents);
}

export function fetchEvidence(taskId: string): Promise<Evidence> {
  return request<unknown>(`${api}/tasks/${taskId}/evidence`, { cache: "no-store" }).then(normalizeEvidence);
}

export type TaskSnapshot = { id: string; reason: string; created_at: number; files: string[] };

export function fetchTaskSnapshots(taskId: string): Promise<TaskSnapshot[]> {
  return request<TaskSnapshot[]>(`${api}/tasks/${taskId}/snapshots`, { cache: "no-store" });
}

export function restoreTaskSnapshot(taskId: string, snapshotId: string): Promise<unknown> {
  return request<unknown>(`${api}/tasks/${taskId}/snapshots/${snapshotId}/restore`, { method: "POST" });
}

export type TreeEntry = { name: string; path: string; is_dir: boolean; size: number | null };
export type TreeResponse = { root: string; path: string; entries: TreeEntry[] };
export type FileContent = { path: string; content: string; binary: boolean; truncated: boolean; message: string; size: number | null };

export function fetchTree(projectId: string, path = "", depth = 1): Promise<TreeResponse> {
  const query = `path=${encodeURIComponent(path)}&depth=${depth}`;
  return request<unknown>(`${api}/projects/${projectId}/tree?${query}`, { cache: "no-store" }).then((body) => {
    const entries = arrayOf(body, "entries", "items", "tree", "children").map((item) => ({
      name: textOf(item, "name") || textOf(item, "path").split(/[\\/]/).pop() || "",
      path: textOf(item, "path", "full_path"),
      is_dir: boolOf(item, "is_dir", "isDir", "directory") ?? false,
      size: numOf(item, "size", "bytes"),
    }));
    return {
      root: textOf(body, "root", "workspace"),
      path: textOf(body, "path"),
      entries,
    } satisfies TreeResponse;
  });
}

export function fetchFile(projectId: string, path: string): Promise<FileContent> {
  return request<unknown>(`${api}/projects/${projectId}/file?path=${encodeURIComponent(path)}`, {
    cache: "no-store",
  }).then((body) => {
    const record = (body ?? {}) as Record<string, unknown>;
    return {
      path: textOf(record, "path") || path,
      content: textOf(record, "content", "text", "data"),
      binary: boolOf(record, "binary") ?? false,
      truncated: boolOf(record, "truncated") ?? false,
      message: textOf(record, "message"),
      size: numOf(record, "size", "bytes"),
    } satisfies FileContent;
  });
}

// ---------------------------------------------------------------------------
// SSE 事件流
// ---------------------------------------------------------------------------

export type StreamHandlers = {
  onEvent: (event: AgentEvent) => void;
  onDone?: () => void;
  signal: AbortSignal;
};

function emitFrame(data: string, onEvent: StreamHandlers["onEvent"]): void {
  if (!data.trim()) return;
  const parsed = parseJson(data);
  if (!parsed) {
    return;
  }
  for (const event of normalizeEvents(parsed)) onEvent(event);
}

/**
 * GET /tasks/{id}/stream。后端没有这个端点（404/405）或返回的不是 event-stream 时抛错，
 * 由 useTaskEvents 退回轮询；抛出的错误消息直接来自后端的中文错误信封。
 */
export async function streamTaskEvents(taskId: string, handlers: StreamHandlers): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${api}/tasks/${taskId}/events/stream`, {
      method: "GET",
      headers: { accept: "text/event-stream" },
      cache: "no-store",
      signal: handlers.signal,
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    throw new ApiError("无法连接后端服务", "network_error", 0);
  }

  if (!response.ok) {
    const text = await response.text();
    throw toApiError(response.status, text ? parseJson(text) : null);
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!response.body || !contentType.includes("event-stream")) {
    throw new ApiError("后端未提供 SSE 事件流", "stream_unavailable", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
      const drained = drainSse(buffer);
      buffer = drained.rest;
      for (const frame of drained.events) {
        if (frame.event === "done" || frame.event === "keepalive" || frame.event === "ping") continue;
        emitFrame(frame.data, handlers.onEvent);
      }
    }
    buffer += decoder.decode().replace(/\r/g, "");
    for (const frame of drainSse(`${buffer}\n\n`).events) {
      if (frame.event === "done" || frame.event === "keepalive" || frame.event === "ping") continue;
      emitFrame(frame.data, handlers.onEvent);
    }
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    throw error;
  } finally {
    reader.releaseLock();
  }
  handlers.onDone?.();
}
