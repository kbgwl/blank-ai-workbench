import { ProjectsView } from "@/components/projects/projects-view";

export default function ProjectsPage() {
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-tight">项目</h1>
          <p className="mt-1 text-[13px] leading-relaxed text-muted">
            创建项目、绑定本地工作区目录，浏览工作区内的文件内容。
          </p>
        </div>
        <span className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[11px] whitespace-nowrap text-faint">
          真实数据 · /api/v1/projects
        </span>
      </div>
      <ProjectsView />
    </div>
  );
}
