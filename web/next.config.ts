import type { NextConfig } from "next";

/**
 * 桌面壳构建（`BLANK_DESKTOP_BUILD=1`）走静态导出：
 * 没有服务器，rewrite 不生效，前端改用 `NEXT_PUBLIC_API_BASE` 直连本机后端。
 * 网页 / 开发态保持 rewrite 代理，行为不变。
 */
const desktopBuild = process.env.BLANK_DESKTOP_BUILD === "1";

const nextConfig: NextConfig = {
  // 本机常用 127.0.0.1 打开，不放行会导致 dev 资源被跨源拦截。
  allowedDevOrigins: ["127.0.0.1", "localhost"],
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
}

export default nextConfig;
