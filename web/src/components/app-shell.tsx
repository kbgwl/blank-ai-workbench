"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { AppRail } from "@/components/app-rail";
import { CommandPalette } from "@/components/command-palette";
import { ModelSwitcher } from "@/components/model";
import { isEditableTarget, PALETTE_SHORTCUT, shortcutHint, shouldHandleEscape } from "@/lib/shortcuts";

const TITLES: Record<string, string> = {
  "/settings": "设置",
  "/projects": "项目与工作区",
  "/monitor": "监控",
  "/evidence": "证据库",
  "/skills": "技能",
  "/tasks": "任务",
  "/tasks/detail": "任务详情",
  "/daily": "日常",
  "/team": "Bot 团队",
};

/**
 * 应用外壳，布局对标 Qoder：
 *   左侧常驻栏（品牌 / 新建 / 搜索 / 工作区 / 最近任务 / 固定入口）
 *   + 灰色画布上的一块圆角主卡片（页面头 + 滚动内容）。
 *
 * 首页自带会话栏、任务工作台自带三栏，这两处由各自页面负责满幅渲染。
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const clean = (pathname ?? "").replace(/\.html$/, "").replace(/\/index$/, "");
  const bare = clean === "" || clean === "/" || clean === "/tasks";
  const title = TITLES[clean] ?? "";

  // Esc 回工作台；在输入框/对话框里按 Esc 不抢事件（那通常是取消编辑）。
  useEffect(() => {
    if (bare) return;
    const onKey = (event: KeyboardEvent) => {
      if (
        !shouldHandleEscape({
          key: event.key,
          defaultPrevented: event.defaultPrevented,
          inEditable: isEditableTarget(event.target as HTMLElement | null),
          overlayOpen: Boolean(document.querySelector("dialog[open], [data-overlay-open]")),
        })
      ) {
        return;
      }
      router.push("/");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [bare, router]);

  if (bare) {
    return (
      <div className="h-dvh overflow-hidden">
        {children}
        <CommandPalette />
      </div>
    );
  }

  return (
    <div className="flex h-dvh overflow-hidden bg-transparent">
      <AppRail />
      <div className="flex min-w-0 flex-1 flex-col p-3">
        <div className="glass flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[var(--radius-lg)]">
          <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line/70 bg-transparent px-5">
            <Link
              href="/"
              title="返回工作台（Esc）"
              className="grid size-7 shrink-0 place-items-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-text"
            >
              <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="m14 6-6 6 6 6" />
              </svg>
            </Link>
            <span className="size-1.5 shrink-0 rounded-full bg-brand" aria-hidden />
            <span className="min-w-0 truncate text-[15px] font-semibold tracking-[-0.01em]">{title || "空白格"}</span>

            <div className="ml-auto flex shrink-0 items-center gap-2.5">
              <kbd className="hidden items-center gap-1 rounded-md border border-line bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-faint lg:flex">
                {shortcutHint(PALETTE_SHORTCUT)}
              </kbd>
              <ModelSwitcher />
            </div>
          </header>
          <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">{children}</main>
        </div>
      </div>
      <CommandPalette />
    </div>
  );
}
