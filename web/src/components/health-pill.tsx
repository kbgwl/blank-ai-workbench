"use client";

import { useEffect, useState } from "react";

import { API_V1 } from "@/lib/http";

type Health = { status: string; database: string };

/**
 * 后端健康徽标。
 *
 * 注意重试策略：桌面壳里第一次跨源请求可能瞬时失败（实测：启动瞬间失败，15 秒后的
 * 下一次轮询就正常）。如果用固定 15 秒间隔，用户每次开应用都会先看到 15 秒的
 * 「后端不可达」——明明一切正常。所以失败后做退避重试，恢复后再回到低频轮询。
 */
export function HealthPill() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = 800;

    const check = async () => {
      try {
        const response = await fetch(`${API_V1}/health`);
        if (!response.ok) throw new Error(String(response.status));
        const parsed = (await response.json()) as Health;
        if (cancelled) return;
        setHealth(parsed);
        setError(false);
        delay = 15000;
      } catch {
        if (cancelled) return;
        setError(true);
        delay = Math.min(delay * 2, 15000);
      }
      if (!cancelled) timer = setTimeout(check, delay);
    };

    void check();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const state = error || !health ? "down" : health.status === "ok" ? "up" : "degraded";
  const dot = state === "up" ? "bg-accent" : state === "degraded" ? "bg-warn" : "bg-danger";
  const label =
    state === "up"
      ? "服务正常"
      : state === "degraded"
        ? `数据库${health?.database ?? "异常"}`
        : "后端不可达";

  return (
    <div className="flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1.5 text-xs text-muted">
      <span className="relative flex size-1.5">
        <span className={`absolute inline-flex size-full rounded-full ${dot} opacity-70`} />
        {state === "up" && (
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-60" />
        )}
      </span>
      <span className="text-text/90">{label}</span>
      <span className="font-mono text-[10px] text-faint">/api/v1</span>
    </div>
  );
}
