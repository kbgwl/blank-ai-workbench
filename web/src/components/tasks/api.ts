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
  /** 所属会话（多轮对话）；独立任务为空串。空串而不是 null，是为了让调用处少一层判空。 */
  threadId: string;
  /** 会话内第几轮，独立任务为 0。 */
  turnIndex: number;
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
    threadId: textOf(raw, "thread_id"),
    turnIndex: numOf(raw, "turn_index") ?? 0,
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
  /** 这一轮随消息一起给的图片。只传 id 与名字：文件本体已经上传过了。 */
  attachments?: AttachmentRef[];
  /** 目标会话：已有会话 id，或 "__new__" 表示新开一条并把这一轮作为第 1 轮。
   *  留空 = 独立任务，开销与没有多轮对话时完全一样。 */
  thread?: string;
};

// ---------------------------------------------------------------------------
// 会话（多轮对话）
//
// 一轮本身仍然是一条普通任务：它有自己的目标、事件、工具调用和总结。
// 会话只额外记住"哪几轮、按什么顺序、从哪条分叉过来"——所以这里不需要
// 一套独立的运行/审批接口，点某一轮就是切换到那个 task。
// ---------------------------------------------------------------------------

export type Thread = {
  id: string;
  projectId: string;
  title: string;
  /** active | archived。归档不删任务，只是不许再开新的一轮。 */
  status: string;
  forkedFrom: string;
  turnCount: number;
  createdAt: string;
  updatedAt: string;
};

/** 一轮的概要。后端刻意给得比 Task 窄：会话列表不需要每轮的目标全文。 */
export type ThreadTurn = {
  id: string;
  title: string;
  goal: string;
  status: string;
  resultSummary: string;
  /** 这一轮贴了什么图。回放时要看得见，否则用户不知道自己当时发了什么。 */
  attachments: AttachmentRef[];
  createdAt: string;
};

/** 一条已上传的附件。库里只存引用，文件本体在 data/uploads 下。 */
export type AttachmentRef = {
  id: string;
  name: string;
  mime: string;
  size: number;
  /** image / text / pdf。后端没给时按 mime 推断，老数据只有图片。 */
  kind?: "image" | "text" | "pdf";
};

export type ThreadDetail = Thread & { turns: ThreadTurn[] };

export function normalizeThread(source: unknown): Thread | null {
  if (!source || typeof source !== "object") return null;
  const raw = source as Record<string, unknown>;
  const id = textOf(raw, "id", "thread_id");
  if (!id) return null;
  return {
    id,
    projectId: textOf(raw, "project_id"),
    title: textOf(raw, "title") || "未命名会话",
    status: textOf(raw, "status") || "active",
    forkedFrom: textOf(raw, "forked_from"),
    turnCount: numOf(raw, "turn_count") ?? 0,
    createdAt: textOf(raw, "created_at"),
    updatedAt: textOf(raw, "updated_at"),
  };
}

export function normalizeThreadTurn(source: unknown): ThreadTurn | null {
  if (!source || typeof source !== "object") return null;
  const raw = source as Record<string, unknown>;
  const id = textOf(raw, "id", "task_id");
  if (!id) return null;
  return {
    id,
    title: textOf(raw, "title") || "未命名轮次",
    goal: textOf(raw, "goal", "prompt"),
    status: normalizeStatus(pick(raw, "status", "state")),
    resultSummary: textOf(raw, "result_summary", "summary"),
    attachments: arrayOf(raw, "attachments")
      .map((item) => {
        const id = textOf(item, "id");
        if (!id) return null;
        return {
          id,
          name: textOf(item, "name") || "图片",
          mime: textOf(item, "mime") || "image/png",
          size: numOf(item, "size") ?? 0,
        } satisfies AttachmentRef;
      })
      .filter((item): item is AttachmentRef => item !== null),
    createdAt: textOf(raw, "created_at"),
  };
}

export function normalizeThreadDetail(source: unknown): ThreadDetail | null {
  const base = normalizeThread(source);
  if (!base) return null;
  const turns = arrayOf(source, "turns")
    .map((item) => normalizeThreadTurn(item))
    .filter((turn): turn is ThreadTurn => turn !== null);
  return { ...base, turns };
}

export function fetchThreads(projectId: string): Promise<Thread[]> {
  return request<unknown[]>(`${api}/projects/${projectId}/threads`, { cache: "no-store" }).then((rows) =>
    (Array.isArray(rows) ? rows : []).map((row) => normalizeThread(row)).filter((thread): thread is Thread => thread !== null),
  );
}

export function fetchThread(threadId: string): Promise<ThreadDetail | null> {
  return request<unknown>(`${api}/threads/${threadId}`, { cache: "no-store" }).then((body) => normalizeThreadDetail(body));
}

export function createThread(projectId: string, input: { title?: string; forkedFrom?: string } = {}): Promise<Thread> {
  const body: Record<string, unknown> = { title: input.title ?? "" };
  if (input.forkedFrom) body.forked_from = input.forkedFrom;
  return request<unknown>(`${api}/projects/${projectId}/threads`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(body),
  }).then((payload) => {
    const thread = normalizeThread(payload) ?? normalizeThread(pick(payload, "thread", "data"));
    if (!thread) throw new ApiError("后端未返回会话信息", "thread_missing_in_response", 502);
    return thread;
  });
}

