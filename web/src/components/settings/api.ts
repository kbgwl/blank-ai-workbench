import { API_V1, ApiError, deltaText, drainSse, errorMessage, request, toApiError, parseJson } from "@/lib/http";

export type Provider = {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  model: string;
  temperature: number;
  max_tokens: number;
  context_window: number;
  supports_tools: boolean;
  supports_vision: boolean;
  enabled: boolean;
  api_key_hint: string;
  /** 推理档位，minimal|low|medium|high，空串表示不启用 */
  reasoning_effort: string;
  /** 输出详略，low|medium|high，空串表示不启用 */
  verbosity: string;
  /** 总参数（十亿）。决定显存，0 表示未填。 */
  total_params_b: number;
  /** 激活参数（十亿）。决定算力与单价，稠密模型等于总参数，0 表示未填。 */
  active_params_b: number;
};

export type ProviderCreate = {
  name: string;
  kind: string;
  base_url: string;
  api_key: string;
  model: string;
  temperature: number;
  max_tokens: number;
  context_window: number;
  supports_tools: boolean;
  supports_vision: boolean;
  enabled: boolean;
  reasoning_effort: string;
  verbosity: string;
  total_params_b: number;
  active_params_b: number;
};

export type ProviderPatch = Partial<Omit<ProviderCreate, "name">>;

export type Preset = {
  kind: string;
  base_url: string;
  model: string;
  supports_tools: boolean;
  supports_vision?: boolean;
};

export type PresetsResponse = {
  presets: Record<string, Preset>;
  kinds: string[];
};

export type Capabilities = {
  provider?: string;
  kind?: string;
  model?: string;
  streaming?: boolean;
  tools?: boolean;
  vision?: boolean;
  structured_output?: boolean;
};

export type TestResult = {
  ok: boolean;
  code?: string;
  message?: string;
  model?: string;
  latency_ms?: number;
  usage?: { prompt_tokens: number; completion_tokens: number };
  capabilities?: Capabilities;
};

export type UsageRow = {
  provider: string;
  model: string;
  calls: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_latency_ms: number;
  cost?: { amount: number; currency: string; priced: boolean };
  success_rate?: number;
  avg_latency_ms?: number;
};

export type UsageSummary = {
  providers: UsageRow[];
  total_calls: number;
  success_rate?: number;
  avg_latency_ms?: number;
  total_cost?: { amount: number; currency: string };
};

export type ObservabilitySummary = {
  traces: number;
  model_calls: number;
  roles: Record<string, { calls: number; failed: number; prompt_tokens: number; completion_tokens: number; latency_ms: number }>;
  models: Record<string, { calls: number; failed: number; latency_ms: number }>;
  runs: Record<string, number>;
  tools: Record<string, number>;
};

export type SafetySettings = {
  shell_enabled: boolean;
  network_enabled: boolean;
  confirm_all_commands: boolean;
  restrict_to_workspace: boolean;
  timeout_seconds: number;
  allowed_roots: string[];
  denied_commands: string[];
};

export type AgentSettings = {
  guardian: { mode: string; timeout_seconds: number };
  compaction: { trigger_ratio: number; reserve_tokens: number };
  permissions: { default_mode: string };
  tools: { search: boolean };
  safety: SafetySettings;
  path: string;
  limits: {
    guardian_modes?: string[];
    guardian_timeout?: { min: number; max: number; default: number };
    permission_modes?: string[];
    permission_mode_labels?: Record<string, string>;
    trigger_ratio?: { min: number; max: number; default: number };
    reserve_tokens?: { min: number; max: number; default: number };
    safety_timeout?: { min: number; max: number; default: number };
    safety_max_entries?: number;
  };
};

export type AgentSettingsPatch = {
  guardian?: { mode?: string; timeout_seconds?: number };
  compaction?: { trigger_ratio?: number; reserve_tokens?: number };
  permissions?: { default_mode?: string };
  tools?: { search?: boolean };
  safety?: Partial<SafetySettings>;
};

/** 这台机器此刻到底怎么隔离的：容器 / 本机实测边界 / 无隔离。 */
export type SafetyRuntime = {
  backend: {
    backend: string;
    isolation: string;
    label: string;
    detail: string;
    platform: string;
    docker_available: boolean;
    tier?: string;
    strength?: string;
    available?: boolean;
    reason?: string;
    notice?: string;
  };
  policy: SafetySettings;
  default_mode: string;
  settings_path: string;
};

const api = API_V1;

export function fetchPresets(): Promise<PresetsResponse> {
  return request<PresetsResponse>(`${api}/providers/presets`, { cache: "no-store" });
}

export function fetchProviders(): Promise<Provider[]> {
  return request<Provider[]>(`${api}/providers`, { cache: "no-store" });
}

