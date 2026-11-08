"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import {
  errorMessage,
  fetchAgentSettings,
  resetAgentSettings,
  updateAgentSettings,
  type AgentSettings,
} from "./api";

/** 后端没给 labels 时的兜底文案（v165 §8）。 */
const AUTOMATION_LABELS: Record<string, string> = {
  manual: "手动：我点执行才动",
  assist: "辅助（默认）：低风险自动做",
  autonomous: "自动驾驶：连续执行与自动修复",
};

/**
 * 通知策略的四种口径（对应 agent/notify.py 的 NOTIFY_*）。
 *
 * 文案按「用户会不会被打扰」写，而不是按枚举名直译——`approval_only` 译成
 * 「只在等你批准时」比「仅审批」更容易判断自己要不要开。
 */
const NOTIFY_LABELS: Record<string, { label: string; detail: string }> = {
  silent: {
    label: "安静：只报审批",
    detail: "除了「任务停在这里等你批准」，什么都不弹。审批是唯一压不得的那类。",
  },
  milestone: {
    label: "里程碑：失败和交付物",
    detail: "任务失败、跑完有结果时才弹；中间进度不打扰。桌面版平时用这个就够。",
  },
  approval_only: {
    label: "只报审批（默认）",
    detail: "后台任务默认不打扰，只有需要你点批准时才弹出来。",
  },
  always: {
    label: "全都说：含中间进度",
    detail: "每一步都通知。长任务里会持续弹窗，通常是调试时才开。",
  },
};

const GUARDIAN_MODES: { value: string; label: string; detail: string }[] = [
  {
    value: "off",
    label: "关闭",
    detail: "高风险操作直接问你，不额外调用审查模型。默认：不加延迟、不替你做判断。",
  },
  {
    value: "veto",
    label: "只收紧",
    detail: "审查模型可以否掉明显危险的动作（删库、密钥外泄、force push），但永远不能替你放行。",
  },
  {
    value: "auto",
    label: "可代批",
    detail: "可回滚的动作在审查通过后自动放行；不可逆命令仍然必须由你确认，模型说了不算。",
  },
];

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 py-2">
      <div className="min-w-0">
        <div className="text-[12.5px] text-text">{label}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-faint">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

