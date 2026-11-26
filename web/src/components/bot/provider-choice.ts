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
import { configuredProviders } from "../configured-providers";

/** 存过的选择只有在供应商仍然存在时才算数；认不出来就当没选过。 */
export function usableProviderId(saved: string, ids: readonly string[]): string {
  if (!saved) return "";
  return ids.includes(saved) ? saved : "";
}

/**
 * 「默认模型」实际会用到的型号名。
 *
 * 用户没显式选供应商时，后端按 settings.model.default_provider 挑；没有它才
 * 回落到"有 key 的第一个"。前端过去直接拿列表第一项，于是设置里锁的是星火、
 * 界面却写 deepseek-flash——看板和事实分叉。这里优先认后端标出的 is_default。
 */
export function defaultModelLabel(providers: readonly Provider[], providerId = ""): string {
  const keyed = configuredProviders(providers);
  const chosen =
    keyed.find((provider) => providerId && provider.id === providerId) ??
    keyed.find((provider) => provider.is_default) ??
    keyed[0];
  return chosen?.model || "";
}

/**
 * 全站「当前模型」在用户没存过选择时该用哪一家。
 *
 * 设置页顶部那个模型切换器以前取「第一个启用的供应商」——库里的排序是名字，
 * 于是它显示 anthropic（没 Key、也不是默认），任务实际却跑默认的 DeepSeek。
 * 这里的口径与后端一致：先认可用（有 Key）、再认设置里的 is_default。
 */
export function defaultProviderId(providers: readonly Provider[]): string {
  const keyed = configuredProviders(providers);
  return (keyed.find((provider) => provider.is_default) ?? keyed[0])?.id ?? "";
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
    const text = chosen.model ? `${chosen.name} · ${chosen.model}` : chosen.name;
    // 选中了一家没配 key 的：显示照旧（用户确实选了它），但要报警。
    // 不报的话，界面上写着"anthropic · sonnet"、发出去 401，用户会去猜是哪一环坏了；
    // 而这条消息点名的正是那一环。
    const hasKey = (chosen.api_key_hint ?? "") !== "";
    if (!chosen.enabled) return { text: `${text}（已停用）`, warn: true };
    return { text, warn: !hasKey };
  }
  // 悬空 id 与空 id 同义：都交给后端按默认挑，不该拿红色去吓人。
  const usable = providers.some((provider) => provider.enabled && (provider.api_key_hint ?? "") !== "");
  return usable ? { text: "默认模型", warn: false } : { text: "未配置模型", warn: true };
}
