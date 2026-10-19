"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { Toggle } from "./bits";
import {
  errorMessage,
  fetchAgentSettings,
  fetchSafetyRuntime,
  updateAgentSettings,
  type AgentSettings,
  type SafetyRuntime,
} from "./api";

/**
 * 「安全与权限」面板。
 *
 * 客户要的不是一个 on/off，而是能说清「谁允许了什么」：
 * 先看这台机器是怎么隔离的（容器 / 本机受限），再逐项决定能力边界，
 * 最后才是审批节奏。顺序反过来，用户会先关掉确认，再发现命令根本跑不了。
 */

const ABSOLUTE_HINT = "需要绝对路径，例如 E:\\projects\\my-app";

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <div className="min-w-0">
        <div className="text-[12.5px] text-text">{label}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-faint">{hint}</div>}
      </div>
      <div className="shrink-0 pt-0.5">{children}</div>
    </div>
  );
}

function ListEditor({
  items,
  placeholder,
  emptyText,
  disabled,
  validate,
  onAdd,
  onRemove,
}: {
  items: string[];
  placeholder: string;
  emptyText: string;
  disabled: boolean;
  validate?: (value: string) => string;
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState("");

  function submit() {
    const value = draft.trim();
    if (!value) return;
    const issue = validate?.(value) ?? "";
    if (issue) {
      setProblem(issue);
      return;
    }
    if (items.includes(value)) {
      setProblem("已经在列表里了");
      return;
    }
    setProblem("");
    setDraft("");
    onAdd(value);
  }

  return (
    <div className="flex flex-col gap-2">
      {items.length === 0 ? (
        <p className="rounded-md border border-dashed border-line bg-surface-2/40 px-2.5 py-2 text-[11.5px] text-faint">
          {emptyText}
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {items.map((item) => (
            <li
              key={item}
              className="flex items-center justify-between gap-2 rounded-md border border-line bg-surface-2/50 px-2 py-1.5"
            >
              <code className="min-w-0 truncate font-mono text-[11.5px] text-text" title={item}>
                {item}
              </code>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onRemove(item)}
                className="shrink-0 rounded px-1.5 text-[11px] text-faint transition-colors hover:text-danger disabled:opacity-50"
                aria-label={`移除 ${item}`}
              >
                移除
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-1.5">
        <input
          value={draft}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(event) => {
            setDraft(event.target.value);
            setProblem("");
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
          className="min-w-0 flex-1 rounded-md border border-line bg-surface px-2 py-1 font-mono text-[11.5px] text-text outline-none focus:border-brand/50"
        />
        <button
          type="button"
          disabled={disabled || !draft.trim()}
          onClick={submit}
          className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-50"
        >
          添加
        </button>
      </div>
      {problem && <p className="text-[11px] text-danger">{problem}</p>}
    </div>
  );
}

function looksAbsolute(value: string): boolean {
  return /^[A-Za-z]:[\\/]/.test(value) || value.startsWith("\\\\") || value.startsWith("/");
}

export function SafetyPanel() {
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [runtime, setRuntime] = useState<SafetyRuntime | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [timeoutDraft, setTimeoutDraft] = useState("");
  const [reloadToken, setReloadToken] = useState(0);

  // 沿用 feature-flags-panel 的写法：用 token 触发刷新，避免在 effect 体内同步 setState。
  useEffect(() => {
    let alive = true;
    Promise.all([fetchAgentSettings(), fetchSafetyRuntime()])
      .then(([next, info]) => {
        if (!alive) return;
        setSettings(next);
        setRuntime(info);
        setTimeoutDraft(String(next.safety.timeout_seconds));
        setError("");
      })
      .catch((err: unknown) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  async function apply(
    patch: Parameters<typeof updateAgentSettings>[0],
    message: string,
  ) {
    setBusy(true);
    setNotice("");
    try {
      const next = await updateAgentSettings(patch);
      setSettings(next);
      setTimeoutDraft(String(next.safety.timeout_seconds));
      setRuntime(await fetchSafetyRuntime());
      setNotice(message);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (!settings) {
    return (
      <Panel title="安全与权限" hint="GET /settings/safety/runtime">
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

  const safety = settings.safety;
  const limits = settings.limits?.safety_timeout ?? { min: 10, max: 600, default: 120 };
  const backend = runtime?.backend;

  function commitTimeout() {
    const value = Number(timeoutDraft);
    if (!Number.isFinite(value) || value <= 0) {
      setTimeoutDraft(String(safety.timeout_seconds));
      return;
    }
    if (value === safety.timeout_seconds) return;
    void apply({ safety: { timeout_seconds: value } }, `单条命令上限已设为 ${Math.round(value)} 秒`);
  }

  return (
    <div className="flex flex-col gap-4">
      {/* 先说清「现在到底怎么隔离的」：含糊地写"已开启沙箱"会让客户以为跑不可信代码是安全的 */}
      <div className="flex flex-col gap-2 rounded-lg border border-line bg-surface px-3.5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`rounded-md border px-2 py-0.5 font-mono text-[10.5px] ${
              backend?.isolation === "container"
                ? "border-ok/40 bg-ok-soft text-ok"
                : "border-warn/40 bg-warn-soft text-warn"
            }`}
          >
            {backend?.label ?? "检测中"}
          </span>
          <span className="font-mono text-[10.5px] text-faint">
            {backend?.platform === "nt" ? "Windows" : backend?.platform}
          </span>
          {backend && (
            <span className="font-mono text-[10.5px] text-faint">
              Docker {backend.docker_available ? "可用" : "未安装"}
            </span>
          )}
          <span className="ml-auto flex items-center gap-1.5 font-mono text-[10.5px] text-faint">
            执行能力
            <span className={safety.shell_enabled ? "text-ok" : "text-danger"}>
              {safety.shell_enabled ? "已允许" : "已禁止"}
            </span>
          </span>
        </div>
        <p className="text-[11.5px] leading-relaxed text-muted">
          {backend?.detail ??
            "命令执行的隔离方式取决于本机是否有可用的容器运行时；策略在所有情况下都生效。"}
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="能力开关" hint={`${settings.path} · 下一个任务生效`}>
          <Row
            label="允许执行命令"
            hint="关着时 Agent 读写文件照常，但 run_shell / run_tests 会被直接拒绝，审批也放不过去"
          >
            <Toggle
              checked={safety.shell_enabled}
              disabled={busy}
              label={safety.shell_enabled ? "允许" : "禁止"}
              onChange={(next) =>
                void apply(
                  { safety: { shell_enabled: next } },
                  next ? "已允许执行命令" : "已禁止执行命令",
                )
              }
            />
          </Row>
          <div className={safety.shell_enabled ? "" : "pointer-events-none opacity-45"}>
            <Row
              label="允许联网"
              hint="下载、git clone、pip / npm install 这类会出网的命令。关着时一律拒绝"
            >
              <Toggle
                checked={safety.network_enabled}
                disabled={busy || !safety.shell_enabled}
                label={safety.network_enabled ? "允许" : "禁止"}
                onChange={(next) =>
                  void apply(
                    { safety: { network_enabled: next } },
                    next ? "已允许联网命令" : "已禁止联网命令",
                  )
                }
              />
            </Row>
            <Row
              label="每条命令都先确认"
              hint="关掉后低风险命令直接执行，只有高风险与不可逆操作才停下来问你"
            >
              <Toggle
                checked={safety.confirm_all_commands}
                disabled={busy || !safety.shell_enabled}
                label={safety.confirm_all_commands ? "逐条确认" : "自动放行"}
                onChange={(next) =>
                  void apply(
                    { safety: { confirm_all_commands: next } },
                    next ? "已开启逐条确认" : "低风险命令将自动放行",
                  )
                }
              />
            </Row>
            <Row
              label="仅限工作区"
              hint="命令里出现工作区以外的真实路径就拒绝。关掉后 Agent 可以触达整台机器"
            >
              <Toggle
                checked={safety.restrict_to_workspace}
                disabled={busy || !safety.shell_enabled}
                label={safety.restrict_to_workspace ? "仅工作区" : "整台机器"}
                onChange={(next) =>
                  void apply(
                    { safety: { restrict_to_workspace: next } },
                    next ? "命令将限制在工作区内" : "已放开到整台机器",
                  )
                }
              />
            </Row>
            <Row label="单条命令上限" hint={`${limits.min}–${limits.max} 秒，超出后回收整棵进程树`}>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  min={limits.min}
                  max={limits.max}
                  step={10}
                  value={timeoutDraft}
                  disabled={busy || !safety.shell_enabled}
                  onChange={(event) => setTimeoutDraft(event.target.value)}
                  onBlur={commitTimeout}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") event.currentTarget.blur();
                  }}
                  className="w-20 rounded-md border border-line bg-surface px-2 py-1 text-right font-mono text-[12px] text-text outline-none focus:border-brand/50"
                />
                <span className="text-[11px] text-faint">秒</span>
              </div>
            </Row>
          </div>
          {!safety.shell_enabled && (
            <p className="mt-1 rounded-md border border-line bg-surface-2/50 px-2.5 py-2 text-[11px] leading-relaxed text-faint">
              默认是禁止的：装好就能跑命令的产品，第一次误删就没人敢用了。需要 Agent 自己跑测试或构建时再打开。
            </p>
          )}
        </Panel>

        <Panel title="路径与命令边界" hint="白名单外的一律拒绝，与审批无关">
          <div className="flex flex-col gap-4">
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[12.5px] text-text">额外允许的目录</span>
                <span className="font-mono text-[10px] text-faint">
                  {safety.allowed_roots.length} 项
                </span>
              </div>
              <p className="mb-2 text-[11px] leading-relaxed text-faint">
                工作区自动包含。只有这里列出的目录，命令才允许读写；其余真实路径一律拒绝。
              </p>
              <ListEditor
                items={safety.allowed_roots}
                placeholder={ABSOLUTE_HINT}
                emptyText="还没有额外目录。Agent 只能碰当前工作区。"
                disabled={busy}
                validate={(value) => (looksAbsolute(value) ? "" : ABSOLUTE_HINT)}
                onAdd={(value) =>
                  void apply(
                    { safety: { allowed_roots: [...safety.allowed_roots, value] } },
                    `已允许访问 ${value}`,
                  )
                }
                onRemove={(value) =>
                  void apply(
                    { safety: { allowed_roots: safety.allowed_roots.filter((item) => item !== value) } },
                    `已移除 ${value}`,
                  )
                }
              />
            </div>

            <div className="border-t border-line/70 pt-3">
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[12.5px] text-text">禁止的命令前缀</span>
                <span className="font-mono text-[10px] text-faint">
                  {safety.denied_commands.length} 项
                </span>
              </div>
              <p className="mb-2 text-[11px] leading-relaxed text-faint">
                按前缀匹配，命中即拒绝，例：<code className="font-mono text-[10.5px]">{'git push'}</code>。
                格式化磁盘、关机、改系统注册表这类内置禁止项不受此处影响，永远拒绝。
              </p>
              <ListEditor
                items={safety.denied_commands}
                placeholder="例如 git push"
                emptyText="还没有自定义禁止项。"
                disabled={busy}
                onAdd={(value) =>
                  void apply(
                    { safety: { denied_commands: [...safety.denied_commands, value] } },
                    `已禁止 ${value}`,
                  )
                }
                onRemove={(value) =>
                  void apply(
                    {
                      safety: {
                        denied_commands: safety.denied_commands.filter((item) => item !== value),
                      },
                    },
                    `已解除对 ${value} 的禁止`,
                  )
                }
              />
            </div>
          </div>
        </Panel>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
        <span className="text-[11px] text-faint">
          {notice || error || "能力边界决定能不能做，权限档位决定要不要问 —— 两者必须都放行才会执行。"}
        </span>
        <span className="font-mono text-[10.5px] text-faint">
          默认档位 · {runtime?.default_mode ?? settings.permissions.default_mode}
        </span>
      </div>
      {error && <p className="text-[11px] text-danger">{error}</p>}
    </div>
  );
}
