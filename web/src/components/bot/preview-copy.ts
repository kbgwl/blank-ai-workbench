import type { PreviewState } from "./preview-api";

/** 预览状态的一句话。认不出的状态不当「运行中」，避免界面假装服务还活着。 */
export function previewHeadline(state: PreviewState | null): string {
  if (!state || state.status === "stopped") return "预览未启动";
  if (state.status === "running") return "预览运行中";
  return "预览不可用";
}

/** 到期时间只显示本地时钟；解析不出来就不显示，不编一个时间。 */
export function previewExpiry(state: PreviewState | null): string {
  if (!state?.expires_at) return "";
  const at = new Date(state.expires_at);
  if (Number.isNaN(at.getTime())) return "";
  return `到期 ${at.toLocaleTimeString("zh-CN", { hour12: false })}`;
}

/** 隔离档位如实翻译：Job 没挂上时不能写成「已隔离」。 */
export function previewIsolation(state: PreviewState | null): string {
  if (!state?.isolation) return "";
  if (state.isolation === "local-job") return "本机 + Job Object";
  if (state.isolation === "local") return "本机（Job 未挂上）";
  return state.isolation;
}
