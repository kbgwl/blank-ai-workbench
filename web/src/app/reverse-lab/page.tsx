"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { activeWorkspace, fetchProjects, type Project } from "@/components/projects/api";
import { Button, Field, Input, Notice, Spinner, StateLine } from "@/components/projects/ui";
import {
  adapterLabel,
  adapterReason,
  adapterTone,
  createReaInvestigation,
  fetchReaLabStatus,
  fetchReaStrings,
  fetchReaTargets,
  findingLabel,
  offsetLabel,
  targetHint,
  type Finding,
  type InvestigationResponse,
  type ReaLabStatus,
  type ReaTarget,
  type StringHit,
} from "@/components/reverse-lab/api";
import { errorMessage } from "@/lib/http";

/**
 * 逆向实验室：把「挑实物 → 抽线索 → 出一份带 Evidence ID 的结论」串成一条链路。
 *
 * 三栏对应三个动作，全部真调后端（`/api/v1/reverse-lab/*`）：
 * 左边挑目标（`scan_targets`），中间看字符串线索（`find_strings`），
 * 右边是这次调查留下的台账（`inspect_target` 的结果 + 任务/事件/证据包入口）。
 *
 * 两条口径写在界面上，不藏在文档里：
 * 1. 适配器不在线时直接说服务端那句话，并把按钮禁掉——不给一个点了必然失败的按钮。
 * 2. 候选目标的格式是幻数猜的（前 16 字节），所以措辞是「像是可执行」。
 */

const TONE_CLASS: Record<"ok" | "warn" | "bad", string> = {
  ok: "border-ok/30 bg-ok/[0.06] text-ok",
  warn: "border-warn/30 bg-warn/[0.06] text-warn",
  bad: "border-danger/30 bg-danger/[0.06] text-danger",
};

