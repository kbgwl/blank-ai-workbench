"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Panel } from "@/components/panel";
import { errorMessage } from "@/lib/http";
import { emptyOverview, fetchMonitor, releaseTrigger, type MonitorOverview } from "./api";
import {
  clockTime,
  intentLabel,
  intentTone,
  judgementOutcome,
  pendingLine,
  relativeTime,
  statusLabel,
  statusTone,
  type IntentTone,
} from "./format";

const INTENT_CLASS: Record<IntentTone, string> = {
  act: "border-brand/30 bg-brand/10 text-brand",
  reply: "border-line bg-surface-2 text-muted",
  control: "border-warn/30 bg-warn/10 text-warn",
  defer: "border-accent/30 bg-accent/10 text-accent",
  unknown: "border-line bg-surface-2 text-faint",
};

const STATUS_CLASS: Record<ReturnType<typeof statusTone>, string> = {
  running: "border-brand/30 bg-brand/10 text-brand",
  waiting: "border-warn/30 bg-warn/10 text-warn",
  failed: "border-danger/25 bg-danger/[0.06] text-danger",
  idle: "border-line bg-surface-2 text-muted",
};

const REFRESH_MS = 5000;

function Stat({ label, value, hint, tone }: { label: string; value: number; hint?: string; tone?: "warn" }) {
  return (
    <div className="rounded-lg border border-line/70 bg-surface/40 px-3 py-2.5">
      <p className="font-mono text-[10px] uppercase tracking-wide text-faint">{label}</p>
      <p className={`mt-1 text-[19px] font-semibold leading-none ${tone === "warn" ? "text-warn" : ""}`}>{value}</p>
      {hint && <p className="mt-1 text-[10.5px] leading-relaxed text-faint">{hint}</p>}
    </div>
  );
}

/**
 * 监控：它现在在盯什么。
 *
 * 这一页存在的理由只有一个——Chat Supervisor 其实每收到一条消息都在判一次，
 * 但那件事只落在事件流里，用户在界面上看不见，于是「持续监控聊天」等于没做。
 * 所以这里把判定、在跑的任务、待触发项、触发源是否真接通摆在一屏，并且
 * **不美化**：等不到事件的待触发项、没接通的聊天软件，都照实写。
 */
