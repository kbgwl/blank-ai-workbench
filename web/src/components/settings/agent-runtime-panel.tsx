"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import {
  errorMessage,
  fetchAgentSettings,
  resetAgentSettings,
  updateAgentSettings,
  type AgentSettings,
} from "./api";

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

  // token 触发刷新，避免在 effect 体内同步 setState（与 feature-flags-panel 一致）。
  useEffect(() => {
    let alive = true;
    fetchAgentSettings()
      .then((next) => {
        if (!alive) return;
        setSettings(next);
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
      setNotice(message);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      setSettings(await resetAgentSettings());
      setNotice("已恢复默认值");
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
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
        <label className="flex flex-col gap-1 text-[11px] text-muted">
          <select
            value={settings.permissions.default_mode}
            disabled={busy}
            onChange={(event) =>
              void apply({ permissions: { default_mode: event.target.value } }, "默认档位已更新")
            }
            className="rounded-md border border-line bg-surface px-2 py-1.5 text-[12px] text-text outline-none"
          >
            {(limits.permission_modes ?? ["default", "accept_edits", "plan", "bypass"]).map((mode) => (
              <option key={mode} value={mode}>
                {limits.permission_mode_labels?.[mode] ?? mode}
              </option>
            ))}
          </select>
        </label>
        <p className="mt-2 text-[11px] leading-relaxed text-faint">
          档位只决定「要不要问」，不决定「能不能做」：不可逆命令在任何档位下都必须人工确认。
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
