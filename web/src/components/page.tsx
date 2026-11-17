import type { ReactNode } from "react";

/**
 * 内容页的统一壳体。
 *
 * 之前每一页各写一套 max-w（896/1024/1152/1360/1400）和标题字号（17–30px），
 * 同一款产品在不同页面之间左右边缘对不上，切换页面像换了一个软件。
 * 宽度、留白、标题层级只在这里定义一次。
 */
export function PageBody({
  children,
  wide = false,
  className = "",
}: {
  children: ReactNode;
  /** 三栏工作台这类需要横向空间的页面用宽壳，其余一律 1120。 */
  wide?: boolean;
  className?: string;
}) {
  return (
    <div
      className={`mx-auto flex w-full flex-col gap-5 px-6 py-6 ${wide ? "max-w-[1440px]" : "max-w-[1120px]"} ${className}`}
    >
      {children}
    </div>
  );
}

export function PageHeader(props: {
  title: string;
  description?: ReactNode;
  /** 右侧操作区。放筛选、刷新、计数这类附属信息。 */
  actions?: ReactNode;
}) {
  // 页面标题已由外壳顶栏承担；这里只留一句说明和操作区。
  // h1 仍保留给读屏与文档大纲，但不再和顶栏重复第二遍。
  return (
    <header className="flex flex-wrap items-center justify-between gap-4">
      <div className="min-w-0">
        <h1 className="sr-only">{props.title}</h1>
        {props.description && (
          <p className="max-w-2xl text-[13.5px] leading-relaxed text-muted">{props.description}</p>
        )}
      </div>
      {props.actions && <div className="flex shrink-0 items-center gap-2">{props.actions}</div>}
    </header>
  );
}