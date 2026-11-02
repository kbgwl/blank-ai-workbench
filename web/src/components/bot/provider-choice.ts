/**
 * 模型选择的持久化判定。
 *
 * 为什么单独摘出来：localStorage 里存的是 provider 的 id，而供应商随时可能被删
 * （换机器、重装、清理配置）。直接把旧 id 发下去，后端只会回一句「模型供应商不存在」，
 * 而界面上这个 id 匹配不到任何 <option>，浏览器就把它显示成第一项「默认模型」——
 * 用户看到的是"我在用默认模型"，实际发出去的却是个已经不存在的供应商。
 * 这种"看板和事实不一致"比报错更难查，所以在读回时先认一遍。
 */

/** 存过的选择只有在供应商仍然存在时才算数；认不出来就当没选过。 */
export function usableProviderId(saved: string, ids: readonly string[]): string {
  if (!saved) return "";
  return ids.includes(saved) ? saved : "";
}
