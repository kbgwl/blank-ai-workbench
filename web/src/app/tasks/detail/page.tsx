"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { TasksView } from "@/components/tasks/tasks-view";

/**
 * 任务详情。
 *
 * 用查询参数 `/tasks/detail?id=xxx` 而不是路径段 `/tasks/[id]`：
 * 任务 ID 是运行时产生的，静态导出无法为它预生成 HTML，动态路径段会被
 * `output: "export"` 直接拒绝。改成查询参数后页面本身是静态的，ID 在客户端读取。
 */
function DetailFallback() {
  return <div className="p-6 font-mono text-[12px] text-faint">正在打开任务…</div>;
}

function TaskDetail() {
  const taskId = useSearchParams().get("id") ?? "";

  if (!taskId) {
    return (
      <div className="mx-auto flex w-full max-w-[1600px] flex-col gap-4 p-6">
        <h1 className="text-[19px] font-semibold tracking-tight">任务详情</h1>
        <p className="text-[13px] text-muted">缺少任务 ID。请从任务列表进入，或附带 <span className="font-mono">?id=</span> 参数。</p>
        <Link
          href="/tasks"
          className="w-fit rounded-md bg-surface-2 px-3 py-1.5 text-[12px] text-muted ring-1 ring-line transition-colors hover:text-text hover:ring-line-strong"
        >
          返回任务列表
        </Link>
      </div>
    );
  }

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

export default function TaskDetailPage() {
  return (
    <Suspense fallback={<DetailFallback />}>
      <TaskDetail />
    </Suspense>
  );
}
