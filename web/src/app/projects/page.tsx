import { PageBody, PageHeader } from "@/components/page";
import { ProjectsView } from "@/components/projects/projects-view";

export default function ProjectsPage() {
  return (
    <PageBody>
      <PageHeader
        title="项目"
        description="创建项目、绑定本地工作区目录，浏览工作区内的文件内容。"
        actions={
          <span className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 font-mono text-[11px] whitespace-nowrap text-faint">
            真实数据 · /api/v1/projects
          </span>
        }
      />
      <ProjectsView />
    </PageBody>
  );
}