/**
 * 团队 Bot 的 HTTP 面。
 *
 * 一次群聊 = 一轮 TeamRound + 若干条 TeamMessage；每条 Bot 消息背后仍是一条
 * 普通任务，所以这里不另造运行/审批接口，只在前端把群消息与任务状态对齐。
 */

import { API_V1, jsonInit, request } from "@/lib/http";

const api = API_V1;

export type PermissionMode = "default" | "accept_edits" | "plan" | "bypass";
export type ApprovalScope = "once" | "prefix" | "tool";

export type TeamBot = {
  id: string;
  name: string;
  role: string;
  instructions: string;
  provider_id: string;
  provider_name: string;
  model: string;
  workspace_id: string;
  workspace_label: string;
  workspace_path: string;
  permission_mode: PermissionMode;
  max_steps: number;
  is_admin: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type TeamGroupSummary = {
  id: string;
  project_id: string;
  name: string;
  topic: string;
  admin_bot_id: string;
  max_speakers: number;
  token_budget: number;
  member_count: number;
  last_message: string;
  round_status: string;
  created_at: string;
  updated_at: string;
  project_name?: string;
};

export type TeamMember = {
  id: string;
  bot_id: string;
  name: string;
  role: string;
  position: number;
  enabled: boolean;
  thread_id: string;
  provider_id: string;
  provider_name: string;
  model: string;
  workspace_id: string;
  workspace_label: string;
  permission_mode: PermissionMode;
  max_steps: number;
  is_admin: boolean;
};

export type RoundLedgerUsage = {
  origin_run_id: string;
  tokens: number;
  calls: number;
  tasks: number;
  runs: number;
  subagent_calls: number;
  by_bot: { bot_id: string; bot_name: string; tokens: number; calls: number; subagent_calls: number }[];
};

export type GroupLedgerUsage = Omit<RoundLedgerUsage, "origin_run_id">;

export type TeamRound = {
  id: string;
  group_id: string;
  user_message_id: number;
  status: string;
  queue: string[];
  spoken: string[];
  // 编排器自己累加的轮计数：只在发言收尾时加。本轮的真实花费看 `usage`（账本口径）。
  tokens: number;
  // 按发起轮（origin_run_id）从账本筛出来的本轮合计，跨成员任务。缺这个字段的是旧接口响应。
  usage?: RoundLedgerUsage;
  wrap_up_done: boolean;
  error: string;
  created_at: string;
  updated_at: string;
};

export type TeamMessage = {
  id: number;
  group_id: string;
  round_id: string;
  role: "user" | "bot" | "system";
  bot_id: string;
  bot_name: string;
  content: string;
  status: string;
  task_id: string;
  meta: {
    provider?: string;
    model?: string;
    wrap_up?: boolean;
    mentions?: string[];
    usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
    steps?: number;
    error?: string;
    pending?: { approvals?: number; questions?: number };
    [key: string]: unknown;
  };
  created_at: string;
};

export type ApprovalOption = { scope: ApprovalScope; label: string; warning?: string };

export type TeamApproval = {
  tool_call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  risk_level: string;
  message: string;
  expires_at?: string;
  // 过期与否由服务端判定（它和拒绝审批用的是同一个函数）。前端不再自己解析时间串，
  // 否则会出现「卡片说待审批、点下去 409」。
  expired?: boolean;
  approval_state?: string;
  command_prefix?: string;
  suggested_prefix?: string;
  approval_options?: ApprovalOption[];
  // 载荷里还有 guardian（独立审查的原始形状）。三态：缺键=这张卡早于审查进载荷、
  // null=审查关着、对象=审查员说了话。一律用 tasks/api 的 guardianFromPayload 读，
  // 这里不声明成归一化形状，免得把没归一化的东西当成归一化过的用。
};

export type TeamQuestion = {
  id: string;
  question: string;
  header?: string;
  options?: { label: string; description?: string }[];
  multiSelect?: boolean;
  allowFreeText?: boolean;
  allow_free_text?: boolean;
};

export type PendingTurn = {
  message_id: number;
  task_id: string;
  bot_id: string;
  bot_name: string;
  approvals: TeamApproval[];
  questions: TeamQuestion[];
  question_expires_at: string;
  question_expired?: boolean;
};

export type TeamGroupState = {
  group: TeamGroupSummary;
  members: TeamMember[];
  round: TeamRound | null;
  messages: TeamMessage[];
  pending: PendingTurn[];
  // 全群累计（跨所有轮）的账本口径：连没有发言行的钱也算，与 round.usage 各说各的。
  usage: GroupLedgerUsage;
  usage_by_bot: GroupLedgerUsage["by_bot"];
};

export type ChatSearchHit = {
  id: number;
  source: "team_message" | "task" | string;
  group_id: string;
  group_name: string;
  thread_id: string;
  task_id: string;
  message_id: number;
  speaker: string;
  role: string;
  snippet: string;
  content: string;
  score: number;
  created_at: string;
};

export type ChatSearchResult = {
  query: string;
  tokens: string[];
  results: ChatSearchHit[];
  total: number;
  truncated: boolean;
};

export type TeamProvider = {
  id: string;
  name: string;
  kind: string;
  model: string;
  supports_tools: boolean;
  supports_vision: boolean;
  enabled: boolean;
};

export type TeamProject = {
  id: string;
  name: string;
  workspaces: { id: string; label: string; path: string; is_active: boolean }[];
};

export type TeamLimits = {
  max_bots: number;
  max_members: number;
  max_queue_depth: number;
  max_speakers: number;
  min_token_budget: number;
  max_token_budget: number;
  default_token_budget: number;
  pass_marker: string;
};

export type TeamBoot = {
  bots: TeamBot[];
  groups: TeamGroupSummary[];
  providers: TeamProvider[];
  projects: TeamProject[];
  limits: TeamLimits;
  admin_name: string;
};

export type BotInput = {
  name: string;
  role?: string;
  instructions?: string;
  provider_id?: string | null;
  workspace_id?: string | null;
  permission_mode?: PermissionMode;
  max_steps?: number;
  enabled?: boolean;
};

export type GroupInput = {
  project_id?: string;
  name: string;
  topic?: string;
  member_ids?: string[];
  max_speakers?: number;
  token_budget?: number;
};

export function fetchTeamBoot(): Promise<TeamBoot> {
  return request<TeamBoot>(`${api}/team/bootstrap`, { cache: "no-store" });
}

export function createTeamBot(input: BotInput): Promise<TeamBot> {
  return request<TeamBot>(`${api}/team/bots`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function updateTeamBot(botId: string, input: Partial<BotInput>): Promise<TeamBot> {
  return request<TeamBot>(`${api}/team/bots/${botId}`, {
    method: "PATCH",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function deleteTeamBot(botId: string): Promise<void> {
  return request<void>(`${api}/team/bots/${botId}`, { method: "DELETE" });
}

export function createTeamGroup(input: GroupInput): Promise<TeamGroupState> {
  return request<TeamGroupState>(`${api}/team/groups`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function updateTeamGroup(groupId: string, input: Partial<GroupInput>): Promise<TeamGroupState> {
  return request<TeamGroupState>(`${api}/team/groups/${groupId}`, {
    method: "PATCH",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function deleteTeamGroup(groupId: string): Promise<void> {
  return request<void>(`${api}/team/groups/${groupId}`, { method: "DELETE" });
}

export function fetchTeamGroupState(
  groupId: string,
  afterId?: number,
  aroundId?: number,
): Promise<TeamGroupState> {
  const params = new URLSearchParams();
  if (afterId && afterId > 0) params.set("after_id", String(afterId));
  if (aroundId && aroundId > 0) params.set("around_id", String(aroundId));
  const query = params.size ? `?${params.toString()}` : "";
  return request<TeamGroupState>(`${api}/team/groups/${groupId}/state${query}`, { cache: "no-store" });
}

export function searchTeamChat(
  q: string,
  options: { projectId?: string; groupId?: string; limit?: number } = {},
): Promise<ChatSearchResult> {
  const params = new URLSearchParams({ q });
  if (options.projectId) params.set("project_id", options.projectId);
  if (options.groupId) params.set("group_id", options.groupId);
  if (options.limit) params.set("limit", String(options.limit));
  return request<ChatSearchResult>(`${api}/team/search?${params.toString()}`, { cache: "no-store" });
}

export function sendTeamMessage(groupId: string, content: string): Promise<TeamGroupState> {
  return request<TeamGroupState>(`${api}/team/groups/${groupId}/messages`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ content }),
  });
}

export function stopTeamRound(groupId: string): Promise<{ stopped: boolean; status: string }> {
  return request<{ stopped: boolean; status: string }>(`${api}/team/groups/${groupId}/stop`, {
    method: "POST",
  });
}

export function decideTeamApproval(
  messageId: number,
  approve: boolean,
  scope: ApprovalScope = "once",
): Promise<unknown> {
  return request<unknown>(`${api}/team/messages/${messageId}/approval`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ approve, scope }),
  });
}

export function answerTeamQuestion(
  messageId: number,
  answers: Record<string, string>,
): Promise<unknown> {
  return request<unknown>(`${api}/team/messages/${messageId}/answer`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ answers }),
  });
}
