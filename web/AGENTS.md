<!-- BEGIN:nextjs-agent-rules -->

## This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

## 前端测试

见仓库根 `AGENTS.md` 的「跑测试」与「改动之后跑什么」两节。

**不要把命令抄到这份文件里。** 本项目只注入工作区根的 `AGENTS.md`
（`services/api/src/blank_api/services/instructions.py` 的 `PROJECT_FILENAMES`），
这份 `web/AGENTS.md` 不会被任何一次运行读到；抄一份命令到这里，只会多一处在悄悄漂移、
而且没人会执行的"约定"。
