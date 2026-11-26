import type { ReactNode } from "react";

export function Panel({
  title,
  hint,
  action,
  children,
  bodyClassName = "p-4",
}: {
  title: string;
  hint?: string;
  /** 右侧操作位。给了 action 就不再显示 hint（两者抢同一块地方，叠着会挤成两行）。 */
  action?: ReactNode;
  children: ReactNode;
  /** 表格、列表这类要贴边的内容用 `bodyClassName="p-0"` 覆盖内边距。 */
  bodyClassName?: string;
}) {
  return (
    <section className="glass-soft overflow-hidden rounded-[var(--radius-lg)]">
      <header className="flex min-h-11 items-center justify-between gap-3 border-b border-line/70 bg-transparent px-4 py-2">
        <h2 className="shrink-0 text-[13px] font-semibold tracking-[-0.01em]">{title}</h2>
        {action ?? (
          hint && <span className="min-w-0 truncate text-right font-mono text-[11px] text-faint">{hint}</span>
        )}
      </header>
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}