/** 开新的一轮：创建的是一个普通任务（后端会带上 thread_id / turn_index）。 */
export function createTurn(threadId: string, draft: TaskDraft): Promise<Task> {
  const body: Record<string, unknown> = {
    title: draft.title,
    goal: draft.prompt,
    acceptance_criteria: draft.acceptanceCriteria,
  };
  if (draft.attachments?.length) {
    body.attachments = draft.attachments.map((item) => ({ id: item.id, name: item.name }));
  }
  if (draft.providerId) body.provider_id = draft.providerId;
  return request<unknown>(`${api}/threads/${threadId}/turns`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(body),
  }).then((payload) => {
    const task = normalizeTask(payload) ?? normalizeTask(pick(payload, "task", "data"));
    if (!task) throw new ApiError("后端未返回任务信息", "task_missing_in_response", 502);
    return task;
  });
}

/** 分叉：带着上下文另开一条线。不复制任务——上下文沿 forked_from 回溯。 */
export function forkThread(threadId: string, title = ""): Promise<Thread> {
  return request<unknown>(`${api}/threads/${threadId}/fork`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ title }),
  }).then((payload) => {
    const thread = normalizeThread(payload) ?? normalizeThread(pick(payload, "thread", "data"));
    if (!thread) throw new ApiError("后端未返回会话信息", "thread_missing_in_response", 502);
    return thread;
  });
}

/** 归档，不是删除：删会话会把那些轮次任务的 thread_id 置空，它们就再也串不回去了。 */
export function archiveThread(threadId: string): Promise<void> {
  return request<void>(`${api}/threads/${threadId}`, { method: "DELETE" });
}

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
  skippedReason: string;
};
/** 三级口径里的一级。后端 `model.usage` 事件一次写 call/turn/task 三个，
 *  这里只取面板要用的那几个数，字段缺失照实给 null（老任务根本没这类事件）。 */
