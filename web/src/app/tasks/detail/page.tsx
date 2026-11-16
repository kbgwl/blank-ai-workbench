"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { PageBody, PageHeader } from "@/components/page";
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

function BackLink() {
  return (
    <Link
      href="/tasks"
      className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-line-strong hover:text-text"
    >
      返回任务列表
    </Link>
  );
}

function TaskDetail() {
  const taskId = useSearchParams().get("id") ?? "";

  if (!taskId) {
    return (
      <PageBody wide>
        <PageHeader title="任务详情" description="缺少任务 ID。请从任务列表进入，或附带 ?id= 参数。" actions={<BackLink />} />
      </PageBody>
    );
  }

  return (
    <PageBody wide>
      <PageHeader
        title="任务详情"
        description={
          <>
            直接打开某条任务，时间线由事件接口完整恢复。
            <span className="ml-2 rounded-md border border-line bg-surface px-2 py-0.5 font-mono text-[10px] text-faint">
              {taskId}
            </span>
          </>
        }
        actions={<BackLink />}
      />
      <TasksView initialTaskId={taskId} />
    </PageBody>
  );
}

export default function TaskDetailPage() {
  return (
    <Suspense fallback={<DetailFallback />}>
      <TaskDetail />
    </Suspense>
  );
}