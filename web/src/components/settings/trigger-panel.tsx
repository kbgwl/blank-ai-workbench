"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import { fetchProjects, type Project } from "@/components/projects/api";
import { actOnTrigger, errorMessage, fetchTriggers, type Trigger } from "./api";
import {
  TRIGGER_ACTION_LABELS,
  triggerActions,
  triggerStatus,
  triggerTaskHref,
  triggerWhen,
} from "./trigger-view";

const TONE: Record<string, string> = {
  brand: "border-brand/40 bg-brand/10 text-brand",
  warn: "border-warn/40 bg-warn/10 text-warn",
  danger: "border-danger/40 bg-danger/10 text-danger",
  muted: "border-line bg-surface-2 text-muted",
};

/**
 * 定时与触发记录。
 *
 * 助手说「我等会儿再来」「到点自动跑」之后，原来没有任何地方能看到
 * 到底排没排上、跑没跑、跑到哪了。用户只记得它说过这句话，于是只能等——
 * 而等待里最难受的是不知道自己在等什么。
 *
 * 只显示已登记的条目，不在这里新建：新建是在对话里说一句话，这里看到它
 * 是否落地就行。已经跑过的行不提供「立即触发」——后端会回 409，
 * 界面不该给一个按了必然失败的按钮。
 */
export function TriggerPanel() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState("");
  const [rows, setRows] = useState<Trigger[] | null>(null);
  // 这一批记录属于哪个「项目 + 刷新轮次」：切换或刷新时旧记录自动视为未就绪。
  const [rowsFor, setRowsFor] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((list) => {
        if (!alive) return;
        setProjects(list);
        const bound = list.find((item) => item.workspaces.length > 0);
        setProjectId((current) => current || bound?.id || "");
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    const key = `${projectId}:${reload}`;
    fetchTriggers(projectId)
      .then((list) => {
        if (!alive) return;
        setRows(list);
        setRowsFor(key);
        setError("");
      })
      .catch((err) => {
        if (!alive) return;
        setRowsFor(key);
        setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [projectId, reload]);

  const bound = (projects ?? []).filter((item) => item.workspaces.length > 0);
  const rowsReady = rowsFor === `${projectId}:${reload}`;

  async function act(row: Trigger, action: "fire" | "pause" | "resume" | "cancel") {
    setBusy(row.id);
    setError("");
    try {
      await actOnTrigger(row.id, action);
      // 动作改的是后端状态，必须回读：本地把 status 改掉只能对这一刻正确，
      // 下一次轮询又会跳回去，用户看到的是"我点了又弹回来"。
      setReload((value) => value + 1);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel
      title="定时与触发"
      hint="说了「等会儿再做」之后，到底排没排上"
      action={
        bound.length > 1 ? (
          <SelectPopover
            value={projectId}
            onChange={setProjectId}
            options={bound.map((item) => ({ value: item.id, label: item.name }))}
            ariaLabel="选择项目"
          />
        ) : undefined
      }
    >
      {rowsReady && error && <p className="text-[13px] text-danger">读取失败：{error}</p>}
      {!rowsReady && <p className="font-mono text-[12px] text-faint">读取触发记录…</p>}
      {rowsReady && rows && rows.length === 0 && (
        <p className="text-[13px] leading-relaxed text-muted">
          还没有排过任何事。在对话里说「过一会儿……」「每天……」时，它才会登记到这里。
        </p>
      )}
      {rowsReady && rows && rows.length > 0 && (
        <ul className="flex flex-col gap-2">
          {rows.map((row) => {
            const status = triggerStatus(row.status);
            const when = triggerWhen(row);
            const href = triggerTaskHref(row);
            const actions = triggerActions(row);
            return (
              <li key={row.id} className="rounded-lg border border-line bg-surface px-2.5 py-2">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[11px] ${TONE[status.tone]}`}>
                    {status.label}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[13px] text-text" title={row.source}>
                    {row.source || row.event_type}
                  </span>
                  {when && <span className="shrink-0 font-mono text-[11px] text-faint">{when}</span>}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  {href ? (
                    <Link href={href} className="font-mono text-[11px] text-brand hover:underline">
                      看跑出来的任务 →
                    </Link>
                  ) : (
                    <span className="font-mono text-[11px] text-faint">
                      {row.status === "pending" ? "还没建任务" : "这次没有建任务"}
                    </span>
                  )}
                  {actions.map((action) => (
                    <button
                      key={action}
                      type="button"
                      disabled={busy === row.id}
                      onClick={() => void act(row, action)}
                      className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
                    >
                      {TRIGGER_ACTION_LABELS[action]}
                    </button>
                  ))}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
