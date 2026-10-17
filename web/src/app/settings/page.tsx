import { Panel } from "@/components/panel";
import { AgentRuntimePanel } from "@/components/settings/agent-runtime-panel";
import { ProvidersView } from "@/components/settings/providers-view";
import { RoleModelSettings } from "@/components/settings/role-model-settings";

export default function SettingsPage() {
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-tight">设置</h1>
          <p className="mt-1 text-[13px] leading-relaxed text-muted">
            模型供应商、API Key 加密存储、独立审查与上下文水位、Token 用量。
          </p>
        </div>
        <span className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[11px] whitespace-nowrap text-faint">
          真实数据 · /api/v1/providers · /api/v1/settings
        </span>
      </div>

      <ProvidersView />

      <div>
        <h2 className="mb-2 text-[13px] font-medium tracking-tight">执行与智能体</h2>
        <p className="mb-3 text-[12px] leading-relaxed text-faint">
          这些开关直接改变内核行为：审查档位、上下文水位、新任务的默认权限，以及工具是否按需加载。
        </p>
        <AgentRuntimePanel />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="沙箱与工作区" hint="v0.6 接入">
          <p className="text-[13px] leading-relaxed text-faint">
            工作区路径白名单、Shell 超时、Docker 沙箱、本地执行显式授权和参数级风险审批已由运行时执行。
          </p>
        </Panel>
        <Panel title="默认模型与任务级选择" hint="v0.4 接入">
          <p className="text-[13px] leading-relaxed text-faint">
            顶部可切换当前模型；任务接口支持备用模型，在可重试故障时自动切换。
          </p>
          <RoleModelSettings />
        </Panel>
      </div>
    </div>
  );
}
