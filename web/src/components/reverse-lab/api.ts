/**
 * 逆向实验室（`/reverse-lab`）的 HTTP 面。
 *
 * 后端四个端点直连 REA MCP Adapter（stdio 子进程），链路是：
 *
 * ```
 * GET  /reverse-lab/status        适配器自述（连没连上、工具表全不全、边界事实）
 * GET  /reverse-lab/targets       经 scan_targets 列出真实候选实物
 * GET  /reverse-lab/strings       经 find_strings 抽字符串线索
 * POST /reverse-lab/investigations 经 inspect_target 做一次有台账的调查
 * ```
 *
 * 这一层只做两件事：把响应原样搬成类型，以及把"状态/大小/偏移"翻成能读的字。
 * 不做兜底猜测——适配器不可用是 503，后端已经把"服务端自己说的那句话"带回来了，
 * 前端照抄比另编一句更接近事实。
 */

import { API_V1, jsonInit, request } from "@/lib/http";

const api = API_V1;

export type AdapterState = "ready" | "partial" | "failed" | "disabled" | (string & {});

export type AdapterStatus = {
  state: AdapterState;
  server: string;
  transport: string;
  root: string;
  expected: string[];
  declared: string[];
  tools: string[];
  tools_count: number;
  missing: string[];
  boundary: string;
  connect_ms: number | null;
  boundary_note: string;
  error?: string;
  hint?: string;
};

export type ReaLabStatus = {
  project_id: string;
  root: string;
  adapter: AdapterStatus;
  root_exists?: boolean;
};

export type ReaTarget = {
  target: string;
  size: number;
  format: string | null;
  runnable_guess: boolean;
};

export type TargetsResponse = {
  project_id: string;
  root: string;
  path: string;
  adapter: AdapterStatus;
  targets: ReaTarget[];
  truncated: number;
  note: string;
};

export type StringHit = { offset: number; text: string };

export type StringsResponse = {
  project_id: string;
  target: string;
  adapter: AdapterStatus;
  strings: StringHit[];
  scanned_bytes: number | null;
  empty_reason: string;
  note: string;
};

export type Finding = {
  kind: string;
  value: string;
  evidence_id: string;
};

export type InvestigationResponse = {
  task: { id: string; title: string; status: string; result_summary?: string; failure_reason?: string };
  state: string;
  ok: boolean;
  reason: string;
  failure: Record<string, unknown>;
  findings: Finding[];
  result: Record<string, unknown>;
  adapter: AdapterStatus;
  evidence: {
    tool?: string | null;
    tool_call_id?: string | null;
    boundary_kind?: string | null;
    risk_level?: string | null;
    duration_ms?: number | null;
    events?: string;
    bundle?: string;
  };
};

// ---------------------------------------------------------------------------
// 端点
// ---------------------------------------------------------------------------

function query(pairs: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(pairs)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function fetchReaLabStatus(projectId: string, refresh = false): Promise<ReaLabStatus> {
  return request<ReaLabStatus>(
    // 只有要重连时才发 refresh：恒带一个 refresh=false 会让"这是常规读取"这件事看不出来
    `${api}/reverse-lab/status${query({ project_id: projectId, refresh: refresh || undefined })}`,
  );
}

export function fetchReaTargets(projectId: string, path = "", limit = 60): Promise<TargetsResponse> {
  return request<TargetsResponse>(
    `${api}/reverse-lab/targets${query({ project_id: projectId, path, limit })}`,
  );
}

export function fetchReaStrings(
  projectId: string,
  target: string,
  options: { minLength?: number; limit?: number } = {},
): Promise<StringsResponse> {
  return request<StringsResponse>(
    `${api}/reverse-lab/strings${query({
      project_id: projectId,
      target,
      min_length: options.minLength,
      limit: options.limit,
    })}`,
  );
}

export function createReaInvestigation(input: {
  projectId: string;
  target: string;
  title?: string;
}): Promise<InvestigationResponse> {
  return request<InvestigationResponse>(`${api}/reverse-lab/investigations`, {
    ...jsonInit,
    method: "POST",
    body: JSON.stringify({
      project_id: input.projectId,
      target: input.target,
      title: input.title ?? "",
    }),
  });
}

// ---------------------------------------------------------------------------
// 读数翻译：状态、字节、偏移、字段缺失
// ---------------------------------------------------------------------------

/** 三档色调。适配器的四种状态落到界面只有"能用 / 半残 / 不能用"三种处置。 */
export function adapterTone(state: AdapterState): "ok" | "warn" | "bad" {
  if (state === "ready") return "ok";
  if (state === "partial") return "warn";
  return "bad";
}

export function adapterLabel(state: AdapterState): string {
  switch (state) {
    case "ready":
      return "适配器在线";
    case "partial":
      return "工具表不全";
    case "failed":
      return "没连上";
    case "disabled":
      return "已关闭";
    default:
      return state || "未知状态";
  }
}

/** 状态不好时，界面该照抄哪一句：服务端的原话优先于我们的转述。 */
export function adapterReason(status: AdapterStatus): string {
  if (status.error) return status.error;
  if (status.hint) return status.hint;
  if (status.missing.length) return `缺少工具：${status.missing.join("、")}`;
  return status.boundary_note;
}

export function formatBytes(size: number | null | undefined): string {
  if (typeof size !== "number" || Number.isNaN(size) || size < 0) return "—";
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB"];
  let value = size / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

/** 十六进制偏移，补到 8 位：字符串线索表按它排序，短一跳就会错行。 */
export function offsetLabel(offset: number): string {
  const value = Number.isFinite(offset) ? Math.max(0, Math.trunc(offset)) : 0;
  return `0x${value.toString(16).padStart(8, "0")}`;
}

export function targetFormat(target: ReaTarget): string {
  return target.format || "认不出格式";
}

/**
 * 候选目标那一行的副标题。
 *
 * "能不能跑"是幻数猜的（`runnable_guess`），所以措辞写成"像是可执行"，
 * 不写成"可执行"——这条链路的定位就是"不解析正文"。
 */
export function targetHint(target: ReaTarget): string {
  const parts = [targetFormat(target), formatBytes(target.size)];
  if (target.runnable_guess) parts.push("像是可执行");
  return parts.join(" · ");
}

/** 结论行的出处：没有 Evidence ID 的结论不该出现在列表里。 */
export function findingLabel(finding: Finding): string {
  const kind = finding.kind?.trim() || "结论";
  return `${kind}｜${finding.value}`;
}
