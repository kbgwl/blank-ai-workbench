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

type ErrorBody = {
  error?: { code?: unknown; message?: unknown } | string;
  message?: unknown;
};

export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function toApiError(status: number, body: unknown, context = ""): ApiError {
  const payload = body as ErrorBody | null;
  const detail = payload && typeof payload === "object" ? payload.error : undefined;
  const nested = detail && typeof detail === "object" ? detail : undefined;
  const legacyCode = typeof detail === "string" ? detail : "";
  const legacyMessage = typeof payload?.message === "string" ? payload.message : "";
  const suffix = context ? ` · ${context}` : "";
  // 没有结构化错误体 = 这个 5xx 不是后端给的：开发态是 Next 的 rewrite 代理在
  // 后端不可达时吐的 HTML 错误页（典型场景：后端正在重启）。说清楚"稍后重试"，
  // 并把请求带上，用户截图就能定位到是哪一个调用。后端的 500 一定带 JSON 错误体，
  // 走上面的 message 分支，不会被这句误导。
  const proxy = status >= 500 && body === null;
  const message =
    nested && typeof nested.message === "string" && nested.message
      ? nested.message
      : legacyMessage ||
        (proxy
          ? `后端暂时不可用（HTTP ${status}），可能正在重启，请稍后重试${suffix}`
          : `请求失败（HTTP ${status}）${suffix}`);
  const code =
    nested && typeof nested.code === "string" && nested.code
      ? nested.code
      : legacyCode || `http_${status}`;
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
  // 烘焙的绝对地址只对桌面壳有意义：桌面壳的页面协议是 app://bundle，跨源请求
  // 由壳放行；而纯浏览器场景（CLI 静态站、开发态）从 3000 打 8000 会被 CORS 拦死，
  // 必须走相对路径，由静态服务器的同源代理（CLI serveStatic / next rewrite）转发。
  const hasDesktopBridge = typeof (globalThis as { blankDesktop?: unknown }).blankDesktop !== "undefined";
  return hasDesktopBridge ? baked ?? "" : "";
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
  if (!response.ok) {
    throw toApiError(response.status, body, `${init?.method || "GET"} ${path}`);
  }
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
