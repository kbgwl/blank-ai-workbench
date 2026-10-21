import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { ModelProvider } from "@/components/model";

export const metadata: Metadata = {
  title: "空白格 bot",
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
