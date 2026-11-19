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
    <section className="overflow-hidden rounded-lg border border-line bg-surface">
      <header className="flex min-h-[42px] items-center justify-between gap-3 border-b border-line px-4 py-2">
        <h2 className="shrink-0 text-[14px] font-medium tracking-tight">{title}</h2>
        {action ?? (
          hint && <span className="min-w-0 truncate text-right font-mono text-[11px] text-faint">{hint}</span>
        )}
      </header>
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}