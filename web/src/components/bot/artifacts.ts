/** 产物清单的分类、过滤与导出。纯函数，`node --test` 能直接跑。 */

export type ArtifactKind = "page" | "code" | "data" | "image" | "doc" | "other";

export const KIND_LABELS: Record<ArtifactKind, string> = {
  page: "页面",
  code: "代码",
  data: "数据",
  image: "图片",
  doc: "文档",
  other: "其他",
};

const KIND_ORDER: ArtifactKind[] = ["page", "code", "data", "image", "doc", "other"];

const EXTENSIONS: Record<Exclude<ArtifactKind, "other">, string[]> = {
  page: ["html", "htm"],
  code: [
    "js",
    "mjs",
    "cjs",
    "ts",
    "tsx",
    "jsx",
    "py",
    "go",
    "rs",
    "java",
    "kt",
    "kts",
    "cs",
    "cpp",
    "cc",
    "c",
    "h",
    "hpp",
    "rb",
    "php",
    "lua",
    "sh",
    "ps1",
    "css",
    "scss",
    "less",
    "vue",
    "svelte",
  ],
  data: ["json", "jsonl", "csv", "tsv", "yaml", "yml", "xml", "sql", "db", "sqlite", "parquet"],
  image: ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif"],
  doc: ["md", "markdown", "txt", "pdf", "doc", "docx", "rst", "adoc"],
};

/** 按扩展名分类；认不出的一律进「其他」，不猜内容。 */
export function artifactKind(path: string): ArtifactKind {
  const name = String(path ?? "").split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  if (dot < 0 || dot === name.length - 1) return "other";
  const ext = name.slice(dot + 1).toLowerCase();
  for (const kind of KIND_ORDER) {
    if (kind === "other") continue;
    if (EXTENSIONS[kind].includes(ext)) return kind;
  }
  return "other";
}

export type ArtifactGroup<T> = {
  kind: ArtifactKind;
  label: string;
  items: T[];
};

/**
 * 先按搜索词过滤路径，再按类型分组。
 *
 * 空组直接不出现：产物面板上挂一个「图片 0」只会让人以为加载失败。
 * 组顺序固定（页面 → 代码 → 数据 → 图片 → 文档 → 其他），不按数量排序——
 * 每次任务类型不同，顺序乱跳会让同一位置的文件换位置。
 */
export function groupArtifacts<T extends { path: string }>(
  items: T[],
  query = "",
): ArtifactGroup<T>[] {
  const needle = query.trim().toLowerCase();
  const filtered = needle
    ? items.filter((item) => String(item.path ?? "").toLowerCase().includes(needle))
    : items;
  const buckets = new Map<ArtifactKind, T[]>();
  for (const item of filtered) {
    const kind = artifactKind(item.path);
    const bucket = buckets.get(kind);
    if (bucket) bucket.push(item);
    else buckets.set(kind, [item]);
  }
  return KIND_ORDER.filter((kind) => (buckets.get(kind)?.length ?? 0) > 0).map((kind) => ({
    kind,
    label: KIND_LABELS[kind],
    items: buckets.get(kind) ?? [],
  }));
}

export type ExportArtifact = {
  path: string;
  changeType?: string;
};

/**
 * 导出清单。
 *
 * JSON 给机器读，Markdown 给人贴到任务说明或交接单里；两者都只导出路径与
 * 变更类型，不导出 diff——diff 可能很大，而且真正的证据在文件与证据包里。
 */
export function artifactsExport(
  items: ExportArtifact[],
  format: "json" | "markdown",
): string {
  if (format === "json") {
    return JSON.stringify(
      items.map((item) => ({ path: item.path, changeType: item.changeType || "modified" })),
      null,
      2,
    );
  }
  const lines = ["# 产物清单", ""];
  for (const group of groupArtifacts(items)) {
    lines.push(`## ${group.label}（${group.items.length}）`, "");
    for (const item of group.items) {
      lines.push(`- \`${item.path}\` · ${item.changeType || "modified"}`);
    }
    lines.push("");
  }
  return lines.join("\n").trimEnd() + "\n";
}
