import { Panel } from "@/components/panel";
import { AgentRuntimePanel } from "@/components/settings/agent-runtime-panel";
import { FeatureFlagsPanel } from "@/components/settings/feature-flags-panel";
import { McpPanel } from "@/components/settings/mcp-panel";
import { ProvidersView } from "@/components/settings/providers-view";
import { RoleModelSettings } from "@/components/settings/role-model-settings";
import { SafetyPanel } from "@/components/settings/safety-panel";

export default function SettingsPage() {
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-tight">设置</h1>
          <p className="mt-1 text-[13px] leading-relaxed text-muted">
            安全与权限、模型供应商、API Key 加密存储、独立审查与上下文水位、Token 用量。
          </p>
        </div>
        <span className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[11px] whitespace-nowrap text-faint">
          真实数据 · /api/v1/providers · /api/v1/settings
        </span>
      </div>

      {/*
        顺序是有讲究的：先放会改变内核行为的开关，再放模型供应商。
        上一个版本把「执行与智能体」放最前，是因为那一版加的是审查与水位；
        现在客户最先问的是"它到底能在这台机器上做什么"，所以安全与权限排第一 ——
        权限不是高级选项，是安装完第一件要确认的事。
      */}
      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-[14px] font-medium tracking-tight">安全与权限</h2>
            <p className="mt-1 text-[12px] leading-relaxed text-faint">
              决定 Agent 在这台机器上「能做什么」：执行命令、联网、可触达的目录、禁止的命令前缀。
              这一层是硬边界，审批也放不过去。
            </p>
          </div>
          <span className="shrink-0 rounded-md border border-brand/40 bg-brand/10 px-2 py-0.5 font-mono text-[10px] text-brand">
            0.5.0 新增
          </span>
        </div>
        <SafetyPanel />
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-[14px] font-medium tracking-tight">执行与智能体</h2>
            <p className="mt-1 text-[12px] leading-relaxed text-faint">
              这四个开关直接改变内核行为：审查档位、上下文水位、新任务默认权限、工具是否按需加载。改完对下一个任务生效。
            </p>
          </div>
          <span className="shrink-0 rounded-md border border-brand/40 bg-brand/10 px-2 py-0.5 font-mono text-[10px] text-brand">
            0.4.0 新增
          </span>
        </div>
        <AgentRuntimePanel />
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-[14px] font-medium tracking-tight">特性开关</h2>
            <p className="mt-1 text-[12px] leading-relaxed text-faint">
              内核里所有「开不开」的判断都在一张注册表里，这里能看见每一项目前是谁说了算：
              环境变量、设置文件、老字段，还是注册表默认。改完对下一个任务生效。
            </p>
          </div>
          <span className="shrink-0 rounded-md border border-brand/40 bg-brand/10 px-2 py-0.5 font-mono text-[10px] text-brand">
            0.15 新增
          </span>
        </div>
        <FeatureFlagsPanel />
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
          <div>
            <h2 className="text-[14px] font-medium tracking-tight">外部工具（MCP）</h2>
            <p className="mt-1 text-[12px] leading-relaxed text-faint">
              接进来的外部服务端，工具会和内置工具一样走审批与审计。这里能看到每个服务端
              连没连上、为什么没连上——配置写错时界面上的症状只有「工具变少了」。
            </p>
          </div>
          <span className="shrink-0 rounded-md border border-brand/40 bg-brand/10 px-2 py-0.5 font-mono text-[10px] text-brand">
            0.6.0 新增
          </span>
        </div>
        <McpPanel />
      </section>

      <section>
        <div className="mb-3">
          <h2 className="text-[14px] font-medium tracking-tight">模型供应商</h2>
          <p className="mt-1 text-[12px] leading-relaxed text-faint">
            没有可用供应商时任务跑不起来。API Key 加密存储，只在本机使用。
          </p>
        </div>
        <ProvidersView />
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="默认模型与任务级选择" hint="v0.4 接入">
          <p className="text-[13px] leading-relaxed text-faint">
            顶部可切换当前模型；任务接口支持备用模型，在可重试故障时自动切换。
          </p>
          <RoleModelSettings />
        </Panel>
        <Panel title="审计与回滚" hint="v0.4 接入">
          <p className="text-[13px] leading-relaxed text-faint">
            每次工具调用都落在 tool_calls 与任务事件里，含风险等级、权限判定与耗时；
            写文件前留备份，任务执行前留整树快照，可以按检查点回滚。
          </p>
        </Panel>
      </div>
    </div>
  );
}
