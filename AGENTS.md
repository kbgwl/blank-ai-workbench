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

两条都不是"感觉好转"，是要出数字的：台账给三笔账，对标给五个分项。

## 改代码前

**先派代理出去学，再动手。** 每次要改内核（agent loop / 上下文 / 工具契约 / 权限 /
验证 / 界面结构）之前，开一个或多个子代理去调研同类开源项目的真实实现，产出对照 md
落到 `docs/`，然后照着它改。理由不是"多看看总没坏处"，而是今晚实测出来的：

- 一次调研换来四个可落地改动（超时转后台、压缩当历史根、审查先出确定性范围、
  驳回带反馈）；
- 它同时纠正了三处我们自己的误判（"没有成本上限"其实有、"卡片墙"其实不存在、
  opencode 的 TUI 早就不是 Go）；
- 它也标出了**不该抄**的地方——我们的计划/目标结构本就强于两家，照着"对齐"反而会降级。

给代理的指令必须包含三样，缺一样回来的就是泛泛介绍：
1. **本项目的现状基线**（相关模块路径 + 已知痛点），让它逐条对照；
2. **要源码不要博客**（能读文件就读文件，给出路径与行号）；
3. **区分事实与推测**，查不到的明确写"未找到"，不许编。

参考产出：`docs/external-reference-2026-11.md`（总报告）与同目录下五份详报。

- 动某个文件之前先读它，不要凭文件名猜。
- 改 Agent、工具权限、沙箱、文件写入路径时，必须同时补测试或说明风险（见 `CONTRIBUTING.md`）。
- 前端改动要说明验证过的浏览器与视口尺寸。

## 写验收判据

**至少一条肯定式，而且能被机器执行。** 全是否定式的判据（不许报错、不许引第三方库、
不能崩）在空壳身上天然成立——空页面永远没有控制台错误，所以这类判据的最优解就是
"什么都不做"。2026-11-20 夜班 L6 就是这么交了一个三列空框的看板，四条判据还全过。

判据要覆盖这四层，缺一层就会漏：

1. **打开时有什么**（首屏状态，不是"点了之后有"）——杀空壳；
2. **走一遍真实动作之后变没变**（填、点、拖、刷新）——杀假象；
3. **刷新/重启之后还在不在**；
4. **哪一条由谁执行**：`run_tests` / `command` / `page` / 人眼看截图，写清楚，别让模型自评。

前端交付用这个跑：

```bash
python scripts/acceptance.py --journey acceptance/kanban.json --page path/to/index.html
```

首屏不成立直接判**不可交付**，不是扣两分。写完页面自己先跑一遍，别等用户打开。

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
# 1.5 组装随包 Python 运行时（129MB，不入库；已有就跳过，--check 只体检）
node scripts/prepare_desktop_runtime.mjs
# 2. 打安装包（版本号在 desktop/package.json，唯一真源）
cd desktop && E:/env/node/node-v24.20.0-win-x64/node.exe ./node_modules/electron-builder/cli.js --win nsis
# 3. 自检 14 项（含"首页是 bot 对话页"）
cd desktop && node verify-package.cjs
# 4. 装到本机（安装目录复用 install-target.local.json）
cd desktop && node install-local.cjs
```

版本号改完跑一次 `node scripts/check_versions.mjs`：根 `package.json` 与 README 里
「当前发布版本：**x.y.z**」必须跟 `desktop/package.json` 同值，CI 的 `desktop` job 会拦。

装完想让已装客户端立刻用上新代码、但不想重装：`node self-deploy.cjs --api`（只同步后端）
或 `--web`（只同步前端），`--shell` 换掉 app.asar 里的主进程与 preload（客户端须先完全退出）。
别手改 `resources/` 下的副本——客户端读的是它，不是工作区源码。

## 工程事实

- 结构：`web/`（Next.js）、`services/api/`（FastAPI，环境变量前缀 `BLANK_`）、`packages/cli/`、`evals/`、`scripts/`。
- 后端 50 个测试文件、540+ 用例，全量跑一遍约 110 秒。
- 工作区路径必须命中白名单，返回前端前脱敏成 `<workspace:name>`。