export type UsageLevel = {
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  cachedTokens: number | null;
  calls: number | null;
  /** 供应商那一笔请求的 id；空串=这一家没给（本地 Ollama）或这条记录早于该字段上线。 */
  responseId: string;
};
/** 一轮（run）的账：后端 `turn_rollup` 的投影，回答「这一轮花了多少」。 */
export type UsageTurn = {
  runId: string;
  /** 这一轮里出现过几条线程（1 = 只有主代理，>1 = 派过子代理）。 */
  threads: number | null;
  subagentCalls: number | null;
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  cachedTokens: number | null;
  calls: number | null;
};
/** 一条线程（分支）的账 + 它的出身：后端 `branch_rollup` 的投影。 */
export type UsageBranch = {
  traceId: string;
  agentPath: string;
  parentTraceId: string;
  rootTraceId: string;
  runId: string;
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  cachedTokens: number | null;
  calls: number | null;
};
/** 一次并行编排（fan_out）的合计账：后端 `parallel_group_rollup` 的投影。 */
export type UsageParallelGroup = {
  group: string;
  runId: string;
  threads: number | null;
  agentPaths: string[];
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  cachedTokens: number | null;
  calls: number | null;
};
export type UsageInfo = {
  promptTokens: number | null;
  completionTokens: number | null;
  totalTokens: number | null;
  cachedTokens: number | null;
  cacheWriteTokens: number | null;
  reasoningTokens: number | null;
  cacheHitRatio: number | null;
  latencyMs: number | null;
  calls: number | null;
  models: string[];
  levels: { call: UsageLevel | null; turn: UsageLevel | null; task: UsageLevel | null } | null;
  /** null = 后端压根没给这两个键（服务版本早于回合归属）；[] = 给了，且确实没有可归的账。
   *  这两个读法在面板上必须不一样：把「没说」显示成「没有」就是在编事实。 */
  turns: UsageTurn[] | null;
  branches: UsageBranch[] | null;
  /** 归属上线前落下的账有多少笔。老账只以条数出现，绝不并成「第 0 轮」。 */
  unattributedCalls: number | null;
  /** 按并行编排批次的合计（后端 `parallel_group_rollup`）。null = 后端还没有这个键。 */
  parallelGroups: UsageParallelGroup[] | null;
  /** 带批次号的笔数 / 不带的。不带的包括单发子代理、主线程、摘要，也包括批次号上线前的老账。 */
  batchedCalls: number | null;
  unbatchedCalls: number | null;
  /** 花在子代理身上的那些行合计（按账本行现数，不是从图状态里累计出来的那个数）。 */
  delegatedTokens: number | null;
  delegatedCalls: number | null;
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
  /** 独立审查员的话。undefined=这张卡是"审查进载荷之前"的老任务；null=记过了、当时审查关着。 */
  guardian?: GuardianVerdictInfo | null;
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

/** 一步当时被什么策略管着。后端只在全局 digest 变化时落一条，所以这里通常是 1 条 + 若干变更。 */
export type GovernanceChange = {
  step: number | null;
  node: string;
  model: string;
  tier: string;
  mode: string;
};

export type Governance = {
  snapshots: number;
  models: string[];
  modes: string[];
  sandbox: {
    backend: string;
    enabled: boolean | null;
    isolation: string;
    network: boolean | null;
    readOnlyRoot: boolean | null;
    limits: string;
    denied: string;
    /** 现场探测出来的边界强度：os_enforced / job_only / none / unknown。
     * 空串 = 这个任务跑的时候后端还没有这个字段，不能拿今天的探测结果冒充当时。 */
    boundaryStrength: string;
    boundaryStrengthLabel: string;
    enforcementSource: string;
    /** 配置值里哪几项真的被执行了。缺键按"未强制"处理：宁可少说，不许把没拦的说成拦了。 */
    enforced: Record<string, boolean>;
  } | null;
  features: { name: string; on: boolean; source: string }[];
  changes: GovernanceChange[];
};

export type InstructionRef = {
  source: string;
  /** 掩码后的路径：事件会进时间线、证据包和全文索引，主机绝对路径不许出现在这里。 */
  path: string;
  chars: number | null;
  /** 内容指纹（sha256 前 40 位）。指令正文从不入账，指认版本靠它。 */
  sha: string;
  truncated: boolean;
};

/** 环境快照（对标 Codex `world_state`）。后端每条都是当时的全量，所以 current 取最后一条。 */
export type WorldState = {
  snapshots: number;
  current: {
    date: string;
    timezone: string;
    cwd: string;
    /** captured / unknown —— unknown 是那会儿还没装配 shell 环境层，不是"探测失败"。 */
    status: string;
    shell: string;
    os: string;
    python: string;
    pathEntries: number | null;
    contextWindow: number | null;
    triggerAt: number | null;
    mode: string;
    model: string;
    /** 整套指令的版本号（顺序也算在内）。 */
    instructionsDigest: string;
    instructions: InstructionRef[];
  } | null;
  changes: { seq: number | null; node: string }[];
};

/** 一条逐条授权（对标 Codex `turn_context.permission_profile.file_system.entries`）。
 * `enforcedBy` 才是这行的重点：`policy` 是我们自己的闸门在执行前判定，不是系统会拒。 */
export type BoundaryEntry = { path: string; access: string; enforcedBy: string };

export type PermissionProfile = {
  /** managed / unrestricted / unrecorded；空串 = 后端没给这一格（早于 M4 的服务）。 */
  type: string;
  /** unrecorded 时后端会写清"为什么不按档位反推"，原文照抄，不改写。 */
  note: string;
  enforcedBy: string;
  /** 闸门的真实语义（skip / not_applicable）；空串 = 没记。 */
  missingPathBehavior: string;
  fileSystemType: string;
  entries: BoundaryEntry[];
  network: { requested: string; enforcedBy: string; commandGate: string } | null;
  /** null = 载荷里没有 shell 这一格，不能读成"shell 关着"。 */
  shellEnabled: boolean | null;
};

/** 边界报告（evidence.py 的 boundary_report）：这次任务的每一笔调用落在哪一层边界内。 */
export type Boundaries = {
  /** 后端有没有给出 boundaries 这一块。false 时界面只说"没有这份报告"，不铺一串 0。 */
  recorded: boolean;
  tier: string;
  requested: string;
  backend: string;
  strength: string;
  degraded: boolean | null;
  filesystemEnforced: boolean | null;
  networkEnforced: boolean | null;
  notice: string;
  mcp: { boundary: string; note: string; servers: string[]; calls: number | null };
  toolCalls: {
    blocked: number | null;
    mcpHostProcess: number | null;
    process: number | null;
    inProcess: number | null;
    unattributed: number | null;
  };
  sandboxModes: string[];
  profile: PermissionProfile;
};

/** 一次独立审查的判定。`outcome` 是"审完之后走到了哪一步"，缺键就是当时没记（老事件）。 */
export type GuardianDecision = {
  seq: number | null;
  runId: string;
  tool: string;
  approvalKey: string;
  verdict: string;
  reasons: string[];
  model: string;
  latencyMs: number | null;
  timedOut: boolean;
  error: string;
  outcome: string | null;
};

/** 一次压缩里审阅侧的账：看得到几条历史、模型侧剩几条、随账带上了几条判定。 */
export type GuardianWindow = {
  seq: number | null;
  windowNumber: number | null;
  itemsReviewed: number | null;
  itemsInModelContext: number | null;
  reviewedChars: number | null;
  reviews: number | null;
  inEffect: Record<string, string | number>;
};

export type GuardianHistory = {
  recorded: boolean;
  decisions: GuardianDecision[];
  autoApprovals: { seq: number | null; approvalKey: string }[];
  windows: GuardianWindow[];
  /** null = 后端没给（老服务）；0 = 记过了，且每一次压缩都记了。 */
  windowsUnrecorded: number | null;
};

/** 审批卡上的审查意见。三种状态靠 `guardian` 这个键在不在、是不是 null 区分。 */
export type GuardianVerdictInfo = {
  verdict: string;
  reasons: string[];
  model: string;
  latencyMs: number | null;
  timedOut: boolean;
  error: string;
};

export type Evidence = {
  status: string;
  acceptance: AcceptanceItem[];
  changes: ChangeItem[];
  test: TestInfo | null;
  usage: UsageInfo;
  governance: Governance;
  world: WorldState;
  boundaries: Boundaries;
  guardian_history: GuardianHistory;
  pendingApprovals: PendingApproval[];
  pendingQuestions: PendingQuestion[];
  tools: ToolRecord[];
  timeline: AgentEvent[];
  unverified: string[];
  raw: Record<string, unknown>;
};

const EMPTY_GOVERNANCE: Governance = { snapshots: 0, models: [], modes: [], sandbox: null, features: [], changes: [] };

const EMPTY_WORLD: WorldState = { snapshots: 0, current: null, changes: [] };

/** 计数一律 null：这一块后端根本没给，界面只能念「没有这份报告」，不能念一串 0。 */
const EMPTY_BOUNDARIES: Boundaries = {
  recorded: false,
  tier: "",
  requested: "",
  backend: "",
  strength: "",
  degraded: null,
  filesystemEnforced: null,
  networkEnforced: null,
  notice: "",
  mcp: { boundary: "", note: "", servers: [], calls: null },
  toolCalls: { blocked: null, mcpHostProcess: null, process: null, inProcess: null, unattributed: null },
  sandboxModes: [],
  profile: {
    type: "",
    note: "",
    enforcedBy: "",
    missingPathBehavior: "",
    fileSystemType: "",
    entries: [],
    network: null,
    shellEnabled: null,
  },
};

const EMPTY_GUARDIAN_HISTORY: GuardianHistory = {
  recorded: false,
  decisions: [],
  autoApprovals: [],
  windows: [],
  windowsUnrecorded: null,
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
    cachedTokens: null,
    cacheWriteTokens: null,
    reasoningTokens: null,
    cacheHitRatio: null,
    latencyMs: null,
    calls: null,
    models: [],
    levels: null,
    turns: null,
    branches: null,
    unattributedCalls: null,
    parallelGroups: null,
    batchedCalls: null,
    unbatchedCalls: null,
    delegatedTokens: null,
    delegatedCalls: null,
  },
  governance: EMPTY_GOVERNANCE,
  world: EMPTY_WORLD,
  boundaries: EMPTY_BOUNDARIES,
  guardian_history: EMPTY_GUARDIAN_HISTORY,
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
    return { command: source, exitCode: null, output: "", passed: null, status: "", skippedReason: "" };
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
    skippedReason: textOf(record, "skipped_reason", "skippedReason"),
  };
}