export function MonitorView() {
  const [data, setData] = useState<MonitorOverview>(() => emptyOverview());
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [releasing, setReleasing] = useState("");
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const next = await fetchMonitor();
      if (!alive.current) return;
      setData(next);
      setError("");
      setLoaded(true);
    } catch (cause) {
      if (!alive.current) return;
      setError(errorMessage(cause));
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    // 首屏与轮询走同一条路径：放进定时器而不是直接在 effect 体里调用，
    // 是为了避开 react-hooks/set-state-in-effect（同步 setState 会触发级联渲染）。
    const run = () => {
      void load();
    };
    const first = setTimeout(run, 0);
    const timer = setInterval(run, REFRESH_MS);
    return () => {
      alive.current = false;
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [load]);

  const release = useCallback(
    async (triggerId: string) => {
      setReleasing(triggerId);
      try {
        await releaseTrigger(triggerId);
        await load();
      } catch (cause) {
        setError(errorMessage(cause));
      } finally {
        if (alive.current) setReleasing("");
      }
    },
    [load],
  );

  const { counts, watching, running, pending, sources } = data;
  const builtin = sources.filter((item) => item.kind === "builtin");
  const im = sources.filter((item) => item.kind === "im");
  const imOff = im.filter((item) => !item.connected).length;
  // 真正在跑的排前面，草稿（说了要盯但没事件源，建完没启动）排最后：
  // 混在一起时「5 条未结束」读起来像是五件事在同时跑。
  const ORDER: Record<string, number> = { running: 0, queued: 1, waiting_approval: 2, paused: 3, draft: 4 };
  const ordered = [...running].sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9));
  const drafts = ordered.filter((item) => item.status === "draft").length;

  return (
    <div className="mx-auto w-full max-w-5xl px-5 py-6">
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[17px] font-semibold tracking-tight">监控</h1>
          <p className="mt-1.5 max-w-2xl text-[12.5px] leading-relaxed text-muted">
            它现在在盯什么。监督者每收到一条消息都会判一次——要不要动手、要不要问、要不要接着上一轮——
            以前这件事只发生在事件流里，看不见。这里不做美化：等不到事件的待触发项、没接通的聊天软件，都照实写。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="font-mono text-[10px] text-faint">
            {data.now ? `读数 ${clockTime(data.now)}` : "读取中…"}
          </span>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-md border border-line bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:text-text"
          >
            刷新
          </button>
        </div>
      </header>

      {error && (
        <p className="mb-4 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2 text-[12px] text-danger">{error}</p>
      )}

      <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="已判定" value={counts.watching} hint="最近这些消息各判成了什么" />
        <Stat label="正在跑" value={counts.running} hint="后台还有多少条在动" />
        <Stat label="等你处理" value={counts.waiting_approval} hint="卡在审批或已暂停" tone={counts.waiting_approval > 0 ? "warn" : undefined} />
        <Stat
          label="待触发"
          value={counts.pending}
          hint={counts.never_fires > 0 ? `其中 ${counts.never_fires} 条永远等不到` : "等事件或等时间"}
          tone={counts.never_fires > 0 ? "warn" : undefined}
        />
      </div>

      <div className="space-y-3">
        <Panel title="它把每句话判成了什么" hint={`每 ${REFRESH_MS / 1000} 秒自动刷`}>
          {!loaded && <p className="py-6 text-center font-mono text-[11px] text-faint">读取中…</p>}
          {loaded && watching.length === 0 && (
            <p className="py-6 text-center text-[12px] leading-relaxed text-faint">
              还没有判定记录。在对话页发一句话，它怎么判的会立刻出现在这里。
            </p>
          )}
          <div className="space-y-1">
            {watching.map((item) => (
              <div
                key={item.task_id}
                className="flex items-start gap-2 rounded-md border border-line/60 bg-surface/30 px-2.5 py-2"
              >
                <span className="mt-0.5 shrink-0 font-mono text-[10px] text-faint">
                  {clockTime(item.created_at)}
                </span>
                <span
                  className={`mt-0.5 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] ${INTENT_CLASS[intentTone(item.intent)]}`}
                >
                  {intentLabel(item.intent)}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[12.5px]">{item.goal || item.title || "（空目标）"}</p>
                  <p className="mt-0.5 truncate text-[10.5px] text-faint">
                    {judgementOutcome(item)}
                    {item.reason ? ` · ${item.reason}` : ""}
                    {item.permission_mode ? ` · 权限 ${item.permission_mode}` : ""}
                  </p>
                </div>
                <span
                  className={`mt-0.5 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] ${STATUS_CLASS[statusTone(item.status)]}`}
                >
                  {statusLabel(item.status)}
                </span>
              </div>
            ))}
          </div>
        </Panel>

        <Panel
          title="正在跑 / 等你处理"
          hint={drafts > 0 ? `${running.length} 条未结束 · 其中 ${drafts} 条草稿没启动` : `${running.length} 条未结束`}
        >
          {loaded && running.length === 0 && (
            <p className="py-5 text-center text-[12px] leading-relaxed text-faint">现在没有在跑的任务，也没有卡在审批的。</p>
          )}
          <div className="space-y-1">
            {ordered.map((item) => (
              <div key={item.task_id} className="flex items-center gap-2 rounded-md border border-line/60 bg-surface/30 px-2.5 py-2">
                <span
                  className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] ${STATUS_CLASS[statusTone(item.status)]}`}
                >
                  {statusLabel(item.status)}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12.5px]">
                  {item.goal || item.title}
                  {item.status === "draft" ? <span className="ml-1 text-[10.5px] text-faint">· 建了但没启动</span> : null}
                </span>
                <span className="hidden shrink-0 font-mono text-[10px] text-faint sm:block">
                  {relativeTime(item.updated_at)}
                </span>
                <a
                  href={`/tasks/detail?id=${encodeURIComponent(item.task_id)}`}
                  className="shrink-0 rounded border border-line-strong bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:text-text"
                >
                  详情
                </a>
              </div>
            ))}
          </div>
        </Panel>

        <Panel
          title="待触发（说「盯着某件事」之后登记在这里）"
          hint={data.background_execution ? `${pending.length} 条 pending` : "后台执行未开启"}
        >
          {!data.background_execution && (
            <p className="py-3 text-[11.5px] leading-relaxed text-warn">
              后台执行关着：待触发项不会有人消费。在设置里打开「后台主动执行」，这里才会有意义。
            </p>
          )}
          {data.background_execution && loaded && pending.length === 0 && (
            <p className="py-5 text-center text-[12px] leading-relaxed text-faint">
              没有待触发项。说一句「盯着 XX，有变化就告诉我」会登记一条到这里。
            </p>
          )}
          <div className="space-y-1">
            {pending.map((item) => (
              <div key={item.trigger_id} className="rounded-md border border-line/60 bg-surface/30 px-2.5 py-2">
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[12.5px]">{item.goal || "（没有写要盯什么）"}</p>
                    <p className={`mt-0.5 text-[10.5px] leading-relaxed ${item.never_fires ? "text-warn" : "text-faint"}`}>
                      {pendingLine(item)}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={releasing === item.trigger_id}
                    onClick={() => void release(item.trigger_id)}
                    className="shrink-0 rounded border border-line-strong bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:text-text disabled:opacity-50"
                  >
                    {releasing === item.trigger_id ? "放行中…" : "立即放行"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </Panel>

        <Panel title="触发源：它到底能收到什么" hint={`${builtin.length} 个内置 · ${im.length} 个外部接入`}>
          <p className="mb-2 text-[11px] leading-relaxed text-faint">
            这一栏是这一页最该看的地方。下面「已接通」的才会真的把事件送进来；「未接通」的读不到任何消息——
            要接的话去设置里的「消息通道」，填完凭据点一次验证，这里的状态会跟着变。
          </p>
          <div className="space-y-1">
            {builtin.map((item) => (
              <div key={item.id} className="flex items-start gap-2 rounded-md border border-line/60 bg-surface/30 px-2.5 py-1.5">
                <span className="mt-0.5 shrink-0 rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-accent">
                  已接通
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12px]">{item.label}</p>
                  <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">{item.note}</p>
                </div>
              </div>
            ))}
            {im.map((item) => (
              <div
                key={item.id}
                className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 ${
                  item.connected ? "border-line/60 bg-surface/30" : "border-warn/25 bg-warn/[0.04]"
                }`}
              >
                <span
                  className={`mt-0.5 shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] ${
                    item.connected ? "border-accent/30 bg-accent/10 text-accent" : "border-warn/30 bg-warn/10 text-warn"
                  }`}
                >
                  {item.connected ? "已接通" : "未接通"}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12px]">{item.label}</p>
                  <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">{item.note}</p>
                </div>
              </div>
            ))}
          </div>
          {imOff > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-line/60 bg-surface-2/50 px-2.5 py-1.5">
              <p className="min-w-0 flex-1 text-[10.5px] leading-relaxed text-muted">
                {`${imOff} 个还没接通。企业微信/钉钉/飞书都在设置里配，不需要改代码：填凭据 → 验证 → 选收消息的方式。`}
              </p>
              <a
                href="/settings#channels"
                className="shrink-0 rounded border border-line-strong bg-surface px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:text-text"
              >
                去配置
              </a>
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
