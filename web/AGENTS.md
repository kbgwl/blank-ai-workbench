<!-- BEGIN:nextjs-agent-rules -->

## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## 前端测试

- 纯逻辑放 `src/lib/`，用 `pnpm -C web test`（`node --test`）覆盖，不引渲染库。
- 因 Node 的 ESM 解析要求写全后缀，测试文件里 import 写 `./xxx.ts`，
  对应 `tsconfig.json` 已开 `allowImportingTsExtensions`。
- 回归门禁 `pnpm regression` 已包含前端单测、类型检查与构建三步。