export default function ReverseLabPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [status, setStatus] = useState<ReaLabStatus | null>(null);
  const [targets, setTargets] = useState<ReaTarget[]>([]);
  const [truncated, setTruncated] = useState(0);
  const [targetsNote, setTargetsNote] = useState("");
  const [subPath, setSubPath] = useState("");
  const [selected, setSelected] = useState("");
  const [strings, setStrings] = useState<StringHit[] | null>(null);
  const [stringsNote, setStringsNote] = useState("");
  const [report, setReport] = useState<InvestigationResponse | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");

  const project = useMemo(() => projects.find((item) => item.id === projectId) ?? null, [projects, projectId]);
  const workspace = project ? activeWorkspace(project) : null;
  const tone = status ? adapterTone(status.adapter.state) : "warn";
  const online = status ? tone === "ok" : false;

  // 换项目时把上一份读数全部作废，一次做完——散在 effect 里同步复位会被
  // `react-hooks/set-state-in-effect` 判成交互期级联渲染，也确实容易漏掉某一块。
  const chooseProject = useCallback((id: string) => {
    setProjectId(id);
    setStatus(null);
    setTargets([]);
    setTruncated(0);
    setSelected("");
    setStrings(null);
    setStringsNote("");
    setReport(null);
    setNotice("");
    setBusy(id ? "status" : "");
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchProjects()
      .then((list) => {
        if (cancelled) return;
        setProjects(list);
        const first = list.find((item) => activeWorkspace(item)) ?? list[0];
        if (first) chooseProject(first.id);
        else setNotice("还没有项目。先在「项目」里建一个并绑定工作区，REA 才有授权根。");
      })
      .catch((error) => {
        if (!cancelled) setNotice(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [chooseProject]);

  const loadTargets = useCallback(async (id: string, path: string) => {
    setBusy("targets");
    try {
      const body = await fetchReaTargets(id, path);
      setTargets(body.targets);
      setTruncated(body.truncated);
      setTargetsNote(body.note);
      setNotice("");
    } catch (error) {
      setTargets([]);
      setNotice(errorMessage(error));
    } finally {
      setBusy("");
    }
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    fetchReaLabStatus(projectId)
      .then(async (body) => {
        if (cancelled) return;
        setStatus(body);
        // 适配器不在线就别再去列举——那一定会 503，界面上只会多一条无意义的红字
        if (adapterTone(body.adapter.state) === "ok") {
          await loadTargets(projectId, "");
        } else {
          setTargets([]);
          setNotice(adapterReason(body.adapter));
        }
      })
      .catch((error) => {
        if (cancelled) return;
        setStatus(null);
        setTargets([]);
        setNotice(errorMessage(error));
      })
      .finally(() => {
        if (!cancelled) setBusy("");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, loadTargets]);

  async function reconnect() {
    if (!projectId) return;
    setBusy("status");
    try {
      const body = await fetchReaLabStatus(projectId, true);
      setStatus(body);
      if (adapterTone(body.adapter.state) === "ok") {
        setNotice("");
        await loadTargets(projectId, subPath.trim());
      } else {
        setNotice(adapterReason(body.adapter));
      }
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  async function pickTarget(target: string) {
    setSelected(target);
    setStrings(null);
    setReport(null);
    setBusy("strings");
    try {
      const body = await fetchReaStrings(projectId, target);
      setStrings(body.strings);
      setStringsNote(body.empty_reason || body.note);
    } catch (error) {
      setStrings([]);
      setStringsNote(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  async function investigate() {
    if (!projectId || !selected) return;
    setBusy("run");
    setReport(null);
    try {
      const body = await createReaInvestigation({ projectId, target: selected });
      setReport(body);
      // 调查失败是"结果"而不是"报错"：台账已经落了，界面照实说哪一步没过
      setNotice(body.ok ? "" : body.reason || `调查未完成（${body.state}）`);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy("");
    }
  }

  const findings: Finding[] = report?.findings ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 overflow-y-auto p-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="font-mono text-[10px] tracking-wide text-faint uppercase">REVERSE LAB</div>
          <h1 className="mt-1 text-[16px] font-semibold">逆向分析</h1>
          <p className="mt-1 text-[12px] text-muted">
            挑一个真实产物，抽它的字符串线索，再把结论落成一条带 Evidence ID 的任务。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={projectId}
            onChange={(event) => chooseProject(event.target.value)}
            className="rounded-lg border border-line bg-surface-2 px-2 py-2 text-[12px] text-text focus:border-brand/60 focus:outline-none"
          >
            {projects.length === 0 && <option value="">没有项目</option>}
            {projects.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
                {activeWorkspace(item) ? "" : "（未绑定工作区）"}
              </option>
            ))}
          </select>
          <span className={`rounded-md border px-2 py-1 text-[11px] ${TONE_CLASS[tone]}`}>
            {status ? adapterLabel(status.adapter.state) : "状态未知"}
          </span>
          <Button onClick={() => void reconnect()} disabled={!projectId || busy === "status"}>
            {busy === "status" ? <Spinner /> : null}
            重连
          </Button>
        </div>
      </header>

      {notice && <Notice tone={online ? "warn" : "error"}>{notice}</Notice>}

      {!workspace && projectId && (
        <Notice tone="warn">
          这个项目还没绑定工作区。REA 的授权根就是项目的活动工作区，没绑就无从扫描—— 去{" "}
          <Link href="/projects" className="text-brand hover:underline">
            项目
          </Link>{" "}
          里绑一个目录。
        </Notice>
      )}

      <div className="grid min-h-0 flex-1 gap-4 xl:grid-cols-[19rem_1fr_21rem]">
        {/* 左：候选实物 */}
        <section className="flex min-h-0 flex-col rounded-lg border border-line bg-surface/15">
          <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
            <span className="font-mono text-[10px] tracking-wide text-faint uppercase">候选实物</span>
            <span className="text-[10px] text-faint">{targets.length} 个</span>
          </div>
          <div className="flex items-end gap-2 border-b border-line px-3 py-2">
            <Field label="子目录">
              <Input
                value={subPath}
                onChange={(event) => setSubPath(event.target.value)}
                placeholder="留空 = 工作区根"
                className="py-1.5 text-[12px]"
              />
            </Field>
            <Button
              onClick={() => void loadTargets(projectId, subPath.trim())}
              disabled={!online || busy === "targets"}
            >
              {busy === "targets" ? <Spinner /> : null}
              扫描
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {targets.length === 0 ? (
              <StateLine>{online ? "这个范围里没有可列举的文件" : "适配器不在线，扫描不可用"}</StateLine>
            ) : (
              targets.map((item) => {
                const active = item.target === selected;
                return (
                  <button
                    key={item.target}
                    type="button"
                    onClick={() => void pickTarget(item.target)}
                    className={`mb-1 flex w-full flex-col items-start gap-0.5 rounded-md px-2 py-1.5 text-left transition-colors ${
                      active ? "bg-brand/10 text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                    }`}
                  >
                    <span className="w-full truncate font-mono text-[12px]">{item.target}</span>
                    <span className="text-[10px] text-faint">{targetHint(item)}</span>
                  </button>
                );
              })
            )}
            {truncated > 0 && (
              <p className="px-2 py-2 text-[10px] text-faint">另有 {truncated} 个超出上限没列举</p>
            )}
            {targetsNote && <p className="px-2 py-2 text-[10px] text-faint">{targetsNote}</p>}
          </div>
        </section>

        {/* 中：字符串线索 + 调查 */}
        <section className="flex min-h-0 flex-col rounded-lg border border-line bg-surface/15">
          <div className="flex items-center justify-between gap-3 border-b border-line px-3 py-2">
            <div className="min-w-0">
              <div className="font-mono text-[10px] tracking-wide text-faint uppercase">字符串线索</div>
              <div className="mt-0.5 truncate font-mono text-[12px] text-text">
                {selected || "先从左栏挑一个目标"}
              </div>
            </div>
            <Button
              variant="primary"
              onClick={() => void investigate()}
              disabled={!online || !selected || busy === "run"}
            >
              {busy === "run" ? <Spinner /> : null}
              开始调查
            </Button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {busy === "strings" ? (
              <StateLine>抽取中…</StateLine>
            ) : !selected ? (
              <StateLine>还没选目标</StateLine>
            ) : strings === null ? (
              <StateLine>点一下左栏的目标</StateLine>
            ) : strings.length === 0 ? (
              <StateLine>{stringsNote || "前导区间里没有可读串"}</StateLine>
            ) : (
              <table className="w-full border-collapse text-[12px]">
                <tbody>
                  {strings.map((hit) => (
                    <tr key={`${hit.offset}-${hit.text}`} className="border-b border-line/60 last:border-0">
                      <td className="w-[7.5rem] px-3 py-1 align-top font-mono text-[11px] text-faint">
                        {offsetLabel(hit.offset)}
                      </td>
                      <td className="px-3 py-1 break-all font-mono text-text">{hit.text}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {strings && strings.length > 0 && stringsNote && (
              <p className="px-3 py-2 text-[10px] text-faint">{stringsNote}</p>
            )}
          </div>

          {report && (
            <div className="border-t border-line p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-[10px] tracking-wide text-faint uppercase">调查结论</span>
                <span
                  className={`rounded border px-1.5 py-0.5 text-[10px] ${
                    report.ok ? TONE_CLASS.ok : TONE_CLASS.bad
                  }`}
                >
                  {report.ok ? `${findings.length} 条` : report.state}
                </span>
              </div>
              {findings.length === 0 ? (
                <p className="mt-2 text-[12px] text-muted">{report.reason || "这次没有产出结论"}</p>
              ) : (
                <ul className="mt-2 space-y-1.5">
                  {findings.map((item) => (
                    <li key={`${item.evidence_id}-${item.kind}-${item.value}`} className="flex items-start gap-2">
                      <span className="mt-0.5 shrink-0 rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                        {item.evidence_id}
                      </span>
                      <span className="text-[12px] text-text">{findingLabel(item)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </section>

        {/* 右：台账与证据 */}
        <section className="flex min-h-0 flex-col gap-3 overflow-y-auto rounded-lg border border-line bg-surface/15 p-3">
          <div>
            <div className="font-mono text-[10px] tracking-wide text-faint uppercase">证据与边界</div>
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              调查是一次真实工具调用，会留下任务、事件与工具账。
            </p>
          </div>

          <dl className="space-y-2 text-[11px]">
            <Row label="授权根" value={status?.root ?? workspace?.path ?? "—"} mono />
            <Row label="服务端" value={status?.adapter.server ?? "—"} />
            <Row
              label="工具表"
              value={status ? `${status.adapter.tools_count} / ${status.adapter.expected.length}` : "—"}
            />
            <Row
              label="连接耗时"
              value={typeof status?.adapter.connect_ms === "number" ? `${status.adapter.connect_ms} ms` : "—"}
            />
          </dl>

          {status && (
            <div className="rounded-md border border-line bg-surface-2/40 p-2 text-[10px] leading-relaxed text-faint">
              {status.adapter.boundary_note}
            </div>
          )}

          {report && (
            <div className="space-y-2 border-t border-line pt-3">
              <div className="font-mono text-[10px] tracking-wide text-faint uppercase">这次调查</div>
              <dl className="space-y-2 text-[11px]">
                <Row label="工具" value={report.evidence.tool ?? "—"} mono />
                <Row label="调用号" value={report.evidence.tool_call_id ?? "—"} mono />
                <Row
                  label="耗时"
                  value={typeof report.evidence.duration_ms === "number" ? `${report.evidence.duration_ms} ms` : "—"}
                />
                <Row label="任务状态" value={report.task.status} />
              </dl>
              <Link
                href={`/tasks/detail?id=${report.task.id}`}
                className="inline-block rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:border-brand hover:text-text"
              >
                打开任务 →
              </Link>
            </div>
          )}

          <p className="mt-auto text-[10px] leading-relaxed text-faint">
            这一条链路调的是宿主进程里的 REA 子进程，不套任务的沙箱档位；文件与网络边界由「授权根 +
            路径闸门」保证，不是由容器保证。
          </p>
        </section>
      </div>
    </div>
  );
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="shrink-0 text-faint">{label}</dt>
      <dd className={`min-w-0 truncate text-right text-muted ${mono ? "font-mono text-[10px]" : ""}`}>{value}</dd>
    </div>
  );
}
