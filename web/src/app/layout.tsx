import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { SceneProvider } from "@/components/scenes/scenes";

export const metadata: Metadata = {
  title: "空白格 AI 操作台",
  description: "面向开发者的可审计 AI 工程执行台",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body className="h-dvh overflow-hidden bg-bg text-text">
        <SceneProvider>
          <AppShell>{children}</AppShell>
        </SceneProvider>
      </body>
    </html>
  );
}
