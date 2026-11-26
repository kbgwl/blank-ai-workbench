import type { PriceRow, PricingTable } from "./api";

const FIELDS = ["input", "cached_input", "output"] as const;

export type PricingParseResult =
  | { ok: true; pricing: PricingTable }
  | { ok: false; error: string };

/**
 * 解析设置页里手写的价目表 JSON。
 *
 * 后端 `settings.normalize` 会再清洗一层，但那一层是"写坏一条就清成 0"：
 * 用户保存成功后回读发现某档变成免费，界面上看不出是哪一步错了。这里先挡一次，
 * 把「哪一档、哪个字段」说清楚，再交给后端做最终夹取。
 */
export function parsePricingDraft(text: string): PricingParseResult {
  const trimmed = text.trim();
  if (!trimmed) return { ok: true, pricing: {} };

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: "价目表不是合法 JSON" };
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      ok: false,
      error: '价目表必须是对象，例如 {"default": {"input": 2, "cached_input": 0.5, "output": 8}}',
    };
  }

  const pricing: PricingTable = {};
  for (const [rawKey, rawValue] of Object.entries(parsed as Record<string, unknown>)) {
    const key = rawKey.trim().toLowerCase();
    if (!key) return { ok: false, error: "档位名不能为空" };
    if (!rawValue || typeof rawValue !== "object" || Array.isArray(rawValue)) {
      return { ok: false, error: `档位「${rawKey}」必须是对象` };
    }
    const row = rawValue as Record<string, unknown>;
    const clean: PriceRow = { input: 0, cached_input: 0, output: 0 };
    for (const field of FIELDS) {
      const value = row[field];
      if (value === undefined || value === null || value === "") continue;
      const amount = Number(value);
      if (!Number.isFinite(amount) || amount < 0) {
        return { ok: false, error: `档位「${rawKey}」的 ${field} 必须是非负数` };
      }
      clean[field] = amount;
    }
    pricing[key] = clean;
  }
  return { ok: true, pricing };
}
