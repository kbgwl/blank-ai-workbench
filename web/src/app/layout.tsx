import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { ModelProvider } from "@/components/model";

export const metadata: Metadata = {
  // 静态导出后 Electron 窗口标题会取 HTML <title>，它覆盖 main.cjs 里的 title。
  // 桌面端应用名统一为 Blank AI，改名字时这里和 main.cjs 的 APP_NAME 要一起改。
  title: "Blank AI",
  description: "说清楚要什么，它读代码、改代码、跑命令——每一步都留在对话里",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body className="h-dvh overflow-hidden bg-bg text-text">
        <ModelProvider>
          <AppShell>{children}</AppShell>
        </ModelProvider>
      </body>
    </html>
  );
}
