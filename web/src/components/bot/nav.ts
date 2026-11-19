/**
 * 左栏导航与状态点：把"有哪些入口"和"这条会话现在是什么状态"变成可测的纯函数。
 *
 * 常驻导航只留「工作区」一项。监控、证据库、技能、连接器都是低频的查看/配置项，
 * 统一收进设置页（设置 → 查看与效率 / 外部工具与插件），不再在侧栏和图标栏摆一排。
 * Qoder、Claude 的常驻栏同样只有「对话 + 搜索 + 工作区 + 设置」这条骨架。
 */

export type NavId = "workspace";

export type NavItem = {
  id: NavId;
  href: string;
  label: string;
  hint: string;
};

export const NAV_ITEMS: NavItem[] = [
  { id: "workspace", href: "/projects", label: "工作区", hint: "项目与目录" },
];

/** 低频入口从侧栏撤下后仍要可达：统一从设置页进入。命令面板也保留这些跳转。 */
export type UtilityLink = { href: string; label: string; hint: string };

export const UTILITY_LINKS: UtilityLink[] = [
  { href: "/tasks", label: "全部任务", hint: "按状态找历史任务，点一行回到那条会话" },
  { href: "/monitor", label: "监控", hint: "它现在在盯什么" },
  { href: "/evidence", label: "证据库", hint: "每次交付的验收、改动与用量" },
  { href: "/skills", label: "技能", hint: "你确认过的做法" },
  { href: "/settings#tools", label: "连接器", hint: "外部工具与数据源" },
];

/**
 * 比对只认路径。
 *
 * `#tools` 是设置页的分区锚点、`?x=1` 是查询参数——它们决定"进去之后停在哪一段"，
 * 不决定"当前在哪个页面"。把它们算进高亮判定，会让同一页在带锚点与不带锚点时
 * 高亮到不同条目。
 */
export function navPath(href: string): string {
  return href.split("#")[0].split("?")[0] || "/";
}

/** 静态导出后首屏是 `/index.html` 或 `/index`，开发态是 `/`——一律归一成 `/`。 */
export function cleanPath(pathname: string | null | undefined): string {
  return (pathname ?? "").replace(/\.html$/, "").replace(/\/index$/, "") || "/";
}

export function isNavActive(pathname: string | null | undefined, href: string): boolean {
  const current = cleanPath(pathname);
  const target = navPath(href);
  if (target === "/") return current === "/";
  return current === target || current.startsWith(`${target}/`);
}

export type TurnTone = "running" | "failed" | "done" | "idle";

const RUNNING = new Set(["running", "queued", "pending", "waiting", "planning", "in_progress", "started"]);
const FAILED = new Set(["failed", "error", "cancelled", "canceled", "interrupted", "timeout", "timed_out"]);
const DONE = new Set(["succeeded", "success", "done", "completed", "finished"]);

/**
 * 一条会话画什么颜色的点。
 *
 * 空串单独算 `idle`（没说过话），不能落进 `done`：把"还没开始"画成"已完成"，
 * 用户会以为点进去有东西看。认不出的状态也归 `idle`——宁可少给一个信息，
 * 也不要把未知说成成功。
 */
export function turnTone(status: string | null | undefined): TurnTone {
  const value = (status ?? "").trim().toLowerCase();
  if (!value || value === "draft") return "idle";
  if (RUNNING.has(value)) return "running";
  if (FAILED.has(value)) return "failed";
  if (DONE.has(value)) return "done";
  return "idle";
}

export const TURN_TONE_LABEL: Record<TurnTone, string> = {
  running: "进行中",
  failed: "失败或已取消",
  done: "已完成",
  idle: "还没开始",
};