function normalizeUsageLevel(source: unknown): UsageLevel | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const record = source as Record<string, unknown>;
  const prompt = numOf(record, "prompt_tokens", "input_tokens", "promptTokens");
  const completion = numOf(record, "completion_tokens", "output_tokens", "completionTokens");
  return {
    promptTokens: prompt,
    completionTokens: completion,
    totalTokens:
      numOf(record, "total_tokens", "totalTokens") ??
      (prompt === null && completion === null ? null : (prompt ?? 0) + (completion ?? 0)),
    cachedTokens: numOf(record, "cached_tokens", "cachedTokens", "cache_read_input_tokens"),
    calls: numOf(record, "calls", "request_count"),
    responseId: textOf(record, "response_id", "responseId"),
  };
}

function normalizeUsageTurn(source: unknown): UsageTurn | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const record = source as Record<string, unknown>;
  return {
    runId: textOf(record, "run_id", "runId"),
    threads: numOf(record, "threads", "thread_count"),
    subagentCalls: numOf(record, "subagent_calls", "subagentCalls"),
    promptTokens: numOf(record, "prompt_tokens", "promptTokens"),
    completionTokens: numOf(record, "completion_tokens", "completionTokens"),
    // 汇总数只在后端真给了的时候才存在。这里不拿 prompt+completion 顶：
    // 顶上去的数看不出是算的还是记的，对不上账时反而更难查。
    totalTokens: numOf(record, "total_tokens", "totalTokens"),
    cachedTokens: numOf(record, "cached_tokens", "cachedTokens"),
    calls: numOf(record, "calls"),
  };
}

function normalizeUsageBranch(source: unknown): UsageBranch | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const record = source as Record<string, unknown>;
  return {
    traceId: textOf(record, "trace_id", "traceId"),
    agentPath: textOf(record, "agent_path", "agentPath"),
    parentTraceId: textOf(record, "parent_thread_id", "parent_trace_id", "parentTraceId"),
    rootTraceId: textOf(record, "root_thread_id", "root_trace_id", "rootTraceId"),
    runId: textOf(record, "run_id", "runId"),
    promptTokens: numOf(record, "prompt_tokens", "promptTokens"),
    completionTokens: numOf(record, "completion_tokens", "completionTokens"),
    totalTokens: numOf(record, "total_tokens", "totalTokens"),
    cachedTokens: numOf(record, "cached_tokens", "cachedTokens"),
    calls: numOf(record, "calls"),
  };
}

function normalizeUsageParallelGroup(source: unknown): UsageParallelGroup | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const record = source as Record<string, unknown>;
  return {
    group: textOf(record, "group", "parallel_group"),
    runId: textOf(record, "run_id", "runId"),
    threads: numOf(record, "threads", "thread_count"),
    agentPaths: arrayOf(record, "agent_paths", "agentPaths").map((item) => String(item)),
    promptTokens: numOf(record, "prompt_tokens", "promptTokens"),
    completionTokens: numOf(record, "completion_tokens", "completionTokens"),
    totalTokens: numOf(record, "total_tokens", "totalTokens"),
    cachedTokens: numOf(record, "cached_tokens", "cachedTokens"),
    calls: numOf(record, "calls"),
  };
}

