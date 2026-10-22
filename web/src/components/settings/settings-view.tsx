"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Panel } from "@/components/panel";
import { AgentRuntimePanel } from "./agent-runtime-panel";
import { FeatureFlagsPanel } from "./feature-flags-panel";
import { McpPanel } from "./mcp-panel";
import { ProvidersView } from "./providers-view";
import { RoleModelSettings } from "./role-model-settings";
import { RoutingEvidencePanel } from "./routing-evidence-panel";
import { SafetyPanel } from "./safety-panel";

type SectionKey = "models" | "safety" | "runtime" | "tools" | "advanced";

type Section = {
  key: SectionKey;
  label: string;
  hint: string;
  description: string;
  status: string;
  icon: ReactNode;
};

const iconProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "size-4",
};

const SECTIONS: Section[] = [
  {
    key: "models",
    label: "模型与供应商",
    hint: "默认模型、接入与用量",
    description:
      "配置任务使用哪个模型、接入哪些供应商，并在这里测试连通性和查看 Token 用量。API Key 只加密保存在本机。",
    status: "即时生效",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z" />
        <path d="M12 12v9M12 12 4 7.5M12 12l8-4.5" />
      </svg>
    ),
  },
  {
    key: "safety",
    label: "安全与权限",
    hint: "命令、联网与路径边界",
    description:
      "决定 Agent 在这台机器上能做什么：执行命令、联网、可触达的目录和禁止的命令前缀。这是硬边界，审批也绕不过去。",
    status: "对下一个任务生效",
    icon: (
      <svg {...iconProps}>
        <path d="M12 3 5 6v5c0 4.2 2.8 7.4 7 9 4.2-1.6 7-4.8 7-9V6l-7-3Z" />
        <path d="m9 12 2 2 4-4" />
      </svg>
    ),
  },
  {
    key: "runtime",
    label: "执行与智能体",
    hint: "审查、上下文与默认权限",
    description:
      "调整独立审查档位、上下文水位、新任务默认权限和工具加载方式。改完对下一个任务生效。",
    status: "对下一个任务生效",
    icon: (
      <svg {...iconProps}>
        <rect x="4" y="4" width="16" height="16" rx="2" />
        <path d="M9 9h6v6H9zM9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
      </svg>
    ),
  },
  {
    key: "tools",
    label: "外部工具",
    hint: "MCP 服务端连接",
    description:
      "接入外部 MCP 服务端，工具会和内置工具一样走审批与审计。连接失败时，这里能看到原因，而不只是「工具变少了」。",
    status: "实时读取",
    icon: (
      <svg {...iconProps}>
        <path d="M9 7V4a3 3 0 0 1 6 0v3" />
        <rect x="5" y="7" width="14" height="9" rx="2" />
        <path d="M12 16v5M8 21h8" />
      </svg>
    ),
  },
  {
    key: "advanced",
    label: "高级与实验",
    hint: "特性开关与路由证据",
    description:
      "内核里所有「开不开」的判断和按需选模型的实测依据。每一项都会标出生效来源，不确定时先看默认值。",
    status: "对下一个任务生效",
    icon: (
      <svg {...iconProps}>
        <path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h7M15 18h5" />
        <circle cx="16" cy="6" r="2" />
        <circle cx="8" cy="12" r="2" />
        <circle cx="13" cy="18" r="2" />
      </svg>
    ),
  },
];

