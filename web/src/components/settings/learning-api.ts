import { API_V1, jsonInit, request } from "@/lib/http";

export type LearningLint = {
  kind: string;
  message: string;
  evidence?: string[];
  path?: string;
  count?: number;
};

export type LearningExperience = {
  statement: string;
  kind: string;
  status: string;
  trigger_paths: string[];
  task_terms: string[];
  error_signature: string;
  evidence: unknown[];
  confidence: number;
  hits: number;
  misses: number;
  fingerprint: string;
  expired: boolean;
};

export type LearningPreference = {
  rule: string;
  dimension: string;
  label: string;
  scope: string;
  scope_ref: string;
  quotes: { text?: string; source?: string; task_id?: string }[];
  occurrences: number;
  origin: string;
  confidence: number;
  status: string;
  fingerprint: string;
  expired: boolean;
};

export type LearningSnapshot = {
  workspace: string;
  experience: {
    path: string;
    entries: LearningExperience[];
    stats: Record<string, number>;
    lint: LearningLint[];
  };
  persona: {
    path: string;
    global_path: string;
    entries: LearningPreference[];
    stats: Record<string, number>;
    origins: Record<string, number>;
    lint: LearningLint[];
  };
};

export function fetchLearning(projectId: string): Promise<LearningSnapshot> {
  return request<LearningSnapshot>(`${API_V1}/projects/${projectId}/learning`, { cache: "no-store" });
}

export function retireExperience(projectId: string, fingerprint: string): Promise<{ retired: string[] }> {
  return request<{ retired: string[] }>(
    `${API_V1}/projects/${projectId}/learning/experience/${fingerprint}/retire`,
    { method: "POST", ...jsonInit },
  );
}

export function retirePreference(projectId: string, fingerprint: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `${API_V1}/projects/${projectId}/learning/persona/${fingerprint}/retire`,
    { method: "POST", ...jsonInit },
  );
}

export function promotePreference(projectId: string, fingerprint: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `${API_V1}/projects/${projectId}/learning/persona/${fingerprint}/promote`,
    { method: "POST", ...jsonInit },
  );
}
