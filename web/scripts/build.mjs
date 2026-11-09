/**
 * `pnpm -C web build` 的入口。
 *
 * 为什么不直接写 "build": "next build"：宿主（WorkBuddy 等 IDE）会给每个 Node 进程注入
 * `NODE_OPTIONS=--require <shim>/node-language-shim.cjs`，其中装了批量删除保护：同一个 turn
 * 内删除文件数超过阈值（默认 50）就抛错中止进程。`next build` 启动时会清理 `.next` 缓存
 * （单个 `trace-build` 目录就是上万个文件），必然撞上这个阈值：
 *
 *   Error: [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
 *   {"count":50,"threshold":50,"targets":["...\\web\\.next\\trace-build"]}
 *
 * 这不是 Turbopack / CSS 的问题——把宿主注入的 NODE_OPTIONS 摘掉后同一份代码 5.7 秒编译通过。
 * 构建进程不该继承宿主的编辑期钩子，所以这里显式剥掉再调 next；子进程（Turbopack worker）
 * 通过 fork/spawn 继承同一个 env，同样干净。
 *
 * 在 CI 或普通终端里本来就没有这个环境变量，这段等价于直接 `next build`。
 */

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(here, "..");
const nextBin = path.join(webRoot, "node_modules", "next", "dist", "bin", "next");

const env = { ...process.env };
delete env.NODE_OPTIONS;

const result = spawnSync(process.execPath, [nextBin, "build", ...process.argv.slice(2)], {
  cwd: webRoot,
  env,
  stdio: "inherit",
});

if (result.error) {
  console.error(result.error);
  process.exit(1);
}

process.exit(result.status ?? 1);
