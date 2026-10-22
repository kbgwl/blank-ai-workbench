"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { HealthPill } from "@/components/health-pill";
import { CommandPalette } from "@/components/command-palette";
import { ModelSwitcher } from "@/components/model";
import { isEditableTarget, PALETTE_SHORTCUT, shortcutHint, shouldHandleEscape } from "@/lib/shortcuts";

const TITLES: Record<string, string> = {
  "/settings": "设置",
  "/projects": "项目与工作区",
  "/tasks/detail": "任务详情",
  "/brainstorm": "头脑风暴",
  "/daily": "日常",
  "/features": "能力",
  "/reverse-lab": "逆向实验室",
  "/team": "Bot 团队",
};

/**
 * 首页是 bot，它自带会话侧栏和设置入口，所以顶栏上不再挂导航。
 * 其余页面（设置、以及暂时保留但不再露出的旧页）仍然走带返回的顶栏。
 */
const NAV: { href: string; label: string }[] = [];

/** 首页与任务页是满幅工作台（左栏自带品牌与导航），其余页面共用一条带返回的顶栏。 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  // 静态导出后首屏 URL 是 /index.html（或 /index），开发态是 /，都要认成首页。
  const clean = (pathname ?? "").replace(/\.html$/, "").replace(/\/index$/, "");
  const bare = clean === "" || clean === "/";
  const title = TITLES[clean] ?? "";

  // Esc 回工作台；在输入框/对话框里按 Esc 不抢事件（那通常是取消编辑）。
  useEffect(() => {
    if (bare) return;
    const onKey = (event: KeyboardEvent) => {
      // 判定统一收敛到 shouldHandleEscape：非 Esc、已被内层消费、输入态、
      // 有浮层（dialog[open] 或带 data-overlay-open 的面板）都不跳回工作台。
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
    <div className="flex h-dvh flex-col overflow-hidden">
      <header className="flex h-11 shrink-0 items-center gap-3 border-b border-line bg-bg px-3">
        <Link
          href="/"
          title="返回工作台（Esc）"
          className="flex shrink-0 items-center gap-1.5 rounded-md border border-line-strong bg-surface px-2 py-1 text-[12px] font-medium text-muted transition-colors hover:bg-surface-2 hover:text-text"
        >
          <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="m14 6-6 6 6 6" />
          </svg>
          返回
        </Link>

        <div className="flex min-w-0 items-center gap-2">
          <span className="grid size-5 shrink-0 place-items-center rounded-md bg-[#1b2a4a]">
            <span className="size-1.5 rounded-[2px] bg-[#5eead4]" />
          </span>
          <span className="shrink-0 text-[13px] font-semibold tracking-tight">空白格</span>
          {title && (
            <>
              <span className="shrink-0 text-line-strong">/</span>
              <span className="min-w-0 truncate text-[13px] text-muted">{title}</span>
            </>
          )}
        </div>

        {NAV.length > 0 && (
          <nav className="ml-3 hidden items-center gap-0.5 md:flex">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-md px-2 py-1 text-[12px] transition-colors ${
                  clean === item.href.replace(/\/$/, "")
                    ? "bg-surface-3 font-medium text-text"
                    : "text-muted hover:bg-surface-2 hover:text-text"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        )}

        <div className="ml-auto flex shrink-0 items-center gap-2.5">
          <kbd className="hidden items-center gap-1 rounded-md border border-line bg-surface px-2 py-1 font-mono text-[10px] text-faint lg:flex">
            {shortcutHint(PALETTE_SHORTCUT)} 命令
          </kbd>
          <ModelSwitcher />
          <HealthPill />
        </div>
      </header>
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">{children}</main>
      <CommandPalette />
    </div>
  );
}
