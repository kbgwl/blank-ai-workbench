import type { ReactNode } from "react";

export function Panel({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-line bg-surface">
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <h2 className="shrink-0 text-[14px] font-medium tracking-tight">{title}</h2>
        {hint && <span className="min-w-0 truncate text-right font-mono text-[11px] text-faint">{hint}</span>}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}