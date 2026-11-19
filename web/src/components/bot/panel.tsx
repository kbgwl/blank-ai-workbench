"use client";

import { useState } from "react";
import type { AcceptanceItem, ChangeItem, TestInfo, UsageInfo } from "../tasks/api";
import { formatDuration } from "../tasks/format";
import { TEST_LABEL, testState } from "../evidence/format";
import { activityRows, TONE_CLASS } from "./panel-activity";
import { RailEmpty } from "../rail-empty";
import type { BotStep } from "./turns";
import { turnEvidence } from "./turns";

/**
 * 空态用的小图标：只画轮廓，不填色。
 *
 * 不引图标库：这个仓库至今零图标依赖，为了四个空态拉一个包不划算，
 * 也让首屏多一份要加载的代码。
 */
function RailIcon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

function DiffText({ diff }: { diff: string }) {
  const lines = diff.split("\n").slice(0, 400);
  return (
    <pre className="max-h-80 overflow-auto rounded-md border border-line bg-surface font-mono text-[12px] leading-[1.55]">
      {lines.map((line, index) => {
        const tone = line.startsWith("+++") || line.startsWith("---")
          ? "text-faint"
          : line.startsWith("@@")
            ? "text-brand bg-brand/[0.05]"
            : line.startsWith("+")
              ? "text-accent bg-accent/[0.06]"
              : line.startsWith("-")
                ? "text-danger bg-danger/[0.05]"
                : "text-muted";
        return (
          <div key={index} className={`px-2 whitespace-pre-wrap ${tone}`}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

const CHANGE_TONE: Record<string, string> = {
  created: "text-accent",
  modified: "text-warn",
  deleted: "text-danger",
};
const CHANGE_LABEL: Record<string, string> = { created: "新增", modified: "修改", deleted: "删除" };

/**
 * 同一个文件改多次只留一行。
 *
 * 后端的改动记录按"每次写入"记一条，一个文件被改四轮就有四条。把四条原样铺出来，
 * 右键面板显示的数是 11，而对话里依据条按去重算是 6——两处数字对不上，用户没有理由
 * 相信任何一个；更实际的问题是同一个文件名重复四遍，点哪一条都像是点错了。
 */
function uniqueChanges(rows: ChangeItem[]): ChangeItem[] {
  const merged = new Map<string, ChangeItem>();
  for (const row of rows) {
    const existing = merged.get(row.path);
    if (!existing) {
      merged.set(row.path, row);
      continue;
    }
    // 先建的记录说 created，后面又改过：对用户来说它就是"这一轮动过的文件"。
    // 差异保留第一次出现的类型（新增仍是新增），diff 取第一个非空的。
    merged.set(row.path, { ...existing, diff: existing.diff || row.diff });
  }
  return [...merged.values()];
}

export type TaskPanelProps = {
  title: string;
  status: string;
  running: boolean;
  steps: BotStep[];
  changes: ChangeItem[];
  usage: UsageInfo | null;
  /** 验收条件与测试结论。缺省表示这一轮没有证据（老任务、或还没跑到验证）。 */
  acceptance?: AcceptanceItem[];
  test?: TestInfo | null;
  onClose: () => void;
};

type PanelTab = "changes" | "evidence" | "activity";

/**
 * 右侧面板：变更 / 证据 / 活动，三件事分三栏看。
 *
 * 合在一起时最长的那一栏（改动文件）会把另外两项挤到看不见，用户于是永远只看到
 * "改了什么"，看不到"凭什么说改对了"。分栏之后每一项都有固定入口。
 *
 * 概览那四格留在栏外：它回答"这一轮花了多少"，三个栏都要用到。
 */
export function TaskPanel(props: TaskPanelProps) {
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [tab, setTab] = useState<PanelTab>("changes");
  // 和对话里那条依据条用同一个函数算。两处数字必须同源——左边说改了 3 个文件、
  // 右边说 4 个，用户就没有理由相信任何一个。耗时优先用模型侧统计，缺了回落到
  // 工具耗时之和（离线跑、老事件里没有 usage 时就是这种情形）。
  const evidence = turnEvidence(props.steps);
  const files = props.changes.length > 0
    ? uniqueChanges(props.changes)
    : props.steps
        .filter((step): step is Extract<BotStep, { kind: "change" }> => step.kind === "change")
        .reduce<ChangeItem[]>((rows, step) => {
          if (!rows.some((row) => row.path === step.path)) rows.push({ path: step.path, diff: "", changeType: step.changeType });
          return rows;
        }, []);
  const latency = props.usage?.latencyMs || evidence.durationMs;

  const acceptance = props.acceptance ?? [];
  const testInfo = props.test ?? null;
  const testRow = testState(testInfo);
  const activity = activityRows(props.steps);
  // 计数写在标签上：不点开也知道哪一栏有东西，避免"点进去才发现是空的"。
  const tabs: { key: PanelTab; label: string; count: number }[] = [
    { key: "changes", label: "变更", count: files.length },
    { key: "evidence", label: "证据", count: acceptance.length + (testRow === "none" ? 0 : 1) },
    { key: "activity", label: "活动", count: activity.length },
  ];

  return (
    <aside className="flex w-[320px] shrink-0 flex-col border-l border-line bg-canvas">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[13px] font-medium">最新一轮</span>
        <span
          className={`rounded-full px-1.5 py-0.5 text-[11px] font-medium ${
            props.status === "waiting_approval"
              ? "bg-warn/[0.12] text-warn"
              : props.running
                ? "bg-brand/[0.08] text-brand"
                : props.status === "failed"
                  ? "bg-danger/[0.08] text-danger"
                  : "bg-surface-2 text-muted"
          }`}
        >
          {/* 等审批也要如实说：显示"进行中"会让人以为它在自己往下走，
              实际是卡在一个等他点的按钮上。 */}
          {props.status === "waiting_approval"
            ? "等你批准"
            : props.status === "paused"
              ? "已暂停"
              : props.running
                ? "进行中"
                : props.status === "failed"
                  ? "失败"
                  : props.status === "cancelled"
                    ? "已停止"
                    : "完成"}
        </span>
        <button
          type="button"
          onClick={props.onClose}
          title="收起面板"
          className="ml-auto grid size-6 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} className="size-3.5" aria-hidden>
            <path d="m14 6-6 6 6 6" />
          </svg>
        </button>
      </header>

      <div role="tablist" aria-label="本轮详情" className="flex shrink-0 items-center gap-0.5 border-b border-line px-2 py-1.5">
        {tabs.map((item) => {
          const active = tab === item.key;
          return (
            <button
              key={item.key}
              type="button"
              aria-selected={active}
              role="tab"
              onClick={() => setTab(item.key)}
              className={`flex items-center gap-1 rounded-md px-2 py-1 text-[12px] transition-colors ${
                active ? "bg-surface-3 font-medium text-text" : "text-muted hover:bg-surface-2 hover:text-text"
              }`}
            >
              {item.label}
              <span className="font-mono text-[9px] text-faint">{item.count}</span>
            </button>
          );
        })}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {/* 用 flex 列而不是 space-y：空态要能 flex-1 撑满剩余高度并垂直居中。
            贴在顶部时整栏下半截是白的，看着像只加载了一半。 */}
        <div className="flex min-h-full flex-col gap-4">
        <section>
          {/* 这一轮的标题不再抄一遍：顶栏已经是它，左栏也是它，右栏再来第三次
              只会让人怀疑三处说的是不是同一件事。这里直接从账本开始。 */}
          {/* 这一轮的账本收成一行。
              原来四个 8px 小标签各自一块灰底格子，占掉右栏最上面 90px，
              而右栏其余部分是空的——四个"0"反而成了这一栏最显眼的东西。
              一行读完：用了什么、动了什么、花了多久。 */}
          <dl className="flex flex-wrap items-baseline gap-x-3.5 gap-y-1">
            <div className="flex items-baseline gap-1">
              <dd className="font-mono text-[12px] text-text">{evidence.calls}</dd>
              <dt className="font-mono text-[11px] text-faint">工具</dt>
              {evidence.failed > 0 && (
                <span className="font-mono text-[11px] text-danger">({evidence.failed} 失败)</span>
              )}
            </div>
            <div className="flex items-baseline gap-1">
              <dd className="font-mono text-[12px] text-text">{files.length}</dd>
              <dt className="font-mono text-[11px] text-faint">文件</dt>
              {evidence.commandCount > 0 && (
                <span className="font-mono text-[11px] text-faint">/ {evidence.commandCount} 命令</span>
              )}
            </div>
            <div className="flex items-baseline gap-1">
              <dd className="font-mono text-[12px] text-text">{latency > 0 ? formatDuration(latency) : "—"}</dd>
              <dt className="font-mono text-[11px] text-faint">耗时</dt>
            </div>
            <div className="flex items-baseline gap-1">
              <dd className="font-mono text-[12px] text-text">
                {props.usage?.totalTokens ? props.usage.totalTokens.toLocaleString() : "—"}
              </dd>
              <dt className="font-mono text-[11px] text-faint">token</dt>
            </div>
          </dl>
        </section>

        {tab === "changes" &&
          (files.length === 0 ? (
            // 空态不再是一句「这一轮没有改文件。」：那句话和加载失败长得一样，
            // 也说不清这一栏本来会出现什么。
            <RailEmpty
              compact
              fill
              icon={<RailIcon d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6" />}
              title="这一轮还没有改动文件"
              body="Agent 改文件后，这里会按文件列出路径、改动类型和 diff。"
            />
          ) : (
            <section>
              <p className="mb-1.5 font-mono text-[11px] tracking-wide text-faint uppercase">改动的文件</p>
              <div className="space-y-1">
                {files.map((file) => {
                  const open = openPath === file.path;
                  return (
                    <div key={file.path} className="overflow-hidden rounded-lg border border-line bg-surface">
                      {/* 没有 diff 也要能点开：以前这里 disabled，行看着是按钮，
                          点下去一点反应都没有，用户只会怀疑整个面板坏了。
                          现在点开至少说明"这一轮没有记录 diff"。 */}
                      <button
                        type="button"
                        aria-expanded={open}
                        onClick={() => setOpenPath(open ? null : file.path)}
                        title={file.diff ? "展开看改动" : "这一轮没有记录 diff"}
                        className="flex w-full items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
                      >
                        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-text" title={file.path}>
                          {file.path}
                        </span>
                        <span className={`shrink-0 font-mono text-[9px] ${CHANGE_TONE[file.changeType] ?? "text-muted"}`}>
                          {CHANGE_LABEL[file.changeType] ?? file.changeType}
                        </span>
                      </button>
                      {open && (
                        <div className="border-t border-line p-1.5">
                          {file.diff ? (
                            <DiffText diff={file.diff} />
                          ) : (
                            <p className="px-1 py-1 text-[12px] text-faint">
                              这一轮只记录了文件路径，没有留下 diff（历史轮次或改动未落盘）。
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          ))}

        {tab === "evidence" &&
          (acceptance.length === 0 && testRow === "none" ? (
            // 两项都空时合成一块空态：原来会连着摆两句「没有验收条件」「没有跑测试」，
            // 读起来像两次报错。
            <RailEmpty
              compact
              fill
              icon={<RailIcon d="M9 11l2.5 2.5L16 8M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6z" />}
              title="这一轮还没有证据"
              body="验收条件和测试结论会出现在这里，回答「凭什么说改对了」。"
            />
          ) : (
            <section className="space-y-3">
              <div>
                <p className="mb-1.5 font-mono text-[11px] tracking-wide text-faint uppercase">验收条件</p>
                {acceptance.length === 0 && <p className="px-1 py-2 text-[12px] text-muted">这一轮没有验收条件。</p>}
                <ul className="space-y-1">
                  {acceptance.map((item, index) => (
                    <li key={index} className="flex items-start gap-1.5 text-[12px] leading-relaxed">
                      <span
                        className={
                          item.covered === true ? "text-accent" : item.covered === false ? "text-danger" : "text-faint"
                        }
                        title={item.covered === true ? "已覆盖" : item.covered === false ? "没覆盖" : "未判定"}
                      >
                        {item.covered === true ? "✓" : item.covered === false ? "✗" : "?"}
                      </span>
                      <span className="min-w-0 flex-1 text-muted">
                        {item.criterion}
                        {item.note && <span className="text-faint"> · {item.note}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>

              <div>
                <p className="mb-1.5 font-mono text-[11px] tracking-wide text-faint uppercase">测试</p>
                {testRow === "none" ? (
                  <p className="px-1 py-2 text-[12px] text-muted">这一轮没有跑测试。</p>
                ) : (
                  <div className="rounded-lg border border-line bg-surface px-2 py-1.5">
                    <p
                      className={`text-[12px] ${
                        testRow === "passed" ? "text-accent" : testRow === "failed" ? "text-danger" : "text-muted"
                      }`}
                    >
                      {testInfo?.skippedReason
                        ? `${TEST_LABEL[testRow]}：${testInfo.skippedReason}`
                        : TEST_LABEL[testRow]}
                    </p>
                    {testInfo?.command && (
                      <p className="mt-1 truncate font-mono text-[11px] text-faint" title={testInfo.command}>
                        $ {testInfo.command}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </section>
          ))}

        {tab === "activity" &&
          (activity.length === 0 ? (
            // 正在跑却一条都没有，是「刚开始」而不是「没干活」——两种情形标题不同，
            // 否则用户会以为这一轮什么都没做。
            <RailEmpty
              compact
              fill
              icon={<RailIcon d="M4 6h16M4 12h16M4 18h10" />}
              title={props.running ? "正在开始" : "这一轮还没有记录"}
              body={
                props.running
                  ? "它刚开始动，第一条工具调用出现后就会记在这里。"
                  : "每一次工具调用和判定都会按顺序记在这里。"
              }
            />
          ) : (
            <section>
              <p className="mb-1.5 font-mono text-[11px] tracking-wide text-faint uppercase">这一轮做了什么</p>
              <ol className="space-y-1">
                {activity.map((row) => (
                  <li key={row.key} className="flex items-start gap-2">
                    <span className={`mt-1 size-1.5 shrink-0 rounded-full ${TONE_CLASS[row.tone]}`} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12px] text-text" title={row.label}>
                        {row.label}
                      </span>
                      {row.detail && (
                        <span className="block truncate font-mono text-[11px] text-faint" title={row.detail}>
                          {row.detail}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          ))}
        </div>
      </div>
    </aside>
  );
}
