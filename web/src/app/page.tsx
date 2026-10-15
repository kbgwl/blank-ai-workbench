import { Panel } from "@/components/panel";
import { LivePanels } from "@/components/overview/live-panels";

const plan = [
  { text: "检查项目结构与登录接口位置", done: true },
  { text: "在 schema 中增加邮箱格式校验", done: true },
  { text: "补充 tests/test_auth.py 用例", done: false },
  { text: "运行 pytest 并修复失败", done: false },
];

const recentTasks: [string, string][] = [
  ["登录接口邮箱校验", "running"],
  ["重构 Tool Gateway", "waiting"],
  ["补齐迁移脚本", "done"],
];

const tools = [
  { name: "read_file", arg: "app/api/auth.py", state: "ok", ms: 12 },
  { name: "apply_patch", arg: "app/api/auth.py · +14 −2", state: "ok", ms: 38 },
  { name: "run_shell", arg: "pytest -q", state: "run", ms: 0 },
];

const terminal = [
  { t: "$", s: "pytest -q", tone: "cmd" },
  { t: "", s: "....F                                      [100%]", tone: "out" },
  { t: "", s: "test_email_rejects_plus_alias FAILED", tone: "err" },
  { t: "", s: "AssertionError: 'a+b@example.com' accepted", tone: "err" },
  { t: "", s: "1 failed, 4 passed in 0.82s", tone: "warn" },
];

const checks = [
  { label: "一条命令启动全栈", ok: true, note: "pnpm dev" },
  { label: "前端访问后端健康检查", ok: true, note: "浏览器实测" },
  { label: "迁移可创建与回滚", ok: true, note: "SQLite + Postgres" },
  { label: "统一错误响应", ok: true, note: "pytest 断言" },
  { label: "类型检查 / Lint / 构建", ok: true, note: "全部通过" },
  { label: "CI 实际运行", ok: false, note: "等待 git 仓库" },
];

export default function Overview() {
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-tight">概览</h1>
          <p className="mt-1 text-[13px] leading-relaxed text-muted">
            描述任务 → Agent 计划 → 修改代码 → 执行测试 → 自动修复 → 查看 Diff → 接受结果
          </p>
        </div>
        <span className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[11px] whitespace-nowrap text-faint">
          项目 / 供应商 / 用量为真实数据
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[240px_minmax(0,1fr)_360px]">
        <div className="flex min-w-0 flex-col gap-4">
          <LivePanels />
          <Panel title="最近任务" hint="示例数据 · v0.4">
            <ul className="flex flex-col gap-1.5">
              {recentTasks.map(([name, st]) => (
                <li
                  key={name}
                  className="flex items-center justify-between rounded-lg border border-line bg-surface/60 px-3 py-2 text-[13px] text-muted"
                >
                  <span className="truncate">{name}</span>
                  <StateDot state={st} />
                </li>
              ))}
            </ul>
          </Panel>
        </div>

        <Panel title="对话与执行过程" hint="示例数据 · v0.4">
          <div className="flex flex-col gap-4">
            <div className="self-end max-w-[80%] rounded-2xl rounded-br-sm bg-brand/15 ring-1 ring-brand/25 px-4 py-2.5 text-[13px]">
              给登录接口增加邮箱格式校验，并补充测试
            </div>

            <div className="rounded-xl border border-line bg-surface/60 p-3">
              <div className="mb-2 font-mono text-[10px] tracking-wide text-faint uppercase">
                执行计划
              </div>
              <ul className="flex flex-col gap-1.5">
                {plan.map((p) => (
                  <li key={p.text} className="flex items-center gap-2 text-[13px]">
                    <span
                      className={`grid size-4 shrink-0 place-items-center rounded-full text-[9px] ${
                        p.done ? "bg-accent/20 text-accent" : "bg-surface-2 text-faint"
                      }`}
                    >
                      {p.done ? "✓" : "·"}
                    </span>
                    <span className={p.done ? "text-muted line-through decoration-line-strong" : "text-text"}>
                      {p.text}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="flex flex-col gap-2">
              {tools.map((t) => (
                <div
                  key={t.name}
                  className="flex items-center gap-3 rounded-lg border border-line bg-surface/60 px-3 py-2"
                >
                  <span className="font-mono text-[11px] text-brand">{t.name}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted">
                    {t.arg}
                  </span>
                  <span className="font-mono text-[10px] text-faint">{t.ms ? `${t.ms}ms` : "…"}</span>
                  <StateDot state={t.state} />
                </div>
              ))}
            </div>

            <div className="rounded-xl border border-warn/30 bg-warn/[0.06] p-3">
              <div className="flex items-center justify-between">
                <div className="text-[13px] font-medium text-warn">需要审批：安装依赖</div>
                <div className="flex gap-2">
                  <button className="rounded-md bg-surface-2 px-3 py-1.5 text-[12px] text-muted ring-1 ring-line hover:text-text">
                    拒绝
                  </button>
                  <button className="rounded-md bg-accent-dim px-3 py-1.5 text-[12px] font-medium text-white hover:brightness-110">
                    批准
                  </button>
                </div>
              </div>
              <p className="mt-1.5 font-mono text-[11px] text-muted">pip install email-validator</p>
            </div>
          </div>
        </Panel>

        <div className="flex flex-col gap-4">
          <Panel title="终端" hint="示例数据 · v0.5">
            <div className="rounded-lg bg-black/50 p-3 font-mono text-[11px] leading-relaxed">
              {terminal.map((l, i) => (
                <div key={i} className="flex gap-2">
                  <span className="text-faint">{l.t}</span>
                  <span
                    className={
                      l.tone === "cmd"
                        ? "text-text"
                        : l.tone === "err"
                          ? "text-danger"
                          : l.tone === "warn"
                            ? "text-warn"
                            : "text-muted"
                    }
                  >
                    {l.s}
                  </span>
                </div>
              ))}
            </div>
          </Panel>

          <Panel title="v0.1 验收" hint="阶段 0">
            <ul className="flex flex-col gap-2">
              {checks.map((c) => (
                <li key={c.label} className="flex items-center gap-2.5 text-[13px]">
                  <span
                    className={`grid size-4 place-items-center rounded text-[10px] ${
                      c.ok ? "bg-accent/15 text-accent" : "bg-warn/15 text-warn"
                    }`}
                  >
                    {c.ok ? "✓" : "!"}
                  </span>
                  <span className="flex-1 text-muted">{c.label}</span>
                  <span className="font-mono text-[10px] text-faint">{c.note}</span>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}

function StateDot({ state }: { state: string }) {
  const map: Record<string, string> = {
    ok: "bg-accent",
    running: "bg-brand",
    done: "bg-accent",
    waiting: "bg-warn",
    run: "bg-brand animate-pulse",
  };
  return <span className={`size-1.5 shrink-0 rounded-full ${map[state] ?? "bg-faint"}`} />;
}
