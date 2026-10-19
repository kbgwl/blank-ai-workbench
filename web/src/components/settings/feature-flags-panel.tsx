"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { errorMessage, fetchFeatures, patchFeatures, type FeatureFlag, type FeaturesResponse } from "./api";

/** 生效来源的人话解释。用户改了没反应时，这一栏就是答案。 */
const SOURCE_LABELS: Record<string, string> = {
  env: "环境变量",
  settings: "设置文件",
  legacy: "老字段",
  default: "注册表默认",
};

const STAGE_LABELS: Record<string, string> = {
  stable: "稳定",
  beta: "实验",
  deprecated: "将废弃",
};

const STAGE_STYLES: Record<string, string> = {
  stable: "border-line bg-surface-2/60 text-muted",
  beta: "border-warn/40 bg-warn/10 text-warn",
  deprecated: "border-danger/40 bg-danger/10 text-danger",
};

function FlagRow({
  flag,
  busy,
  onToggle,
}: {
  flag: FeatureFlag;
  busy: boolean;
  onToggle: (next: boolean) => void;
}) {
  // 环境变量优先级最高：按钮点了也不会生效，所以直接禁用，别让人白点
  const locked = flag.source === "env";
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line/60 py-2.5 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[12px] text-text">{flag.name}</span>
          <span
            className={`rounded border px-1 py-0.5 text-[10px] ${STAGE_STYLES[flag.stage] ?? STAGE_STYLES.stable}`}
          >
            {STAGE_LABELS[flag.stage] ?? flag.stage}
          </span>
          <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 text-[10px] text-faint">
            来自{SOURCE_LABELS[flag.source] ?? flag.source}
          </span>
          {locked && (
            <span className="font-mono text-[10px] text-warn">被环境变量锁定，重启前不可改</span>
          )}
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-faint">{flag.summary}</p>
      </div>
      <button
        type="button"
        disabled={busy || locked}
        onClick={() => onToggle(!flag.enabled)}
        className={`shrink-0 rounded-md border px-3 py-1.5 text-[11px] transition disabled:opacity-60 ${
          flag.enabled ? "border-brand/60 bg-brand/10 text-text" : "border-line bg-surface text-muted"
        }`}
      >
        {flag.enabled ? "已开启" : "已关闭"}
      </button>
    </div>
  );
}

export function FeatureFlagsPanel() {
  const [data, setData] = useState<FeaturesResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  // 用 token 触发重新拉取：在 effect 里直接 setState 会级联渲染，而这里真正要
  // 同步的是"外部系统"（后端注册表），所以走 fetch 回调最贴合这套规则。
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    fetchFeatures()
      .then((next) => {
        if (!alive) return;
        setData(next);
        setError("");
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  async function toggle(name: string, next: boolean) {
    setBusy(true);
    setNotice("");
    try {
      setData(await patchFeatures({ [name]: next }));
      setNotice(`${name} 已${next ? "开启" : "关闭"}，对下一个任务生效`);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <Panel title="特性开关" hint="GET /settings/features">
        <p className="text-[12px] text-faint">{error || "加载中…"}</p>
      </Panel>
    );
  }

  return (
    <Panel title="特性开关" hint={`PATCH /settings/features · 环境变量前缀 ${data.env_prefix}`}>
      <div className="flex flex-col">
        {data.features.map((flag) => (
          <FlagRow
            key={flag.name}
            flag={flag}
            busy={busy}
            onToggle={(next) => void toggle(flag.name, next)}
          />
        ))}
      </div>

      {data.unknown.length > 0 && (
        <p className="mt-2 rounded-md border border-warn/40 bg-warn/10 px-2.5 py-2 text-[11px] leading-relaxed text-warn">
          设置文件里有认不出的开关名：{data.unknown.join("、")}。拼错的名字不会生效，改对或删掉它。
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-line/70 pt-2">
        <span className="text-[11px] text-faint">
          {notice || error || "开关值会写入设置文件；环境变量优先级更高，用来临时压过界面上的选择"}
        </span>
        <button
          type="button"
          disabled={busy}
          onClick={() => setReloadToken((token) => token + 1)}
          className="rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:border-line-strong"
        >
          刷新
        </button>
      </div>
    </Panel>
  );
}
