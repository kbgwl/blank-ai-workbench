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