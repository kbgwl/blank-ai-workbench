import { API_V1, jsonInit, request } from "@/lib/http";

export type PreviewState = {
  status: "running" | "stopped" | string;
  url?: string;
  port?: number;
  started_at?: string;
  expires_at?: string;
  isolation?: string;
  notes?: string[];
  log_tail?: string;
  workspace?: string;
  exit_code?: number | null;
};

export function fetchPreview(projectId: string): Promise<PreviewState> {
  return request<PreviewState>(`${API_V1}/projects/${projectId}/preview`, { cache: "no-store" });
}

export function startPreview(projectId: string): Promise<PreviewState> {
  return request<PreviewState>(`${API_V1}/projects/${projectId}/preview`, {
    method: "POST",
    ...jsonInit,
  });
}

export function stopPreview(projectId: string): Promise<PreviewState> {
  return request<PreviewState>(`${API_V1}/projects/${projectId}/preview`, { method: "DELETE" });
}
