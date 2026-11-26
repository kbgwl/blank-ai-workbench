"use client";

import { useEffect } from "react";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <html lang="zh-CN">
      <body style={{ margin: 0, minHeight: "100vh", display: "grid", placeItems: "center", background: "#0b0b0c", color: "#f5f5f5", fontFamily: "system-ui, sans-serif" }}>
        <main style={{ width: "min(32rem, calc(100% - 3rem))", padding: "1.5rem", border: "1px solid #303033", borderRadius: "0.75rem", background: "#161618" }}>
          <p style={{ margin: 0, fontSize: "0.75rem", letterSpacing: "0.08em", color: "#9a9aa0" }}>应用没能启动</p>
          <h1 style={{ margin: "0.5rem 0", fontSize: "1.25rem" }}>界面根组件出错了</h1>
          <p style={{ margin: "0.5rem 0 1rem", fontSize: "0.875rem", color: "#b8b8bd" }}>先重试；如果还是失败，请重新打开应用。</p>
          <details style={{ fontSize: "0.75rem", color: "#b8b8bd" }}>
            <summary style={{ cursor: "pointer" }}>技术详情</summary>
            <pre style={{ maxHeight: "10rem", overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{error.message}</pre>
          </details>
          <button type="button" onClick={reset} style={{ marginTop: "1rem", padding: "0.4rem 0.8rem", border: 0, borderRadius: "0.375rem", background: "#4c7dff", color: "#fff", cursor: "pointer" }}>重试</button>
        </main>
      </body>
    </html>
  );
}
