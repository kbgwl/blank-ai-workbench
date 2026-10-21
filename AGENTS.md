# 空白格 AI 操作台 · 项目约定

本文件由 `services/api/src/blank_api/services/instructions.py` 作为 project 层注入每次运行的
system prompt。用户级 `~/.blank/AGENTS.md` 优先级更低，本次任务的 skill 指令优先级更高。
任何一层都不能放宽安全、审批或验收规则。

## 跑测试

本地是 Windows，`python` 不在 PATH，`uv` 不一定可用。后端测试直接用仓库内的虚拟环境：

```bash
cd services/api && .venv/Scripts/python.exe -m pytest -q
```

Linux / CI 上用 `pnpm test:api`（内部是 `uv run pytest -q`）。

前端测试与类型检查**不要依赖 `pnpm`**：应用拉起的后端进程里 `PATH` 往往没有 pnpm，
`where node` / `dir node_modules\.bin` 这类探测一条都问不出来。直接用绝对路径的 node：

```bash
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe --test "src/**/*.test.ts"
cd web && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/typescript/bin/tsc --noEmit
```

两条命令都实测过（43 例单测全过、tsc 无输出即通过）。工具链位置就这些，别再用
`where` / `dir` / `--version` 逐个试——那是每次几十次调用换不出一条结论。

## 量成本与比质量

```bash
# token 台账：读本机客户端库（只读打开），按"钱的去处"分类
python scripts/token_ledger.py
python scripts/token_ledger.py --compare 2026-10-21T06:00,2026-10-21T12:00

# 同题对标：同一批 fixture、同一套判分，跑我们和别家
node evals/compare.mjs --only T01,T02,T03 --agents ours,claude
```

口径分别写在 `docs/COST.md`（成本）与 `docs/QUALITY.md`（质量）。
两条都不是"感觉好转"，是要出数字的：台账给三笔账，对标给五个分项。

## 改代码前

- 动某个文件之前先读它，不要凭文件名猜。
- 改 Agent、工具权限、沙箱、文件写入路径时，必须同时补测试或说明风险（见 `CONTRIBUTING.md`）。
- 前端改动要说明验证过的浏览器与视口尺寸。

## 不要做的事

- 不要提交 `.env`、API Key、`data/*.db`、`logs/`、`_backup/`、`_verify/` 里的产物。
- 不要为了「让它通过」而放宽 `services/api/src/blank_api/services/safety.py`、
  `permissions.py`、`sandbox/policy.py` 里的限制。
- 不要在结论里把模型自报的验收覆盖度当成事实；可核对的证据是工具卡片、Diff 和测试输出。

## 打包桌面版

```bash
# 1. 前端静态导出（必须带这两个变量，否则构建出来的是要在服务器上跑的那份）
cd web && BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 \
  E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/next/dist/bin/next build
# 2. 打安装包（版本号在 desktop/package.json）
cd desktop && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/electron-builder/cli.js --win nsis
# 3. 自检 14 项（含"首页是 bot 对话页"）
cd desktop && node verify-package.cjs
# 4. 装到本机（安装目录复用 install-target.local.json）
cd desktop && node install-local.cjs
```

装完想让已装客户端立刻用上新代码、但不想重装：`node self-deploy.cjs --api`（只同步后端）
或 `--web`（只同步前端）。别手改 `resources/` 下的副本——客户端读的是它，不是工作区源码。

## 工程事实

- 结构：`web/`（Next.js）、`services/api/`（FastAPI，环境变量前缀 `BLANK_`）、`packages/cli/`、`evals/`、`scripts/`。
- 后端 50 个测试文件、540+ 用例，全量跑一遍约 110 秒。
- 工作区路径必须命中白名单，返回前端前脱敏成 `<workspace:name>`。