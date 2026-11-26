import type { SessionGrant } from "./grants-api";

/** 到期时间只显示本地时钟；解析不出来就不显示，不编一个时间。 */
export function grantExpiry(value: string): string {
  if (!value) return "";
  const at = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(at.getTime())) return "";
  return `到期 ${at.toLocaleTimeString("zh-CN", { hour12: false })}`;
}

/** 会话条上的入口文案；没有授权时不占位置。 */
export function grantSummary(grants: SessionGrant[]): string {
  return grants.length > 0 ? `授权 ${grants.length}` : "";
}
