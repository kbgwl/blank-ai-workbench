/**
 * 任务页筛选与地址栏的映射。
 *
 * 刷新/分享/从任务详情返回时，筛选条件应该还在——以前它只活在组件 state 里，
 * 刷新就回到「全部」，用户得重新搜一遍。这里只做纯字符串转换，组件负责
 * replaceState；不引入路由库，也不生成历史记录。
 */
import { TASK_STATUS_FILTERS, type TaskStatusFilter } from "../tasks/status";

export type TaskFilterState = { query: string; status: TaskStatusFilter };

export function parseTaskFilters(search: string): TaskFilterState {
  const params = new URLSearchParams(String(search || "").replace(/^\?/, ""));
  const raw = params.get("status") ?? "";
  const status = TASK_STATUS_FILTERS.some((item) => item.key === raw)
    ? (raw as TaskStatusFilter)
    : "all";
  return { query: (params.get("q") ?? "").trim(), status };
}

/** 把筛选写回 href（保留其他参数，如 project）。返回 path+search+hash，不带 origin。 */
export function writeTaskFilters(href: string, state: TaskFilterState): string {
  const url = new URL(String(href || "/tasks"), "http://blank.local");
  const query = state.query.trim();
  if (query) url.searchParams.set("q", query);
  else url.searchParams.delete("q");
  if (state.status !== "all") url.searchParams.set("status", state.status);
  else url.searchParams.delete("status");
  return `${url.pathname}${url.search}${url.hash}`;
}
