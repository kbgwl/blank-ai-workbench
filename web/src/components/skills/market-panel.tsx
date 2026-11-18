"use client";

/**
 * 技能市场：**先说要做的事，再去 GitHub 找**。
 *
 * 流程对齐用户的原话："内置不符合就去 GitHub 找技能/工具，但要有证据——
 * 拉的是什么、多少分"。所以每一条结果都带 stars、仓库、目录与最近更新，
 * 装完在技能库里也能看到来源戳。
 *
 * 三种来源形状（后端适配，这里只展示）：
 * - GitHub 仓库：任意子目录下的 SKILL.md → 可装成技能；没有 SKILL.md → 当工具
 * - Claude 市场清单：`.claude-plugin/marketplace.json`
 * - 我们自己的清单：`registry.json`（离线可读，带 stars 快照与核对日期）
 */

import { useCallback, useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { errorMessage } from "@/lib/http";
import {
  fetchMarket,
  fetchMarketSources,
  installFromMarket,
  installFromRepo,
  searchMarket,
  type MarketHit,
  type MarketItem,
  type MarketSource,
} from "./api";

export function SkillMarketPanel({ onInstalled }: { onInstalled: () => void }) {
  const [sources, setSources] = useState<MarketSource[]>([]);
  const [source, setSource] = useState("curated");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<MarketHit[] | null>(null);
  const [items, setItems] = useState<MarketItem[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [installing, setInstalling] = useState("");
  const [note, setNote] = useState("");
  // 网络不可用时后端会回落到本机目录，并把原因写在这里。
  const [fallback, setFallback] = useState("");
  const [scope, setScope] = useState<"user" | "project">("user");

  useEffect(() => {
    let alive = true;
    fetchMarketSources()
      .then((rows) => {
        if (alive) setSources(rows);
      })
      .catch(() => {
        // 来源清单读不到不影响搜索（搜索走 GitHub 公共接口）。
      });
    return () => {
      alive = false;
    };
  }, []);

  const runSearch = useCallback(async () => {
    const text = query.trim();
    if (!text) return;
    setLoading(true);
    setError("");
    setNote("");
    setHits(null);
    setItems(null);
    setFallback("");
    try {
      const result = await searchMarket(text);
      setHits(result.items);
      setFallback(result.origin === "local-cache" ? result.notice : "");
    } catch (cause) {
      setHits([]);
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }, [query]);

  const loadSource = useCallback(async () => {
    if (!source) return;
    setLoading(true);
    setError("");
    setNote("");
    setHits(null);
    setItems(null);
    try {
      const result = await fetchMarket(source);
      setItems(result.items);
    } catch (cause) {
      setItems([]);
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  }, [source]);

  async function installHit(hit: MarketHit) {
    setInstalling(`${hit.repo}:${hit.path}`);
    setError("");
    setNote("");
    try {
      const result = await installFromRepo({ repo: hit.repo, path: hit.path, scope, stars: hit.stars });
      setNote(`已安装「${result.name}」（来自 ${hit.repo}，★${hit.stars}）。`);
      onInstalled();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setInstalling("");
    }
  }

  async function installItem(item: MarketItem) {
    setInstalling(item.id);
    setError("");
    setNote("");
    try {
      const result = await installFromMarket({ source, item_id: item.id, scope });
      setNote(`已安装「${result.name}」。`);
      onInstalled();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setInstalling("");
    }
  }

  return (
    <Panel title="从市场拉取" hint="先说要做的事，去 GitHub 找；也可以按来源浏览">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void runSearch();
          }}
          placeholder="要做的事，例如：给 PDF 提取表格 / 写一个 Playwright 测试"
          aria-label="搜索技能或工具"
          className="min-w-[17rem] flex-1 rounded-md border border-line bg-surface px-2 py-1.5 text-[12px] text-text placeholder:text-faint focus:outline-none"
        />
        <select
          value={scope}
          onChange={(event) => setScope(event.target.value as "user" | "project")}
          aria-label="装到哪"
          className="rounded-md border border-line bg-surface px-2 py-1.5 text-[12px] text-text focus:outline-none"
        >
          <option value="user">装到「我的」</option>
          <option value="project">装到当前项目</option>
        </select>
        <button
          type="button"
          disabled={loading || query.trim() === ""}
          onClick={() => void runSearch()}
          className="rounded-md border border-brand/40 bg-brand/10 px-2 py-1 text-[12px] text-brand transition-colors hover:bg-brand/15 disabled:opacity-50"
        >
          {loading ? "搜索中…" : "去 GitHub 找"}
        </button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-line pt-2">
        <span className="text-[11px] text-faint">或按来源浏览</span>
        <select
          value={source}
          onChange={(event) => setSource(event.target.value)}
          aria-label="市场来源"
          className="rounded-md border border-line bg-surface px-2 py-1 text-[12px] text-text focus:outline-none"
        >
          {sources.map((row) => (
            <option key={row.id} value={row.id}>
              {row.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={loading || !source}
          onClick={() => void loadSource()}
          className="rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
        >
          拉取列表
        </button>
      </div>

      {note && <p className="mt-2 text-[12px] text-accent">{note}</p>}
      {error && <p className="mt-2 text-[12px] text-danger">{error}</p>}
      {fallback && <p className="mt-2 text-[12px] text-warn">{fallback}</p>}

      {hits !== null && (
        <div className="mt-2.5 flex flex-col gap-1.5">
          {hits.length === 0 && !loading && !error && (
            <p className="text-[12px] text-faint">
              没找到。换个更具体的关键词，或者直接把需求说清楚，让模型用现有工具做。
            </p>
          )}
          {hits.map((hit) => {
            const key = `${hit.repo}:${hit.path}`;
            const isSkill = hit.kind === "skill";
            return (
              <div key={key} className="flex items-start gap-2 rounded-lg border border-line bg-surface px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium">{hit.name}</span>
                    <span
                      className={`shrink-0 rounded border px-1 py-0.5 text-[11px] ${
                        isSkill ? "border-brand/30 bg-brand/10 text-brand" : "border-line bg-surface-2 text-muted"
                      }`}
                    >
                      {isSkill ? "技能" : "工具/仓库"}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-faint">★ {hit.stars}</span>
                  </div>
                  <p className="mt-1 text-[12px] leading-relaxed text-muted">{hit.description || "（没有说明）"}</p>
                  <p className="mt-1 truncate font-mono text-[11px] text-faint">
                    {hit.repo}
                    {hit.path ? ` · ${hit.path}` : ""} · 更新 {hit.updated_at.slice(0, 10) || "未知"}
                  </p>
                </div>
                {isSkill ? (
                  <button
                    type="button"
                    disabled={installing !== ""}
                    onClick={() => void installHit(hit)}
                    className="shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
                  >
                    {installing === key ? "安装中…" : "安装"}
                  </button>
                ) : (
                  <a
                    href={hit.url}
                    target="_blank"
                    rel="noreferrer"
                    className="shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
                  >
                    打开
                  </a>
                )}
              </div>
            );
          })}
        </div>
      )}

      {items !== null && (
        <div className="mt-2.5 flex flex-col gap-1.5">
          {items.length === 0 && (
            <p className="text-[12px] text-faint">
              这个来源里没有条目。（我们自己的清单还没收录外部技能：随包的 12 条已可直接用。）
            </p>
          )}
          {items.map((item) => (
            <div key={item.id} className="flex items-start gap-2 rounded-lg border border-line bg-surface px-3 py-2">
              <div className="min-w-0 flex-1">
                <span className="truncate text-[13px] font-medium">{item.name}</span>
                {item.description && (
                  <p className="mt-1 text-[12px] leading-relaxed text-muted">{item.description}</p>
                )}
              </div>
              <button
                type="button"
                disabled={installing !== ""}
                onClick={() => void installItem(item)}
                className="shrink-0 rounded-md border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
              >
                {installing === item.id ? "安装中…" : "安装"}
              </button>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
