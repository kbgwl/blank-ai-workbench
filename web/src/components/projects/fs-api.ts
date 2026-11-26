import { API_V1, request } from "@/lib/http";

export type FsEntry = { name: string; path: string };

export type DirectoryListing = {
  path: string;
  parent: string | null;
  roots: FsEntry[];
  entries: FsEntry[];
  truncated: boolean;
};

export function fetchDirectories(path = ""): Promise<DirectoryListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return request<DirectoryListing>(`${API_V1}/fs/directories${query}`, { cache: "no-store" });
}
