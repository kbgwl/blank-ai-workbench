import type { Metadata } from "next";
import "./globals.css";
import { HealthPill } from "@/components/health-pill";
import { SideNav } from "@/components/side-nav";

export const metadata: Metadata = {
  title: "空白格 AI 操作台",
  description: "面向开发者的可审计 AI 工程执行台",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body className="flex h-dvh flex-col overflow-hidden">
        <header className="flex h-11 shrink-0 items-center justify-between border-b border-line bg-bg px-4">
          <div className="flex items-center gap-3">
            <div className="grid size-6 place-items-center rounded-md bg-brand/12 ring-1 ring-brand/25">
              <span className="size-2 rounded-[2px] bg-brand" />
            </div>
            <div className="text-[13px] font-semibold tracking-tight">空白格</div>
          </div>
          <div className="flex items-center gap-3">
            <HealthPill />
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          <aside className="hidden w-52 shrink-0 border-r border-line bg-surface/30 md:block">
            <SideNav />
          </aside>
          <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
        </div>
      </body>
    </html>
  );
}
