/**
 * 模型选择的持久化判定。
 *
 * 为什么单独摘出来：localStorage 里存的是 provider 的 id，而供应商随时可能被删
 * （换机器、重装、清理配置）。直接把旧 id 发下去，后端只会回一句「模型供应商不存在」，
 * 而界面上这个 id 匹配不到任何 <option>，浏览器就把它显示成第一项「默认模型」——
 * 用户看到的是"我在用默认模型"，实际发出去的却是个已经不存在的供应商。
 * 这种"看板和事实不一致"比报错更难查，所以在读回时先认一遍。
 */

import type { Provider } from "../tasks/api";

/** 存过的选择只有在供应商仍然存在时才算数；认不出来就当没选过。 */
export function usableProviderId(saved: string, ids: readonly string[]): string {
  if (!saved) return "";
  return ids.includes(saved) ? saved : "";
}

/**
 * 选择器上该显示的文案，以及它是不是真的"没配置"。
 *
 * 空 id 不等于没配置：后端会按角色分工自己挑一个默认供应商，这时候说
 * 「未配置模型」是把"没显式选"误报成"没配置"。只有一家能用的都没有时，
 * 才是真的发不出去。
 */
export function providerLabel(
  providerId: string,
  providers: Provider[],
  ready = true,
): { text: string; warn: boolean } {
  // 还没读到供应商列表时，空数组只代表"没读到"，不代表"一家都没有"。
  // 这一瞬间报红，用户看到的是自己的配置被否认了一次——和"未配置模型"是两回事。
  if (!ready) return { text: "读取中", warn: false };
  const chosen = providerId ? providers.find((provider) => provider.id === providerId) : undefined;
  if (chosen) {
    return { text: chosen.model ? `${chosen.name} · ${chosen.model}` : chosen.name, warn: false };
  }
  // 悬空 id 与空 id 同义：都交给后端按默认挑，不该拿红色去吓人。
  const usable = providers.some((provider) => provider.enabled && (provider.api_key_hint ?? "") !== "");
  return usable ? { text: "默认模型", warn: false } : { text: "未配置模型", warn: true };
}
