/**
 * 桌面壳暴露给页面的能力。
 *
 * 页面在两种环境里跑：Electron 壳内（`window.blankDesktop` 存在）和普通浏览器里
 * （不存在）。所有入口都先问这里，而不是渲染出一个按钮再让用户点了没反应——
 * "点了没反应"是上一轮被投诉最多的一类问题。
 */

type BlankDesktop = {
  platform: string;
  versions: { electron: string; chrome: string };
  pickDirectory: (current?: string) => Promise<string>;
};

declare global {
  interface Window {
    blankDesktop?: BlankDesktop;
  }
}

/** 能不能弹系统原生的目录选择框。浏览器里为 false，调用方据此隐藏按钮。 */
export function canPickDirectory(): boolean {
  return typeof window !== "undefined" && typeof window.blankDesktop?.pickDirectory === "function";
}

/**
 * 弹目录选择框，返回绝对路径；取消或不可用时返回空串。
 *
 * 不抛错：用户点"取消"和"这个环境没有该能力"对调用方是同一件事——什么都不用做。
 */
export async function pickDirectory(current = ""): Promise<string> {
  const api = typeof window === "undefined" ? undefined : window.blankDesktop;
  if (!api?.pickDirectory) return "";
  try {
    return await api.pickDirectory(current);
  } catch {
    return "";
  }
}