function sectionFromHash(): SectionKey | null {
  const value = window.location.hash.replace(/^#/, "");
  return SECTIONS.some((section) => section.key === value) ? (value as SectionKey) : null;
}

function SectionBody({ active }: { active: SectionKey }) {
  switch (active) {
    case "models":
      return (
        <div className="flex flex-col gap-4">
          <Panel title="任务默认模型" hint="按角色指定供应商">
            <p className="text-[12px] leading-relaxed text-faint">
              不选就跟随顶部主模型。任务接口支持备用模型，在可重试故障时自动切换。
            </p>
            <RoleModelSettings />
          </Panel>
          <ProvidersView />
        </div>
      );
    case "safety":
      return <SafetyPanel />;
    case "runtime":
      return <AgentRuntimePanel />;
    case "tools":
      return <McpPanel />;
    case "advanced":
      return (
        <div className="flex flex-col gap-4">
          <FeatureFlagsPanel />
          <RoutingEvidencePanel />
          <Panel title="审计与回滚" hint="tool_calls · 文件快照">
            <p className="text-[12.5px] leading-relaxed text-faint">
              每次工具调用都落在 tool_calls 与任务事件里，含风险等级、权限判定与耗时；写文件前留备份，
              任务执行前留整树快照，可以按检查点回滚。
            </p>
          </Panel>
        </div>
      );
  }
}

export function SettingsView() {
  const [active, setActive] = useState<SectionKey>("models");
  const contentRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const applyHash = () => {
      const next = sectionFromHash();
      if (next) setActive(next);
    };
    applyHash();
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, []);

  function select(key: SectionKey) {
    if (key === active) return;
    setActive(key);
    contentRef.current?.scrollIntoView({ block: "start" });
  }

  const index = SECTIONS.findIndex((section) => section.key === active);
  const section = SECTIONS[index] ?? SECTIONS[0];

  return (
    <div className="mx-auto w-full max-w-[1360px] px-4 py-6 sm:px-6 lg:px-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 font-mono text-[10px] tracking-[0.16em] text-faint uppercase">
            <span className="size-1.5 rounded-full bg-brand" />
            本机工作区配置
          </div>
          <h1 className="mt-2 text-[24px] font-semibold tracking-tight">设置</h1>
          <p className="mt-1 max-w-[660px] text-[13px] leading-relaxed text-muted">
            按分类管理这台机器上的运行方式。改动只保存在本机后端，不会同步到其他设备。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[10px] text-faint">
            {SECTIONS.length} 个分类
          </span>
          <span className="rounded-md border border-brand/25 bg-brand/[0.06] px-2.5 py-1 font-mono text-[10px] text-brand">
            API Key 加密存储
          </span>
        </div>
      </header>

      <div className="mt-6 grid items-start gap-5 lg:grid-cols-[15.5rem_minmax(0,1fr)] xl:gap-8">
        <div className="min-w-0 lg:sticky lg:top-5">
          <nav
            aria-label="设置分类"
            className="flex gap-2 overflow-x-auto pb-1 lg:flex-col lg:gap-1 lg:overflow-visible lg:rounded-xl lg:border lg:border-line lg:bg-canvas lg:p-2 lg:pb-2"
          >
            {SECTIONS.map((item) => {
              const selected = item.key === active;
              return (
                <a
                  key={item.key}
                  href={`#${item.key}`}
                  aria-current={selected ? "page" : undefined}
                  onClick={() => select(item.key)}
                  className={`relative flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-left transition-colors lg:w-full lg:gap-3 lg:py-2.5 ${
                    selected
                      ? "bg-surface-2 text-text"
                      : "text-muted hover:bg-surface-2/60 hover:text-text"
                  }`}
                >
                  {selected && (
                    <span className="absolute top-1/2 left-0 hidden h-5 w-0.5 -translate-y-1/2 rounded-full bg-brand lg:block" />
                  )}
                  <span
                    className={`grid size-7 shrink-0 place-items-center rounded-md border transition-colors ${
                      selected
                        ? "border-brand/25 bg-brand/[0.08] text-brand"
                        : "border-line bg-surface text-faint"
                    }`}
                  >
                    {item.icon}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[12.5px] font-medium whitespace-nowrap">{item.label}</span>
                    <span className="mt-0.5 hidden truncate text-[10.5px] text-faint lg:block">
                      {item.hint}
                    </span>
                  </span>
                </a>
              );
            })}
          </nav>
          <div className="mt-3 hidden rounded-xl border border-line bg-surface/60 p-3 lg:block">
            <div className="flex items-center gap-2 text-[11px] font-medium text-muted">
              <svg {...iconProps} className="size-3.5">
                <rect x="5" y="10" width="14" height="10" rx="2" />
                <path d="M8 10V7a4 4 0 0 1 8 0v3" />
              </svg>
              本机保存
            </div>
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-faint">
              密钥不会以明文回显；安全边界由后端强制执行，不依赖界面。
            </p>
          </div>
        </div>

        <section ref={contentRef} aria-labelledby="settings-section-title" className="min-w-0 scroll-mt-4">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line pb-4">
            <div className="flex min-w-0 items-start gap-3">
              <span className="grid size-10 shrink-0 place-items-center rounded-xl border border-brand/20 bg-brand/[0.06] text-brand">
                {section.icon}
              </span>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 id="settings-section-title" className="text-[18px] font-semibold tracking-tight">
                    {section.label}
                  </h2>
                  <span className="rounded-md border border-line bg-surface px-1.5 py-0.5 font-mono text-[10px] text-faint">
                    {section.status}
                  </span>
                </div>
                <p className="mt-1 max-w-[700px] text-[12.5px] leading-relaxed text-muted">
                  {section.description}
                </p>
              </div>
            </div>
            <span className="shrink-0 font-mono text-[10px] text-faint">
              {String(index + 1).padStart(2, "0")} / {String(SECTIONS.length).padStart(2, "0")}
            </span>
          </div>

          <div className="mt-4">
            <SectionBody active={active} />
          </div>
        </section>
      </div>
    </div>
  );
}
