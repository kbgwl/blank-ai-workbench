"use client";

import { useCallback, useEffect, useState } from "react";

type Health = { status: string; database: string };

export function HealthPill() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState(false);

  const check = useCallback(() => {
    fetch("/api/v1/health")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((h: Health) => {
        setHealth(h);
        setError(false);
      })
      .catch(() => setError(true));
  }, []);

  useEffect(() => {
    check();
    const id = setInterval(check, 15000);
    return () => clearInterval(id);
  }, [check]);

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
      <span className="font-mono text-[10px] text-faint">/api/v1 · Next 代理</span>
    </div>
  );
}
