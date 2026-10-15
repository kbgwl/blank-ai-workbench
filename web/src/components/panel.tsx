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
    <section className="rounded-lg border border-line/80 bg-surface/25">
      <header className="flex items-center justify-between border-b border-line/80 px-3.5 py-2.5">
        <h2 className="text-[13px] font-medium tracking-tight">{title}</h2>
        {hint && <span className="font-mono text-[10px] text-faint">{hint}</span>}
      </header>
      <div className="p-3">{children}</div>
    </section>
  );
}
