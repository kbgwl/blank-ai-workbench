"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { HealthPill } from "@/components/health-pill";
import { ModelSwitcher, SceneSwitcher } from "@/components/scenes/scenes";

/** 首页是满幅工作台（左栏自带品牌与导航），其余页面共用一条顶栏。 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  // 静态导出后首屏 URL 是 /index.html（或 /index），开发态是 /，都要认成首页。
  const clean = (pathname ?? "").replace(/\.html$/, "").replace(/\/index$/, "");
  const bare = clean === "" || clean === "/" || clean === "/tasks";

  if (bare) {
    return <div className="h-dvh overflow-hidden">{children}</div>;
  }

  return (
    <div className="flex h-dvh flex-col overflow-hidden">
      <header className="flex h-11 shrink-0 items-center justify-between border-b border-line bg-bg px-4">
        <div className="flex items-center gap-3">
          <div className="grid size-6 place-items-center rounded-md bg-[#1b2a4a]">
            <span className="size-2 rounded-[2px] bg-[#5eead4]" />
          </div>
          <div className="text-[13px] font-semibold tracking-tight">空白格</div>
        </div>
        <div className="flex items-center gap-3">
          <SceneSwitcher />
          <ModelSwitcher />
          <HealthPill />
        </div>
      </header>
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">{children}</main>
    </div>
  );
}
