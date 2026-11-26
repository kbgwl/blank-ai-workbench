import { API_V1, request } from "@/lib/http";

export type SessionGrant = {
  thread: string;
  category: string;
  label: string;
  granted_at: string;
  expires_at: string;
  source_task: string;
};

export function fetchGrants(threadId: string): Promise<SessionGrant[]> {
  return request<{ grants: SessionGrant[] }>(`${API_V1}/threads/${threadId}/grants`, {
    cache: "no-store",
  }).then((body) => body.grants ?? []);
}

export function revokeGrant(threadId: string, category: string): Promise<{ revoked: boolean }> {
  return request<{ revoked: boolean }>(
    `${API_V1}/threads/${threadId}/grants/${encodeURIComponent(category)}`,
    { method: "DELETE" },
  );
}

export function revokeAllGrants(threadId: string): Promise<{ revoked: number }> {
  return request<{ revoked: number }>(`${API_V1}/threads/${threadId}/grants`, {
    method: "DELETE",
  });
}
