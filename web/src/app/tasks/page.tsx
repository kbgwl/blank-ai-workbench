import { TasksView } from "@/components/tasks/tasks-view";

export default function TasksPage() {
  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex h-11 shrink-0 items-center justify-between border-b border-line px-4">
        <div className="flex min-w-0 items-center gap-2 text-[12px]"><span className="text-muted">任务</span><span className="text-faint">/</span><span className="truncate text-text">工作区执行</span></div>
        <span className="font-mono text-[10px] text-faint">local runtime</span>
      </div>
      <div className="min-h-0 flex-1 p-3 lg:p-4"><TasksView /></div>
    </div>
  );
}
