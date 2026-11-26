"use client";

import Link from "next/link";
import { useEffect } from "react";

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="flex h-dvh items-center justify-center bg-bg p-6 text-text">
      <section className="w-full max-w-lg rounded-[var(--radius-lg)] border border-line bg-surface p-6 shadow-[var(--shadow-lg)]">
        <p className="text-[12px] font-medium uppercase tracking-wide text-faint">页面没能打开</p>
        <h1 className="mt-2 text-xl font-semibold">这一屏没有渲染出来</h1>
        <p className="mt-2 text-sm text-muted">你的数据没有被这一步改掉。先重试；如果反复出现，换到工作台再进来。</p>
        <details className="mt-4 text-xs text-muted">
          <summary className="cursor-pointer">技术详情</summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-bg p-2">{error.message}</pre>
        </details>
        <div className="mt-5 flex gap-2">
          <button type="button" onClick={reset} className="rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-on-brand transition-colors hover:bg-brand-hover">重试</button>
          <Link href="/" className="rounded-md border border-line px-3 py-1.5 text-sm transition-colors hover:bg-surface-hover">返回工作台</Link>
        </div>
      </section>
    </main>
  );
}