export function createProvider(input: ProviderCreate): Promise<Provider> {
  return request<Provider>(`${api}/providers`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateProvider(id: string, patch: ProviderPatch): Promise<Provider> {
  return request<Provider>(`${api}/providers/${id}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function deleteProvider(id: string): Promise<void> {
  return request<void>(`${api}/providers/${id}`, { method: "DELETE" });
}

export function testProvider(id: string): Promise<TestResult> {
  return request<TestResult>(`${api}/providers/${id}/test`, { method: "POST" });
}

export function fetchUsage(): Promise<UsageSummary> {
  return request<UsageSummary>(`${api}/usage/summary`, { cache: "no-store" });
}

export function fetchObservability(): Promise<ObservabilitySummary> {
  return request<ObservabilitySummary>(`${api}/observability/summary`, { cache: "no-store" });
}

export function fetchAgentSettings(): Promise<AgentSettings> {
  return request<AgentSettings>(`${api}/settings/agent`, { cache: "no-store" });
}

/** 特性注册表里的一条。source 说明当前值是哪一层定的：env / settings / legacy / default。 */
export type FeatureFlag = {
  name: string;
  stage: string;
  default: boolean;
  summary: string;
  legacy: string[];
  enabled: boolean;
  source: string;
};

export type FeaturesResponse = {
  features: FeatureFlag[];
  unknown: string[];
  stages: string[];
  sources: string[];
  env_prefix: string;
};

export function fetchFeatures(): Promise<FeaturesResponse> {
  return request<FeaturesResponse>(`${api}/settings/features`, { cache: "no-store" });
}

export function patchFeatures(patch: Record<string, boolean>): Promise<FeaturesResponse> {
  return request<FeaturesResponse>(`${api}/settings/features`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function updateAgentSettings(patch: AgentSettingsPatch): Promise<AgentSettings> {
  return request<AgentSettings>(`${api}/settings/agent`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function resetAgentSettings(): Promise<AgentSettings> {
  return request<AgentSettings>(`${api}/settings/agent/reset`, { method: "POST" });
}

export function fetchSafetyRuntime(): Promise<SafetyRuntime> {
  return request<SafetyRuntime>(`${api}/settings/safety/runtime`, { cache: "no-store" });
}

/** 一个外部工具服务端此刻的状态。state：ready / failed / skipped。 */
export type McpServerStatus = {
  name: string;
  transport: string;
  state: string;
  tools: number;
  /** 失败或跳过时后端给的中文原因，成功时为空 */
  error?: string;
  server?: string;
};

export type McpResponse = {
  /** 配置文件的绝对路径：用户要改的就是这个文件 */
  config: string;
  /** 所有服务端提供的工具总数 */
  tools: number;
  servers: McpServerStatus[];
};

export function fetchMcp(): Promise<McpResponse> {
  return request<McpResponse>(`${api}/settings/mcp`, { cache: "no-store" });
}

export function chatProvider(providerId: string, message: string): Promise<{ text: string; model?: string }> {
  return request<{ text: string; model?: string }>(`${api}/providers/${providerId}/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, stream: false }),
  });
}

export { ApiError, errorMessage };

/**
 * POST /providers/{id}/chat/stream and append `event: delta` payloads as they arrive.
 * Failures surface through onNotice with the backend's Chinese message.
 */
export async function streamChat(
  providerId: string,
  message: string,
  handlers: {
    onDelta: (text: string) => void;
    onNotice?: (text: string) => void;
    onDone?: () => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${api}/providers/${providerId}/chat/stream`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message, stream: true }),
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
  if (!response.body) throw new ApiError("后端未返回流式内容", "empty_stream", 204);

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { events, rest } = drainSse(buffer);
    buffer = rest;
    for (const event of events) {
      if (event.event === "delta") handlers.onDelta(deltaText(event.data));
      else if (event.event === "error") handlers.onNotice?.(deltaText(event.data));
      else if (event.event === "done") handlers.onDone?.();
    }
  }
  const trailing = drainSse(`${buffer}\n\n`);
  for (const event of trailing.events) {
    if (event.event === "delta") handlers.onDelta(deltaText(event.data));
    else if (event.event === "error") handlers.onNotice?.(deltaText(event.data));
    else if (event.event === "done") handlers.onDone?.();
  }
}

/** 按需选模型的转正依据（/observability/routing）。 */
export type RoutingEvidence = {
  scanned: number;
  sampled_tasks: number;
  calls: number;
  switched: number;
  escalations: number;
  cost_units: number;
  baseline_units: number;
  saved_ratio: number;
  switch_ratio: number;
  verdict: string;
  recent: {
    task_id: string;
    status: string;
    at: string;
    calls: number;
    switched: number;
    saved_ratio: number;
  }[];
};

export function fetchRoutingEvidence(): Promise<RoutingEvidence> {
  return request<RoutingEvidence>(`${api}/observability/routing`, { cache: "no-store" });
}
