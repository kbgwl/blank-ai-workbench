"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Panel } from "@/components/panel";
import { PageBody } from "@/components/page";
import { AgentRuntimePanel } from "./agent-runtime-panel";
import { ChannelsPanel } from "./channels-panel";
import { FeatureFlagsPanel } from "./feature-flags-panel";
import { McpPanel } from "./mcp-panel";
import { PluginsPanel } from "./plugins-panel";
import { ProvidersView } from "./providers-view";
import { RoleModelSettings } from "./role-model-settings";
import { RoutingEvidencePanel } from "./routing-evidence-panel";
import { SafetyPanel } from "./safety-panel";

type SectionKey = "models" | "safety" | "runtime" | "channels" | "tools" | "advanced";

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
    key: "channels",
    label: "消息通道",
    hint: "企业微信 / 钉钉 / 飞书",
    description:
      "把任务提醒推到客户自己的聊天软件，也可以在那边发一句话让它动手。填完凭据点「验证」就能用；收消息的方式各家不同——钉钉的长连接不需要公网地址。",
    status: "保存即生效",
    icon: (
      <svg {...iconProps}>
        <path d="M20 12a8 8 0 1 1-3.1-6.3" />
        <path d="M4 20l1.6-3.2A8 8 0 0 0 12 20h8" />
        <path d="M9 10h.01M13 10h.01M17 10h.01" />
      </svg>
    ),
  },
  {
    key: "tools",
    label: "外部工具与插件",
    hint: "MCP 服务端 · 插件包",
    description:
      "接入外部 MCP 服务端，工具会和内置工具一样走审批与审计；插件包（plugin.json）则把命令、子代理、技能与输出样式按工作区打包分发。加载失败时，这里能看到原因，而不只是「工具变少了」。",
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
    case "channels":
      return <ChannelsPanel />;
    case "tools":
      return (
        <div className="flex flex-col gap-4">
          <PluginsPanel />
          <McpPanel />
        </div>
      );
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
    <PageBody>
      <div className="grid items-start gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <nav aria-label="设置分类" className="flex min-w-0 gap-1 overflow-x-auto lg:sticky lg:top-0 lg:flex-col lg:overflow-visible">
          {SECTIONS.map((item) => {
            const selected = item.key === active;
            return (
              <a
                key={item.key}
                href={`#${item.key}`}
                aria-current={selected ? "page" : undefined}
                onClick={() => select(item.key)}
                className={`flex shrink-0 items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px] transition-colors lg:w-full ${
                  selected ? "bg-surface-2 font-medium text-text" : "text-muted hover:bg-surface-2 hover:text-text"
                }`}
              >
                <span className={selected ? "text-text" : "text-faint"}>{item.icon}</span>
                <span className="whitespace-nowrap">{item.label}</span>
              </a>
            );
          })}
        </nav>

        <section ref={contentRef} aria-labelledby="settings-section-title" className="min-w-0 scroll-mt-4">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3">
            <div className="flex min-w-0 items-center gap-2">
              <h2 id="settings-section-title" className="text-[13.5px] font-semibold tracking-tight">
                {section.label}
              </h2>
              <span className="rounded border border-line bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-faint">
                {section.status}
              </span>
            </div>
            <span className="shrink-0 font-mono text-[10px] text-faint">
              {String(index + 1).padStart(2, "0")} / {String(SECTIONS.length).padStart(2, "0")}
            </span>
          </div>
          <p className="mt-2 text-[12px] leading-relaxed text-muted">{section.description}</p>

          <div className="mt-4">
            <SectionBody active={active} />
          </div>
        </section>
      </div>
    </PageBody>
  );
}
