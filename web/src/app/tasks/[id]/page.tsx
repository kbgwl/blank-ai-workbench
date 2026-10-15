import { Suspense } from "react";
import Link from "next/link";
import type { Metadata } from "next";
import { TasksView } from "@/components/tasks/tasks-view";

type Props = {
  params: Promise<{ id: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  return { title: `任务 ${id.slice(0, 8)} · 空白格 AI 操作台` };
}

function DetailFallback() {
  return <div className="p-6 font-mono text-[12px] text-faint">正在打开任务…</div>;
}

async function TaskDetail({ params }: Props) {
  const { id } = await params;
  const taskId = decodeURIComponent(id);
  return (
    <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-5 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[19px] font-semibold tracking-tight">任务详情</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-[13px] leading-relaxed text-muted">
            <span>直接打开某条任务，时间线由事件接口完整恢复。</span>
            <span className="rounded-md border border-line bg-surface px-2 py-0.5 font-mono text-[11px] text-faint">
              {taskId}
            </span>
          </p>
        </div>
        <Link
          href="/tasks"
          className="shrink-0 rounded-md bg-surface-2 px-3 py-1.5 text-[12px] text-muted ring-1 ring-line transition-colors hover:text-text hover:ring-line-strong"
        >
          返回任务列表
        </Link>
      </div>
      <TasksView initialTaskId={taskId} />
    </div>
  );
}

export default function TaskDetailPage({ params }: Props) {
  return (
    <Suspense fallback={<DetailFallback />}>
      <TaskDetail params={params} />
    </Suspense>
  );
}
