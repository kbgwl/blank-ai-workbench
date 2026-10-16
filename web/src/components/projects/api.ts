import { API_V1, ApiError, errorCode, errorMessage, jsonInit, request } from "@/lib/http";

export type Workspace = {
  id: string;
  label: string;
  path: string;
  is_active: boolean;
};

export type Project = {
  id: string;
  name: string;
  description: string;
  workspaces: Workspace[];
};

export type TreeEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
};

export type TreeResponse = {
  root: string;
  path: string;
  entries: TreeEntry[];
};

export type FileContent = {
  path: string;
  size: number;
  encoding: string;
  binary: boolean;
  truncated: boolean;
  content: string;
  message: string;
};

export { ApiError, errorMessage, errorCode };

/** Backend errors always carry {code, message}; keep both for targeted handling. */
export function errorDetails(error: unknown): { code: string; message: string } {
  return { code: errorCode(error), message: errorMessage(error) };
}

const api = API_V1;

export function fetchProjects(): Promise<Project[]> {
  return request<Project[]>(`${api}/projects`, { cache: "no-store" });
}

export function createProject(input: { name: string; description: string }): Promise<Project> {
  return request<Project>(`${api}/projects`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function updateProject(id: string, input: { name?: string; description?: string }): Promise<Project> {
  return request<Project>(`${api}/projects/${id}`, {
    method: "PATCH",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function deleteProject(id: string): Promise<void> {
  return request<void>(`${api}/projects/${id}`, { method: "DELETE" });
}

export function bindWorkspace(id: string, input: { path: string; label?: string }): Promise<Workspace> {
  return request<Workspace>(`${api}/projects/${id}/workspaces`, {
    method: "POST",
    ...jsonInit,
    body: JSON.stringify(input),
  });
}

export function unbindWorkspace(projectId: string, workspaceId: string): Promise<void> {
  return request<void>(`${api}/projects/${projectId}/workspaces/${workspaceId}`, { method: "DELETE" });
}

export function fetchTree(projectId: string, path = "", depth = 1): Promise<TreeResponse> {
  const query = `path=${encodeURIComponent(path)}&depth=${depth}`;
  return request<TreeResponse>(`${api}/projects/${projectId}/tree?${query}`, { cache: "no-store" });
}

export function fetchFile(projectId: string, path: string): Promise<FileContent> {
  return request<FileContent>(`${api}/projects/${projectId}/file?path=${encodeURIComponent(path)}`, {
    cache: "no-store",
  });
}

// 后端在没有 is_active 工作区时回落到第一个工作区，这里保持一致。
export function activeWorkspace(project: Project | null): Workspace | null {
  if (!project || project.workspaces.length === 0) return null;
  return project.workspaces.find((workspace) => workspace.is_active) ?? project.workspaces[0];
}
