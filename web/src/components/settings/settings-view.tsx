"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Panel } from "@/components/panel";
import { PageBody } from "@/components/page";
import { UTILITY_LINKS } from "@/components/bot/nav";
import { AgentRuntimePanel } from "./agent-runtime-panel";
import { ChannelsPanel } from "./channels-panel";
import { FeatureFlagsPanel } from "./feature-flags-panel";
import { McpPanel } from "./mcp-panel";
import { PluginsPanel } from "./plugins-panel";
import { ProvidersView } from "./providers-view";
import { RoleModelSettings } from "./role-model-settings";
import { RoutingEvidencePanel } from "./routing-evidence-panel";
import { SafetyPanel } from "./safety-panel";

type SectionKey = "models" | "safety" | "runtime" | "channels" | "tools" | "utilities" | "advanced";

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
    key: "utilities",
    label: "查看与效率",
    hint: "监控 · 证据库 · 技能 · 连接器",
    description:
      "监控、交付证据、你确认过的做法、外部工具——这些是低频的查看与配置项，不占常驻侧栏。需要时从这里进去。",
    status: "随时可达",
    icon: (
      <svg {...iconProps}>
        <path d="M4 5h16v14H4z" />
        <path d="M4 9h16M9 9v10M13 13h4" />
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
            <p className="text-[13px] leading-relaxed text-faint">
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
    case "utilities":
      return (
        <Panel title="低频入口" hint="从侧栏收进这里，不再占常驻位置">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {UTILITY_LINKS.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="group flex min-w-0 flex-col gap-0.5 rounded-lg border border-line bg-surface-2 px-3 py-2.5 transition-colors hover:border-line-strong hover:bg-surface-3"
              >
                <span className="flex items-center justify-between text-[14px] font-medium text-text">
                  {item.label}
                  <span className="text-faint transition-transform group-hover:translate-x-0.5">→</span>
                </span>
                <span className="text-[12px] text-faint">{item.hint}</span>
              </Link>
            ))}
          </div>
        </Panel>
      );
    case "advanced":
      return (
        <div className="flex flex-col gap-4">
          <FeatureFlagsPanel />
          <RoutingEvidencePanel />
          <Panel title="审计与回滚" hint="tool_calls · 文件快照">
            <p className="text-[13px] leading-relaxed text-faint">
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
    <PageBody wide className="gap-4">
      {/* 设置页不是宽表：内容铺到 1600px 时，说明文字一行能跑七八十个字，
          左侧标签与右侧取值之间隔半屏，扫读时眼睛要来回跳。收到 1080 并左对齐——
          居中会在导航与内容之间再插一段空白，看着像两个不相干的区域。 */}
      <section ref={contentRef} aria-label="设置" className="min-w-0 max-w-[1080px] scroll-mt-4">
        {/* 分类改成顶部横向 tab：去掉第二层 208px 侧栏，内容区铺满。
             Qoder 的设置就是单栏 + 顶部分类，不再让左栏叠左栏。
             标题只由外壳顶栏显示一次，这里不再重复写一遍「设置」——
             同一屏出现两个一样的标题，是之前"看着乱"最直接的一处。

            选中态用品牌色下划线：浅色皮肤是近黑、深色皮肤是浅蓝，两套都够对比；
             白底胶囊在浅色下和背景差一档、在深色下又像"更黑的一块"，两头都不稳。
             指示条用 bg-brand 的元素画、不写 border-bottom：globals.css 里的
             `* { border-color: var(--line) }` 是未分层的规则，会盖掉 utility 层的
             `border-brand`（两边都看不出品牌色）。 */}
        <div className="border-b border-line">
          <nav aria-label="设置分类" className="flex flex-wrap items-center gap-0.5">
            {SECTIONS.map((item) => {
              const selected = item.key === active;
              return (
                <a
                  key={item.key}
                  href={`#${item.key}`}
                  aria-current={selected ? "page" : undefined}
                  onClick={() => select(item.key)}
                  className={`relative flex shrink-0 items-center gap-1.5 px-3 pt-1 pb-3 text-[13px] transition-colors ${
                    selected ? "font-medium text-text" : "text-muted hover:text-text"
                  }`}
                >
                  <span className={selected ? "text-brand" : "text-faint"}>{item.icon}</span>
                  <span className="whitespace-nowrap">{item.label}</span>
                  {selected && (
                    <span aria-hidden className="pointer-events-none absolute inset-x-0 -bottom-px h-0.5 bg-brand" />
                  )}
                </a>
              );
            })}
          </nav>
        </div>
        {/* 描述降到辅助层：状态不再是带边框的等宽 chip，只作为句首一个小注脚，
            和正文同一行读下来，不与 tab 抢注意力。 */}
        <p className="mt-2.5 max-w-[72ch] text-[12px] leading-relaxed text-muted">
          <span className="font-medium text-muted">{section.status}</span>
          <span className="mx-1.5 text-line-strong">·</span>
          {section.description}
        </p>

        <div className="mt-4">
          <SectionBody active={active} />
        </div>
      </section>
    </PageBody>
  );
}
