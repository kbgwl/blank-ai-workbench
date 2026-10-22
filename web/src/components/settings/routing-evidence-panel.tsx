"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { errorMessage, fetchRoutingEvidence, type RoutingEvidence } from "./api";

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/**
 * 按需选模型（model_routing）的转正看板。
 *
 * 这个面板存在的理由很具体：features.py 里写着"那个比值是转正的唯一依据"，
 * 但那个比值一直藏在事件流的 payload 里，没有出口。没有出口的指标等于没有指标——
 * beta 开关就会永远停在"默认关闭"，因为谁都不敢凭感觉打开。
 */
export function RoutingEvidencePanel() {
  const [data, setData] = useState<RoutingEvidence | null>(null);
  const [error, setError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    fetchRoutingEvidence()
      .then((next) => {
        if (alive) {
          setData(next);
          setError("");
        }
      })
      .catch((problem: unknown) => {
        if (alive) setError(errorMessage(problem));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  const tone =
    data?.verdict === "值得转正"
      ? "border-ok/40 bg-ok/10 text-ok"
      : data?.verdict === "暂不转正"
        ? "border-warn/40 bg-warn/10 text-warn"
        : "border-line bg-surface-2/60 text-muted";

  return (
    <Panel title="转正依据">
      {error && <p className="text-[12px] text-danger">{error}</p>}

      {!data ? (
        !error && <p className="text-[12px] text-faint">正在读取最近的运行记录…</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-start justify-between gap-3">
          <p className="text-[11.5px] leading-relaxed text-faint">
            分母是「每一步都交给池子里最强模型」的成本当量，所以这个比值不依赖任何一家的真实单价。
            省 3% 却多一个可能判错的环节，不值得开；省 20% 以上且失败率没抬头，才值得开成默认。
          </p>
          <button
            type="button"
            onClick={() => setReloadToken((token) => token + 1)}
            className="shrink-0 rounded-md border border-line px-2 py-1 text-[11px] text-muted transition hover:bg-surface-2"
          >
            刷新
          </button>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded border px-2 py-0.5 text-[11px] ${tone}`}>{data.verdict}</span>
            <span className="rounded border border-line bg-surface-2/60 px-2 py-0.5 font-mono text-[11px] text-muted">
              省下 {percent(data.saved_ratio)}
            </span>
            <span className="rounded border border-line bg-surface-2/60 px-2 py-0.5 font-mono text-[11px] text-muted">
              切档 {percent(data.switch_ratio)}
            </span>
            <span className="rounded border border-line bg-surface-2/60 px-2 py-0.5 font-mono text-[11px] text-muted">
              {data.calls} 步 / {data.sampled_tasks} 个任务
            </span>
            {data.escalations > 0 && (
              <span className="rounded border border-warn/40 bg-warn/10 px-2 py-0.5 font-mono text-[11px] text-warn">
                提档重试 {data.escalations} 次
              </span>
            )}
          </div>

          {data.sampled_tasks === 0 ? (
            <p className="text-[11.5px] leading-relaxed text-faint">
              还没有开着 model_routing 跑过的任务。先去上面的特性开关打开它，跑几个真实任务再回来看——
              这个面板不会替你决定，只把依据摆出来。
            </p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-line">
              <table className="w-full text-left text-[11.5px]">
                <thead className="bg-surface-2/60 text-faint">
                  <tr>
                    <th className="px-2 py-1.5 font-normal">任务</th>
                    <th className="px-2 py-1.5 font-normal">结果</th>
                    <th className="px-2 py-1.5 font-normal">步数</th>
                    <th className="px-2 py-1.5 font-normal">切档</th>
                    <th className="px-2 py-1.5 font-normal">省下</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.slice(0, 8).map((row) => (
                    <tr key={row.task_id} className="border-t border-line/60">
                      <td className="px-2 py-1.5 font-mono text-[10.5px] text-muted">
                        {row.task_id.slice(0, 8)}
                      </td>
                      <td className="px-2 py-1.5 text-muted">{row.status}</td>
                      <td className="px-2 py-1.5 font-mono text-muted">{row.calls}</td>
                      <td className="px-2 py-1.5 font-mono text-muted">{row.switched}</td>
                      <td className="px-2 py-1.5 font-mono text-muted">{percent(row.saved_ratio)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
