import { API_V1, jsonInit, request } from "@/lib/http";

export type MemoryCandidate = {
  id: string;
  text: string;
  source: string;
  status: "pending" | "accepted" | "rejected" | string;
  created_at: string;
  reviewed_at: string | null;
};

export type Skill = {
  id: string;
  name: string;
  instructions: string;
  status: "draft" | "active" | "disabled" | "proposed" | string;
  version: number;
  source_candidate_id: string | null;
  history: unknown[];
  // 提案元数据（kind/target/description/evidence/proposed_at）。模型发起的提案
  // 落成 status=proposed 的行，采用后才变成 draft 或并入目标 Skill。
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

const api = API_V1;

/**
 * 磁盘上真正可用的技能（内置 / 我的 / 项目 / 插件）。
 *
 * 与 `Skill` 的区别：那个是库里"用户审过的记忆技能"，这个是运行时真正会被
 * 加载的 SKILL.md 目录。技能页要回答"我现在能用什么"，得看这一份。
 */
export type SkillLibraryItem = {
  name: string;
  display_name: string;
  description: string;
  when_to_use: string;
  allowed_tools: string[];
  paths: string[];
  model_invocable: boolean;
  source: "builtin" | "user" | "project" | "plugin" | string;
  path: string;
  shadowed: boolean;
  /** 安装来源与时间（技能目录里 .skill-source.json 的内容）。内置技能为空。 */
  installed_from: string;
  installed_at: string;
};

export type SkillLibrary = {
  items: SkillLibraryItem[];
  counts: Record<string, number>;
  /** 技能名 → 最近 7 天用量；没被调用过的技能不在这里。 */
  usage: Record<string, { calls: number; tasks: number; tokens: number; last_used: string }>;
  usage_days: number;
  root: string;
  errors: string[];
  warnings: string[];
};

export function fetchSkillLibrary(): Promise<SkillLibrary> {
  return request<SkillLibrary>(`${api}/skills/library`, { cache: "no-store" });
}

export type SkillInstallPayload = {
  source: string;
  scope: "user" | "project";
  project_id?: string | null;
  name?: string;
  overwrite?: boolean;
};

/** 装一份技能：来源可以是 http(s) 地址（.md / .zip），也可以是本机目录。 */
export function installSkill(payload: SkillInstallPayload): Promise<{ name: string; path: string }> {
  return request(`${api}/skills/install`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(payload),
  });
}

export function uninstallSkill(payload: {
  name: string;
  scope: "user" | "project";
  project_id?: string | null;
  /** 技能目录的绝对路径。多项目时按名字猜会删错项目。 */
  path?: string;
}): Promise<unknown> {
  return request(`${api}/skills/uninstall`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(payload),
  });
}

export type MarketSource = { id: string; label: string; kind: string; repo: string };

export type MarketItem = {
  id: string;
  name: string;
  description: string;
  source_id: string;
  source_label: string;
};

export function fetchMarketSources(): Promise<MarketSource[]> {
  return request<MarketSource[]>(`${api}/skills/market/sources`, { cache: "no-store" });
}

/** 列市场里的技能。只读：后端不会下载也不会落盘。 */
export function fetchMarket(source: string, query = ""): Promise<{ source: string; items: MarketItem[] }> {
  const params = new URLSearchParams({ source });
  if (query.trim()) params.set("query", query.trim());
  return request(`${api}/skills/market?${params.toString()}`, { cache: "no-store" });
}

export function installFromMarket(payload: {
  source: string;
  item_id: string;
  scope: "user" | "project";
  overwrite?: boolean;
}): Promise<{ name: string; path: string }> {
  return request(`${api}/skills/market/install`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(payload),
  });
}

/** 提交一条待审核的记忆候选；确认权始终在用户手里，后端只落 pending。 */
export function submitMemoryCandidate(text: string, source = "daily"): Promise<MemoryCandidate> {
  return request<MemoryCandidate>(`${api}/skills/memory-candidates`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify({ text, source }),
  });
}

export function fetchMemoryCandidates(): Promise<MemoryCandidate[]> {
  return request<MemoryCandidate[]>(`${api}/skills/memory-candidates`, { cache: "no-store" });
}

/** 用户确认后才会生成 Skill 草案；草案默认 draft，不自动启用。 */
export function acceptMemoryCandidate(id: string): Promise<Skill> {
  return request<Skill>(`${api}/skills/memory-candidates/${id}/accept`, { method: "POST" });
}

export function rejectMemoryCandidate(id: string): Promise<MemoryCandidate> {
  return request<MemoryCandidate>(`${api}/skills/memory-candidates/${id}/reject`, { method: "POST" });
}

export function fetchSkills(): Promise<Skill[]> {
  return request<Skill[]>(`${api}/skills`, { cache: "no-store" });
}

/** 采用一条模型提案：improvement 并入目标 Skill，new 落成 draft。 */
export function acceptSkillProposal(id: string): Promise<Skill> {
  return request<Skill>(`${api}/skills/${id}/accept`, { method: "POST" });
}

export function enableSkill(id: string): Promise<Skill> {
  return request<Skill>(`${api}/skills/${id}/enable`, { method: "POST" });
}

export function disableSkill(id: string): Promise<Skill> {
  return request<Skill>(`${api}/skills/${id}/disable`, { method: "POST" });
}

/** 物理删除一条记忆。与停用不同：删除后这条做法不再存在于任何列表里。 */
export async function deleteSkill(id: string): Promise<void> {
  await request<unknown>(`${api}/skills/${id}`, { method: "DELETE" });
}
export type MarketHit = {
  kind: "skill" | "repo" | string;
  name: string;
  description: string;
  repo: string;
  path: string;
  stars: number;
  url: string;
  updated_at: string;
};

/** 按一句话去 GitHub 找技能/工具。只读，不下载。 */
export function searchMarket(
  query: string,
  limit = 8,
): Promise<{ query: string; items: MarketHit[]; origin: string; synced_at: string; notice: string }> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return request(`${api}/skills/market/search?${params.toString()}`, { cache: "no-store" });
}

/** 装搜索结果里的一条（repo + 目录前缀）。 */
export function installFromRepo(payload: {
  repo: string;
  path: string;
  ref?: string;
  scope: "user" | "project";
  overwrite?: boolean;
  stars?: number;
}): Promise<{ name: string; path: string }> {
  return request(`${api}/skills/market/install_repo`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(payload),
  });
}
