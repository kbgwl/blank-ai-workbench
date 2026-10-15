import { ApiError, deltaText, drainSse, errorMessage, request, toApiError, parseJson } from "@/lib/http";

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

const api = "/api/v1";

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
