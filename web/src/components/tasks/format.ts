export function formatClock(value: string): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function formatDateShort(value: string): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[index]}`;
}

export function formatDateTime(value: string): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
}

export function relativeTime(value: string, now?: number): string {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  const base = now ?? Date.now();
  const diff = Math.round((base - date.getTime()) / 1000);
  const abs = Math.abs(diff);
  if (abs < 45) return "刚刚";
  if (abs < 3600) return `${Math.round(abs / 60)} 分钟前`;
  if (abs < 86400) return `${Math.round(abs / 3600)} 小时前`;
  if (typeof Intl !== "undefined" && "RelativeTimeFormat" in Intl) {
    return new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" }).format(-Math.round(abs / 86400), "day");
  }
  return formatDateShort(value);
}

export function formatNumber(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "暂无";
  return value.toLocaleString("zh-CN");
}

export function orNone(value: string | null | undefined): string {
  const text = (value ?? "").trim();
  return text || "暂无";
}

export function clip(text: string, max = 160): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

export type DiffTone = "add" | "del" | "hunk" | "meta" | "ctx";
export type DiffLine = { text: string; tone: DiffTone };

export function parseDiff(diff: string): DiffLine[] {
  return diff.split("\n").map((line) => {
    if (line.startsWith("+++")) return { text: line, tone: "add" as const };
    if (line.startsWith("---")) return { text: line, tone: "del" as const };
    if (line.startsWith("+")) return { text: line, tone: "add" as const };
    if (line.startsWith("-")) return { text: line, tone: "del" as const };
    if (line.startsWith("@@") || line.startsWith("diff ") || line.startsWith("index "))
      return { text: line, tone: "hunk" as const };
    return { text: line, tone: "ctx" as const };
  });
}

export function diffStats(diff: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}
