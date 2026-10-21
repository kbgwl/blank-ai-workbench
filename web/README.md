# 空白格 Web 前端

Next.js 16（App Router，静态导出）+ React 19 + Tailwind 4。

## 常用命令

```bash
pnpm -C web dev      # 开发服务器
pnpm -C web test     # 单元测试（node --test，跑 src/**/*.test.ts）
pnpm -C web lint     # ESLint
pnpm -C web build    # 生产构建
```

根目录也提供了 `pnpm test:web`、`pnpm lint:web`、`pnpm build:web`、`pnpm typecheck:web`。

## 代码约定

- 纯逻辑（快捷键匹配、命令过滤、格式化）放 `src/lib/`，用 `node --test` 覆盖，不引渲染库。
- 测试文件里 import 需写全 `.ts` 后缀（Node ESM 解析要求），`tsconfig.json` 已开
  `allowImportingTsExtensions`。
- 全局快捷键的匹配与「输入态让路」判定统一走 `src/lib/shortcuts.ts`，
  不要在组件里各写一份。
- 页面跳转入口集中在 `src/lib/commands.ts`，新增页面时同步补一条命令。

## 快捷键

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl+K` / `⌘K` | 打开命令面板（任意页面，含输入态） |
| `↑` `↓` | 在命令面板里移动高亮 |
| `Enter` | 打开选中命令 |
| `Esc` | 关闭命令面板；非首页时返回工作台 |

---

以下为 create-next-app 原始说明，保留备查。

This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.
