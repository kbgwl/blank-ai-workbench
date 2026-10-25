export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return "未知错误";
}

export function errorCode(error: unknown): string {
  if (error instanceof ApiError) return error.code;
  return "";
}

type ErrorBody = { error?: { code?: unknown; message?: unknown } };

export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function toApiError(status: number, body: unknown): ApiError {
  const payload = body as ErrorBody | null;
  const detail = payload && typeof payload === "object" ? payload.error : undefined;
  const message =
    detail && typeof detail.message === "string" && detail.message ? detail.message : `请求失败（HTTP ${status}）`;
  const code = detail && typeof detail.code === "string" ? detail.code : `http_${status}`;
  return new ApiError(message, code, status);
}

export const jsonInit = { headers: { "content-type": "application/json" } };

/**
 * API 基址。
 *
 * - 网页 / 开发态：留空 → 走相对路径 `/api/...`，由 Next 的 rewrite 代理到本机后端。
 * - 桌面壳构建：`NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000` → 直连后端。
 *   静态导出没有服务器，rewrite 不生效，所以桌面构建必须给绝对地址。
 * - 桌面壳运行态：preload 注入 `window.blankDesktop.apiBase`，优先于烘焙值——
 *   `BLANK_BACKEND_PORT` / `BLANK_API_BASE` 改了端口时，界面不用重新构建。
 */
type DesktopBridge = { apiBase?: unknown };

export function resolveApiBase(injected: unknown, baked: string | undefined): string {
  const fromShell = typeof injected === "string" ? injected.trim() : "";
  if (fromShell) return fromShell.replace(/\/+$/, "");
  return baked ?? "";
}

export const API_BASE = resolveApiBase(
  (globalThis as { blankDesktop?: DesktopBridge }).blankDesktop?.apiBase,
  process.env.NEXT_PUBLIC_API_BASE,
);

export const API_V1 = `${API_BASE}/api/v1`;

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError("无法连接后端服务", "network_error", 0);
  }
  const text = await response.text();
  const body = text ? parseJson(text) : null;
  if (!response.ok) throw toApiError(response.status, body);
  if (response.status === 204 || body === null) return undefined as T;
  return body as T;
}

export async function postJson<T>(path: string, payload: unknown): Promise<T> {
  return request<T>(path, { method: "POST", ...jsonInit, body: JSON.stringify(payload) });
}

// ---- SSE -------------------------------------------------------------------

export type SseEvent = { event: string; data: string };

/** Split a buffer on blank-line SSE frames; returns the leftover tail. */
export function drainSse(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  const frames = buffer.split("\n\n");
  const rest = frames.pop() ?? "";
  for (const frame of frames) {
    if (!frame.trim()) continue;
    let event = "message";
    const data: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    events.push({ event, data: data.join("\n") });
  }
  return { events, rest };
}

/** Text deltas may arrive as JSON payloads ({"text": "…"}) or as raw Chinese messages. */
export function deltaText(data: string): string {
  const parsed = parseJson(data);
  if (parsed && typeof parsed === "object" && typeof (parsed as { text?: unknown }).text === "string") {
    return (parsed as { text: string }).text;
  }
  return typeof parsed === "string" ? parsed : data;
}