export function AgentRuntimePanel() {
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);
  // 免打扰时段是自由文本，逐字提交会把「22:0」这种半成品写进设置文件，
  // 所以这里留草稿，失焦或按回车才真的保存。
  const [quietDraft, setQuietDraft] = useState("");

  // token 触发刷新，避免在 effect 体内同步 setState（与 feature-flags-panel 一致）。
  useEffect(() => {
    let alive = true;
    fetchAgentSettings()
      .then((next) => {
        if (!alive) return;
        setSettings(next);
        setQuietDraft(next.background?.quiet_hours ?? "");
        setError("");
      })
      .catch((err: unknown) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  async function apply(patch: Parameters<typeof updateAgentSettings>[0], message: string) {
    setBusy(true);
    setNotice("");
    try {
      const next = await updateAgentSettings(patch);
      setSettings(next);
      // 免打扰是文本输入：回读服务端归一化之后的值，用户才能看见「22:0-8」
      // 这种半成品到底被接受了还是被清掉了。
      if (patch.background?.quiet_hours !== undefined) {
        setQuietDraft(next.background?.quiet_hours ?? "");
      }
      const expected = patch.permissions?.default_mode;
      const expectedMode = patch.background?.automation_mode;
      // 以前不管服务端回了什么都提示「已更新」：档位被静默回落到 default 时，
      // 用户以为开了全放行，实际上每条命令还在弹审批（v165 P0）。
      // 自动化模式同样要回显校验——「选了自动驾驶却仍在手动」是同一类故障。
      const badMode = expected && next.permissions.default_mode !== expected;
      const badAutomation = expectedMode && next.background?.automation_mode !== expectedMode;
      if (badMode || badAutomation) {
        const actual = badMode ? next.permissions.default_mode : next.background?.automation_mode;
        setError(`设置未生效：服务端仍是「${actual ?? "未返回"}」，请检查设置文件是否可写`);
        setNotice("");
      } else {
        setNotice(message);
        setError("");
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      const next = await resetAgentSettings();
      setSettings(next);
      setQuietDraft(next.background?.quiet_hours ?? "");
      setNotice("已恢复默认值");
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  /**
   * 保存免打扰时段。
   *
   * 空串是合法值（= 不设免打扰），所以不能拿 falsy 当"没填"。格式对不对由后端
   * `_clean_quiet_hours` 判，坏值会被清成空串——这里按服务端回读的结果同步草稿，
   * 用户能立刻看见自己那句话有没有被接受。
   */
  function saveQuietHours() {
    const current = settings?.background?.quiet_hours ?? "";
    const next = quietDraft.trim();
    if (next === current) return;
    void apply(
      { background: { quiet_hours: next } },
      next ? "免打扰时段已更新" : "免打扰已关闭",
    );
  }

  if (!settings) {
    return (
      <Panel title="执行与智能体" hint="GET /settings/agent">
        <p className="text-[12px] text-faint">{error || "加载中…"}</p>
        {error && (
          <button
            type="button"
            onClick={() => setReloadToken((token) => token + 1)}
            className="mt-2 self-start rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text"
          >
            重试
          </button>
        )}
      </Panel>
    );
  }

  const guardian = settings.guardian;
  const compaction = settings.compaction;
  const limits = settings.limits || {};
  const ratioLimits = limits.trigger_ratio ?? { min: 0.5, max: 0.95, default: 0.82 };
  const reserveLimits = limits.reserve_tokens ?? { min: 1024, max: 131072, default: 16384 };
  const timeoutLimits = limits.guardian_timeout ?? { min: 1, max: 60, default: 8 };

  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Panel title="独立审查（guardian）" hint={`POST /settings/agent · ${settings.path}`}>
        <div className="flex flex-col gap-1.5">
          {GUARDIAN_MODES.map((mode) => {
            const active = guardian.mode === mode.value;
            return (
              <button
                key={mode.value}
                type="button"
                disabled={busy}
                onClick={() =>
                  void apply({ guardian: { mode: mode.value } }, `审查模式已设为「${mode.label}」`)
                }
                className={`rounded-md border px-3 py-2 text-left transition ${
                  active
                    ? "border-brand/60 bg-brand/10"
                    : "border-line bg-surface/40 hover:border-line-strong"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-[12.5px] text-text">{mode.label}</span>
                  {active && <span className="font-mono text-[10px] text-brand">当前</span>}
                </div>
                <p className="mt-1 text-[11px] leading-relaxed text-muted">{mode.detail}</p>
              </button>
            );
          })}
        </div>
        <Row
          label="审查超时"
          hint="模型在这么久没回话就放弃本次审查，照常走人工审批（失败不会卡住任务）"
        >
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={timeoutLimits.min}
              max={timeoutLimits.max}
              step={1}
              value={guardian.timeout_seconds}
              disabled={busy}
              onChange={(event) =>
                void apply({ guardian: { timeout_seconds: Number(event.target.value) } }, "超时已更新")
              }
              className="w-20 rounded-md border border-line bg-surface px-2 py-1 text-right font-mono text-[12px] text-text outline-none"
            />
            <span className="text-[11px] text-faint">秒</span>
          </div>
        </Row>
        {guardian.mode === "auto" && (
          <p className="mt-1 rounded-md border border-warn/40 bg-warn/10 px-2.5 py-2 text-[11px] leading-relaxed text-warn">
            可代批只在「可回滚」的动作上生效：删除、force push、读写密钥这类不可逆操作一律回到你面前。
          </p>
        )}
      </Panel>

      <Panel title="上下文水位" hint="按模型窗口算，不用字符数猜">
        <Row label="触发压缩的位置" hint="上下文用到这个比例就先做一层裁剪，再决定要不要总结">
          <div className="flex items-center gap-2">
            <input
              type="range"
              min={ratioLimits.min}
              max={ratioLimits.max}
              step={0.01}
              value={compaction.trigger_ratio}
              disabled={busy}
              onChange={(event) =>
                void apply({ compaction: { trigger_ratio: Number(event.target.value) } }, "水位已更新")
              }
              className="w-32 accent-[var(--brand)]"
            />
            <span className="w-10 text-right font-mono text-[12px] text-text">
              {Math.round(compaction.trigger_ratio * 100)}%
            </span>
          </div>
        </Row>
        <Row
          label="给回复预留"
          hint="窗口减掉这块余量是硬上限：必须留够写总结和收尾的 token"
        >
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={reserveLimits.min}
              max={reserveLimits.max}
              step={1024}
              value={compaction.reserve_tokens}
              disabled={busy}
              onChange={(event) =>
                void apply({ compaction: { reserve_tokens: Number(event.target.value) } }, "预留已更新")
              }
              className="w-24 rounded-md border border-line bg-surface px-2 py-1 text-right font-mono text-[12px] text-text outline-none"
            />
            <span className="text-[11px] text-faint">tokens</span>
          </div>
        </Row>
        <p className="mt-1 text-[11px] leading-relaxed text-faint">
          水位以模型回报的真实 prompt_tokens 校准：32k 窗口和 200k 窗口用的是同一套规则，只是位置不同。
          所有数值保存后对下一个任务生效，正在跑的任务不受影响。
        </p>
      </Panel>

      <Panel title="新任务的默认权限档位" hint="任务里仍可随时切换">
        <div className="flex flex-col gap-1 text-[11px] text-muted">
          <SelectPopover
            ariaLabel="新任务的默认权限档位"
            value={settings.permissions.default_mode}
            disabled={busy}
            options={(limits.permission_modes ?? ["default", "accept_edits", "plan", "bypass"]).map((mode) => ({
              value: mode,
              label: limits.permission_mode_labels?.[mode] ?? mode,
            }))}
            onChange={(mode) => void apply({ permissions: { default_mode: mode } }, "默认档位已更新")}
            menuMinWidth={220}
          />
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-faint">
          档位只决定「要不要问」，不决定「能不能做」：不可逆命令在任何档位下都必须人工确认。
        </p>
      </Panel>

      <Panel title="自动化模式" hint="v165 §8 · 决定「要不要自己动」">
        <div className="flex flex-col gap-1 text-[11px] text-muted">
          <SelectPopover
            ariaLabel="自动化模式"
            value={settings.background?.automation_mode ?? "assist"}
            disabled={busy}
            options={(limits.automation_modes ?? ["manual", "assist", "autonomous"]).map((mode) => ({
              value: mode,
              label: limits.automation_mode_labels?.[mode] ?? AUTOMATION_LABELS[mode] ?? mode,
            }))}
            onChange={(mode) =>
              void apply(
                { background: { automation_mode: mode as "manual" | "assist" | "autonomous" } },
                "自动化模式已更新",
              )
            }
            menuMinWidth={220}
          />
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-faint">
          这和上面的权限档位是两回事：档位决定「要不要问」，模式决定「要不要自己动」。
          自动驾驶可以连续执行、自动修复失败、后台常驻，但高风险操作仍然必须你审批。
        </p>
      </Panel>

      {/* v0.11.1：通知与免打扰。
          后端的 notify.py 与 quiet_hours 早就在跑（判定写进 notify.decided 事件），
          但设置页一直没有入口——用户想设免打扰只能去手改 settings.json。 */}
      <Panel title="通知与免打扰" hint="什么时候该弹出来打扰你">
        <Row
          label="通知时机"
          hint={NOTIFY_LABELS[settings.background?.notify_policy ?? "approval_only"]?.detail ?? "认不出的策略按「只报审批」处理"}
        >
          <SelectPopover
            ariaLabel="通知时机"
            value={settings.background?.notify_policy ?? "approval_only"}
            disabled={busy}
            options={Object.entries(NOTIFY_LABELS).map(([value, item]) => ({
              value,
              label: item.label,
            }))}
            onChange={(policy) =>
              void apply({ background: { notify_policy: policy } }, "通知时机已更新")
            }
            menuMinWidth={240}
          />
        </Row>
        <Row label="免打扰时段" hint="HH:MM-HH:MM；跨零点也行（22:00-08:00）。留空表示不设">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={quietDraft}
              placeholder="22:00-08:00"
              disabled={busy}
              onChange={(event) => setQuietDraft(event.target.value)}
              onBlur={() => saveQuietHours()}
              onKeyDown={(event) => {
                if (event.key === "Enter") saveQuietHours();
              }}
              className="w-36 rounded-md border border-line bg-surface px-2 py-1 text-center font-mono text-[12px] text-text outline-none"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => saveQuietHours()}
              className="rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:border-line-strong"
            >
              保存
            </button>
          </div>
        </Row>
        <p className="mt-1 text-[11px] leading-relaxed text-faint">
          免打扰只压「做完了 / 有变化了」这类通知。<span className="text-text">审批永远会响</span>
          ——任务停在那儿等你点，压到第二天早上等于白跑一晚。
        </p>
      </Panel>

      <Panel title="工具加载" hint="按需检索，省掉每轮重复的 schema">
        <Row
          label="tool_search 按需加载"
          hint="关闭后所有工具（含 git / 联网 / 子代理）每轮都直接下发给模型"
        >
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void apply({ tools: { search: !settings.tools.search } }, "工具加载策略已更新")
            }
            className={`rounded-md border px-3 py-1.5 text-[11px] transition ${
              settings.tools.search
                ? "border-brand/60 bg-brand/10 text-text"
                : "border-line bg-surface text-muted"
            }`}
          >
            {settings.tools.search ? "已开启" : "已关闭"}
          </button>
        </Row>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 border-t border-line/70 pt-2">
          <span className="text-[11px] text-faint">
            {notice || error || "改动立即写入设置文件，下一个任务生效"}
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={() => void reset()}
            className="rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:border-line-strong"
          >
            恢复默认
          </button>
        </div>
        {error && <p className="mt-1 text-[11px] text-danger">{error}</p>}
      </Panel>
    </div>
  );
}