/** 列表型汇总的读法：键不在就是 null（后端没说），别用空数组冒充「后端说了没有」。 */
function normalizeUsageRollup<T>(source: unknown, key: string, alt: string, item: (value: unknown) => T | null): T[] | null {
  const value = pick<unknown>(source, key, alt);
  if (!Array.isArray(value)) return null;
  return value.map(item).filter((entry): entry is T => entry !== null);
}

function normalizeUsage(container: unknown): UsageInfo {
  const record = recordOf(container, "usage", "token_usage", "tokens");
  const prompt = numOf(record, "prompt_tokens", "input_tokens", "promptTokens");
  const completion = numOf(record, "completion_tokens", "output_tokens", "completionTokens");
  const total = numOf(record, "total_tokens", "totalTokens") ?? (prompt !== null || completion !== null ? (prompt ?? 0) + (completion ?? 0) : null);
  const levels = pick<Record<string, unknown>>(record, "levels", "usage_levels");
  return {
    promptTokens: prompt,
    completionTokens: completion,
    totalTokens: total,
    // 缓存与推理单独成项：「打开缓存开关」和「缓存真的命中」的区别，只有把这两个数
    // 摊在输入旁边才看得见；后端没给（老任务）就是 null，面板照实显示「暂无」。
    cachedTokens: numOf(record, "cached_tokens", "cachedTokens", "cache_read_input_tokens"),
    cacheWriteTokens: numOf(record, "cache_write_tokens", "cacheWriteTokens"),
    reasoningTokens: numOf(record, "reasoning_tokens", "reasoningTokens", "reasoning_output_tokens"),
    cacheHitRatio: numOf(record, "cache_hit_ratio", "cacheHitRatio"),
    latencyMs: numOf(record, "latency_ms", "latency", "duration_ms", "total_latency_ms"),
    calls: numOf(record, "calls", "request_count"),
    models: arrayOf(record, "models", "model").map((item) => String(item)),
    levels: levels
      ? {
          call: normalizeUsageLevel(levels["call"]),
          turn: normalizeUsageLevel(levels["turn"]),
          task: normalizeUsageLevel(levels["task"]),
        }
      : null,
    // 回合归属的两张表直接从账本行汇总（后端 evidence.turn_rollup / branch_rollup），
    // 前端不再从事件 payload 里推一遍：两处各推一次早晚对不上，且对不上时看不出错在哪边。
    turns: normalizeUsageRollup(record, "turns", "usage_turns", normalizeUsageTurn),
    branches: normalizeUsageRollup(record, "branches", "usage_branches", normalizeUsageBranch),
    unattributedCalls: numOf(record, "unattributed_calls", "unattributedCalls"),
    // 并行批次的账：一次 fan_out 花了多少，由后端按账本行的批次号现数（evidence.parallel_group_rollup）。
    parallelGroups: normalizeUsageRollup(record, "parallel_groups", "parallelGroups", normalizeUsageParallelGroup),
    batchedCalls: numOf(record, "batched_calls", "batchedCalls"),
    unbatchedCalls: numOf(record, "unbatched_calls", "unbatchedCalls"),
    delegatedTokens: numOf(record, "delegated_tokens", "delegatedTokens"),
    delegatedCalls: numOf(record, "delegated_calls", "delegatedCalls"),
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
    .map((item) => {
      // 三种状态各有各的读法，所以不能用 pick（它把 null 折成 undefined）：
      // 键不在=这张卡早于审查进载荷；null=记过了、当时审查关着；对象=审查员真说了话。
      const guardian = guardianFromPayload(item);
      return {
        toolCallId: textOf(item, "tool_call_id", "id", "approval_id"),
        tool: textOf(item, "tool", "name", "action") || "未知操作",
        args: recordOf(item, "arguments", "args", "input", "params"),
        risk: textOf(item, "risk_level", "risk", "level"),
        message: textOf(item, "message", "reason", "summary", "approval_state"),
        prefix: textOf(item, "suggested_prefix", "prefix"),
        scopes: normalizeApprovalScopes(item),
        mode: textOf(item, "permission_mode", "mode"),
        ...(guardian === undefined ? {} : { guardian }),
      };
    });
}

/** 审批载荷上的审查意见（三态）。时间线和待批列表共用这一个读法，免得两处判歪。 */
export function guardianFromPayload(source: Record<string, unknown>): GuardianVerdictInfo | null | undefined {
  if (!("guardian" in source)) return undefined;
  return normalizeGuardianVerdict(source["guardian"]);
}

function normalizeGuardianVerdict(source: unknown): GuardianVerdictInfo | null {
  if (!source || typeof source !== "object") return null;
  const raw = source as Record<string, unknown>;
  return {
    verdict: textOf(raw, "verdict"),
    reasons: arrayOf(raw, "reasons").map((item) => (typeof item === "string" ? item : "")).filter(Boolean),
    model: textOf(raw, "model"),
    latencyMs: numOf(raw, "latency_ms"),
    timedOut: boolOf(raw, "timed_out") === true,
    error: textOf(raw, "error"),
  };
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

/** 治理快照的投影。老任务没有 turn.context 事件，后端返回 snapshots:0 —— 这里就照实空着，
 * 不能拿当前设置去"补"一份，那等于把今天的策略冒充成执行那天的策略。 */
function normalizeGovernance(source: unknown): Governance {
  const block = recordOf(source, "governance");
  if (Object.keys(block).length === 0) return EMPTY_GOVERNANCE;
  const first = recordOf(block, "first");
  const sandbox = recordOf(first, "sandbox");
  const enforcedRaw = recordOf(sandbox, "enforced");
  const enforced: Record<string, boolean> = {};
  for (const [key, value] of Object.entries(enforcedRaw)) {
    if (typeof value === "boolean") enforced[key] = value;
  }
  // 展示值与生效值分不开，就是这个面板最早撒的谎：cpus 一直显示 1.0，可没有任何东西在执行它。
  // 每一项后面标出"谁在执行"，标不出来就标"未强制"。timeout 由我们自己的计时器执行，
  // 不是内核强制，所以它不进这张表——它没有"配置了但没人管"这一档。
  const limitKeys: [string, string][] = [
    ["cpus", "cpu"],
    ["memory", "memory"],
    ["pids", "pids"],
    ["tmpfs", "filesystem"],
    ["timeout_seconds", ""],
  ];
  const limits = limitKeys
    .map(([key, field]) => {
      const value = sandbox[key];
      if (value === undefined || value === null || value === "") return "";
      const mark = field === "" || enforced[field] ? "" : "（未强制）";
      return `${key} ${String(value)}${mark}`;
    })
    .filter(Boolean)
    .join(" · ");
  const features = Object.entries(recordOf(first, "features"))
    .map(([name, value]) => ({
      name,
      on: boolOf(value, "on") === true,
      source: textOf(value, "source"),
    }))
    .filter((item) => item.on || item.source === "env");
  // 后端把 features 摊成 {name:{on,source}}，这里换成数组方便渲染；只留开着的
  // 和被环境变量强制关掉的——8 个全显示是噪音，但"你以为开着其实被 env 关了"必须看得见。
  const list = (key: string): string[] => arrayOf(block, key).map((item) => (typeof item === "string" ? item : ""));
  return {
    snapshots: numOf(block, "snapshots") ?? 0,
    models: list("models"),
    modes: list("modes"),
    sandbox: Object.keys(sandbox).length === 0 ? null : {
      backend: textOf(sandbox, "backend"),
      enabled: boolOf(sandbox, "enabled"),
      isolation: textOf(sandbox, "isolation"),
      network: boolOf(sandbox, "network"),
      readOnlyRoot: boolOf(sandbox, "read_only_root"),
      limits,
      denied: textOf(sandbox, "denied"),
      boundaryStrength: textOf(sandbox, "boundary_strength"),
      boundaryStrengthLabel: textOf(sandbox, "boundary_strength_label"),
      enforcementSource: textOf(sandbox, "enforcement_source"),
      enforced,
    },
    features,
    changes: arrayOf(block, "changes").map((item) => ({
      step: numOf(item, "step"),
      node: textOf(item, "node"),
      model: textOf(item, "model"),
      tier: textOf(item, "tier"),
      mode: textOf(item, "permission_mode"),
    })),
  };
}

/** 环境快照的投影（对标 Codex `world_state{full:true}`）。
 *
 * 老任务没有 world.state 事件 → snapshots 0、current null，界面显示「暂无」。
 * 不许拿今天的目录/shell 去补一份：那等于把现在的环境冒充成执行那天的环境。
 * 数字缺失一律 `null`，不给 0 —— "当时没记"和"当时是 0 条 PATH"是两件事。 */
function normalizeWorld(source: unknown): WorldState {
  const block = recordOf(source, "world");
  if (Object.keys(block).length === 0) return EMPTY_WORLD;
  const payload = recordOf(block, "current");
  const state = recordOf(payload, "state");
  if (Object.keys(state).length === 0) {
    return { snapshots: numOf(block, "snapshots") ?? 0, current: null, changes: [] };
  }
  const local = recordOf(recordOf(state, "environments"), "local");
  const mode = recordOf(state, "operating_mode");
  const guidance = recordOf(state, "context_window_guidance");
  return {
    snapshots: numOf(block, "snapshots") ?? 0,
    current: {
      date: textOf(state, "current_date"),
      timezone: textOf(state, "timezone"),
      cwd: textOf(local, "cwd"),
      status: textOf(local, "status"),
      shell: textOf(local, "shell"),
      os: textOf(local, "os"),
      python: textOf(local, "python"),
      pathEntries: numOf(local, "path_entries"),
      contextWindow: numOf(guidance, "context_window"),
      triggerAt: numOf(guidance, "trigger_at"),
      mode: textOf(mode, "mode"),
      model: textOf(mode, "model"),
      instructionsDigest: textOf(mode, "instructions"),
      instructions: arrayOf(state, "instructions").map((item) => ({
        source: textOf(item, "source"),
        path: textOf(item, "path"),
        chars: numOf(item, "chars"),
        sha: textOf(item, "sha"),
        truncated: boolOf(item, "truncated") === true,
      })),
    },
    changes: arrayOf(block, "changes").map((item) => ({
      seq: numOf(item, "seq"),
      node: textOf(item, "node"),
    })),
  };
}

/** 边界报告的投影（对标 Codex `turn_context` 里那半张权限表）。
 *
 * 读法只有一条纪律：**这份报告不存在 ≠ 报告里说什么都没发生**。
 * 老服务不返回 `boundaries` → recorded=false，界面只念「后端没给边界报告」；
 * 返回了但某个计数缺键 → null（未记），不许补 0——0 是"一次都没有"这个事实。 */
function normalizeBoundaries(source: unknown): Boundaries {
  const block = recordOf(source, "boundaries");
  if (Object.keys(block).length === 0) return EMPTY_BOUNDARIES;
  const sandbox = recordOf(block, "sandbox");
  const mcp = recordOf(block, "mcp");
  const counts = recordOf(block, "tool_calls");
  const profile = recordOf(block, "permission_profile");
  const fs = recordOf(profile, "file_system");
  const network = recordOf(profile, "network");
  const shell = profile["shell"];
  return {
    recorded: true,
    tier: textOf(sandbox, "tier"),
    requested: textOf(sandbox, "requested"),
    backend: textOf(sandbox, "backend"),
    strength: textOf(sandbox, "strength"),
    degraded: boolOf(sandbox, "degraded"),
    filesystemEnforced: boolOf(sandbox, "filesystem_enforced"),
    networkEnforced: boolOf(sandbox, "network_enforced"),
    notice: textOf(sandbox, "notice"),
    mcp: {
      boundary: textOf(mcp, "boundary"),
      note: textOf(mcp, "note"),
      servers: arrayOf(mcp, "servers").map((item) => (typeof item === "string" ? item : textOf(item, "name"))).filter(Boolean),
      calls: numOf(mcp, "calls"),
    },
    toolCalls: {
      blocked: numOf(counts, "blocked"),
      mcpHostProcess: numOf(counts, "mcp_host_process"),
      process: numOf(counts, "process"),
      inProcess: numOf(counts, "in_process"),
      unattributed: numOf(counts, "unattributed"),
    },
    sandboxModes: arrayOf(block, "sandbox_modes").map((item) => (typeof item === "string" ? item : "")).filter(Boolean),
    profile: {
      type: textOf(profile, "type"),
      note: textOf(profile, "note"),
      enforcedBy: textOf(profile, "enforced_by"),
      missingPathBehavior: textOf(profile, "missing_path_behavior"),
      fileSystemType: textOf(fs, "type"),
      entries: arrayOf(fs, "entries").map((item) => ({
        path: textOf(item, "path"),
        access: textOf(item, "access"),
        enforcedBy: textOf(item, "enforced_by"),
      })),
      network: Object.keys(network).length === 0 ? null : {
        requested: textOf(network, "requested"),
        enforcedBy: textOf(network, "enforced_by"),
        commandGate: textOf(network, "command_gate"),
      },
      shellEnabled: typeof shell === "object" && shell !== null ? boolOf(shell, "enabled") : null,
    },
  };
}

/** 审阅轨迹的投影（#79 的 `guardian_history`）。
 *
 * 判定只从事件来，界面再抄一遍就成了第三个口径，所以这里只搬字段、不做任何推断：
 * `outcome` 缺键保持 null（当时没记去向），`windows_unrecorded` 缺键保持 null（老服务）。 */
function normalizeGuardianHistory(source: unknown): GuardianHistory {
  const block = recordOf(source, "guardian_history");
  if (Object.keys(block).length === 0) return EMPTY_GUARDIAN_HISTORY;
  return {
    recorded: true,
    decisions: arrayOf(block, "decisions").map((item) => ({
      seq: numOf(item, "seq"),
      runId: textOf(item, "run_id", "run"),
      tool: textOf(item, "tool"),
      approvalKey: textOf(item, "approval_key"),
      verdict: textOf(item, "verdict"),
      reasons: arrayOf(item, "reasons").map((line) => (typeof line === "string" ? line : "")).filter(Boolean),
      model: textOf(item, "model"),
      latencyMs: numOf(item, "latency_ms"),
      timedOut: boolOf(item, "timed_out") === true,
      error: textOf(item, "error"),
      outcome: "outcome" in (item as object) ? textOf(item, "outcome") || null : null,
    })),
    autoApprovals: arrayOf(block, "auto_approvals").map((item) => ({
      seq: numOf(item, "seq"),
      approvalKey: textOf(item, "approval_key"),
    })),
    windows: arrayOf(block, "windows").map((item) => ({
      seq: numOf(item, "seq"),
      windowNumber: numOf(item, "window_number"),
      itemsReviewed: numOf(item, "items_reviewed"),
      itemsInModelContext: numOf(item, "items_in_model_context"),
      reviewedChars: numOf(item, "reviewed_chars"),
      reviews: numOf(item, "reviews"),
      inEffect: Object.fromEntries(
        Object.entries(recordOf(item, "in_effect")).filter(
          ([, value]) => typeof value === "string" || typeof value === "number",
        ) as [string, string | number][],
      ),
    })),
    windowsUnrecorded: numOf(block, "windows_unrecorded"),
  };
}

/** 边界强度的那半句。空串是老任务：那时后端还没探测过，不能拿今天的值冒充当时。 */
const STRENGTH_SEGMENT: Record<string, string> = {
  os_enforced: "文件与网络边界由系统强制",
  job_only: "只有内存/进程数/进程树被内核强制",
  none: "无任何内核强制",
  unknown: "边界探测装置不可信，不承诺任何边界",
};

/** 边界那一行的文案。放在这里而不是组件里：它是判定，得能被断言。
 * 「local」会被读成有一种沙箱叫 local，它真正的意思是没开隔离——这层意思不能省。
 * 强度段是后加的：没有它，"宿主直连"后面跟着 memory 512m，用户会以为那个数字有人在执行。 */
export function sandboxBoundaryLabel(sb: Governance["sandbox"]): string {
  if (!sb) return "暂无";
  // 档位说的是"这台机器拦得住什么"，不是"这次执行被拦了什么"。
  // 关档跑的时候 boundary_strength 依然是 job_only（探测没坏，机器确实能建 Job），
  // 直接把它接在「沙箱未启用」后面就成了一句谎——这条是点验时在真页面上撞出来的。
  const strength = STRENGTH_SEGMENT[sb.boundaryStrength] ?? "";
  const enforcedAny = Object.values(sb.enforced).some((value) => value === true);
  return [
    sb.enabled === false
      ? "沙箱未启用"
      : sb.backend === "none"
        ? "无可用沙箱后端"
        // `local` 不是沙箱的名字，是"没起容器、进程就在这个宿主上"。直接把代号印出来，
        // 读者会以为有一种叫 local 的隔离在保护他——docker 那格可以印代号，因为它真是容器。
        : sb.backend === "local"
          ? "无容器（进程在本机）"
          : sb.backend,
    sb.isolation === "container" ? "容器隔离" : "宿主直连（不承诺隔离）",
    sb.network === null ? "" : sb.network ? "可联网" : "断网",
    sb.readOnlyRoot === null ? "" : sb.readOnlyRoot ? "根目录只读" : "根目录可写",
    strength === "" || enforcedAny ? strength : "本次未强制任何限额",
    sb.enforcementSource === "docker_flags" ? "限额由容器参数提交，未在本机实测" : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/** 边界行要不要显警告色。没隔离、没后端、能联网，三种都够用户重新决定要不要继续。
 * 强度已知但不是 os_enforced 也一样要警告：那说明文件与网络只是策略级约束，
 * 模型把路径藏进脚本字符串里时没有任何东西会拦。 */
export function sandboxBoundaryNeedsWarn(sb: Governance["sandbox"]): boolean {
  if (!sb) return true;
  if (sb.enabled === false || sb.backend === "none" || sb.network === true) return true;
  if (sb.isolation !== "container" && sb.boundaryStrength !== "" && sb.boundaryStrength !== "os_enforced") {
    return true;
  }
  return false;
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
    governance: normalizeGovernance(container),
    world: normalizeWorld(container),
    boundaries: normalizeBoundaries(container),
    guardian_history: normalizeGuardianHistory(container),
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

export type LiveDelta = {
  node: string;
  text: string;
  chars: number;
  truncated: boolean;
  done: boolean;
};

export type StreamHandlers = {
  onEvent: (event: AgentEvent) => void;
  /** 实时增量：模型正在说的话，允许丢，不进时间线。 */
  onDelta?: (delta: LiveDelta) => void;
  onDone?: () => void;
  signal: AbortSignal;
};

function parseDelta(data: string): LiveDelta | null {
  const parsed = parseJson(data);
  if (!parsed || typeof parsed !== "object") return null;
  const row = parsed as Record<string, unknown>;
  if (typeof row.text !== "string") return null;
  return {
    node: typeof row.node === "string" ? row.node : "",
    text: row.text,
    chars: typeof row.chars === "number" ? row.chars : row.text.length,
    truncated: row.truncated === true,
    done: row.done === true,
  };
}

function emitFrame(data: string, onEvent: StreamHandlers["onEvent"]): void {
  if (!data.trim()) return;
  const parsed = parseJson(data);
  if (!parsed) {
    return;
  }
  for (const event of normalizeEvents(parsed)) onEvent(event);
}

/** 一帧 SSE：增量走 onDelta，其余当持久事件处理。 */
function emitChunk(frame: { event: string; data: string }, handlers: StreamHandlers): void {
  if (frame.event === "done" || frame.event === "keepalive" || frame.event === "ping") return;
  if (frame.event === "delta") {
    const delta = parseDelta(frame.data);
    if (delta) handlers.onDelta?.(delta);
    return;
  }
  emitFrame(frame.data, handlers.onEvent);
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
      for (const frame of drained.events) emitChunk(frame, handlers);
    }
    buffer += decoder.decode().replace(/\r/g, "");
    for (const frame of drainSse(`${buffer}\n\n`).events) emitChunk(frame, handlers);
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    throw error;
  } finally {
    reader.releaseLock();
  }
  handlers.onDone?.();
}
