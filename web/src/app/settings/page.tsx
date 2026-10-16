import { Panel } from "@/components/panel";
import { ProvidersView } from "@/components/settings/providers-view";

export default function SettingsPage() {
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-tight">设置</h1>
          <p className="mt-1 text-[13px] leading-relaxed text-muted">
            模型供应商、API Key 加密存储、流式与工具能力检测、Token 用量。
          </p>
        </div>
        <span className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[11px] whitespace-nowrap text-faint">
          真实数据 · /api/v1/providers
        </span>
      </div>

      <ProvidersView />

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="执行与安全" hint="v0.6 接入">
          <p className="text-[13px] leading-relaxed text-faint">
            工作区路径白名单、Shell 超时、Docker 沙箱、本地执行显式授权和参数级风险审批已由运行时执行。
          </p>
        </Panel>
        <Panel title="默认模型与任务级选择" hint="v0.4 接入">
          <p className="text-[13px] leading-relaxed text-faint">
            顶部可切换当前模型；任务接口支持备用模型，在可重试故障时自动切换。
          </p>
        </Panel>
      </div>
    </div>
  );
}
