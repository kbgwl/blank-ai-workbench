import type { NextConfig } from "next";

/**
 * 桌面壳构建（`BLANK_DESKTOP_BUILD=1`）走静态导出：
 * 没有服务器，rewrite 不生效，前端改用 `NEXT_PUBLIC_API_BASE` 直连本机后端。
 * 网页 / 开发态保持 rewrite 代理，行为不变。
 */
const desktopBuild = process.env.BLANK_DESKTOP_BUILD === "1";

// 端到端门禁要同时开第二个前端实例：Next 的 dev 单实例锁与产物都挂在 distDir 上，
// 换个输出目录即可与日常 `pnpm dev` 并存，也不会覆盖开发者正在用的构建缓存。
const distDir = process.env.BLANK_WEB_DIST_DIR ?? ".next";

const nextConfig: NextConfig = {
  distDir,
  // 审批接口是同步恢复：批准后要跑完 guardian 审查 + 这次工具 + 模型下一跳，
  // 直到下一个断点或任务结束才返回。真机一次只读盘点用了 52 秒，而 Next 的
  // rewrite 代理默认 30 秒就掐断，前端只拿到一张 HTML 500，误报「后端不可用」，
  // 实际决定已落库、命令也已执行。放宽到 15 分钟，覆盖 600 秒单命令上限
  // 加上审查与模型调用的开销。
  experimental: { proxyTimeout: 900_000 },
  // 桌面式窗口里左下角常驻栏是导航区，开发浮标会盖住「设置」等入口。
  devIndicators: false,
  // 本机常用 127.0.0.1 打开，不放行会导致 dev 资源被跨源拦截。
  allowedDevOrigins: ["127.0.0.1", "localhost", "192.168.2.111"],
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

if (desktopBuild) {
  nextConfig.output = "export";
  nextConfig.images = { unoptimized: true };
} else {
  nextConfig.rewrites = async () => [
    {
      source: "/api/:path*",
      destination: `${process.env.API_PROXY_TARGET ?? "http://localhost:8000"}/api/:path*`,
    },
  ];
  // 静态导出不支持 headers()；桌面壳那边在 app:// 协议响应里补同一组。
  nextConfig.headers = async () => [
    {
      source: "/(.*)",
      headers: [
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Referrer-Policy", value: "no-referrer" },
      ],
    },
  ];
}

export default nextConfig;
