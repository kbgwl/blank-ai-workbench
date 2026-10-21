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

## 改代码前

- 动某个文件之前先读它，不要凭文件名猜。
- 改 Agent、工具权限、沙箱、文件写入路径时，必须同时补测试或说明风险（见 `CONTRIBUTING.md`）。
- 前端改动要说明验证过的浏览器与视口尺寸。

## 不要做的事

- 不要提交 `.env`、API Key、`data/*.db`、`logs/`、`_backup/`、`_verify/` 里的产物。
- 不要为了「让它通过」而放宽 `services/api/src/blank_api/services/safety.py`、
  `permissions.py`、`sandbox/policy.py` 里的限制。
- 不要在结论里把模型自报的验收覆盖度当成事实；可核对的证据是工具卡片、Diff 和测试输出。

## 工程事实

- 结构：`web/`（Next.js）、`services/api/`（FastAPI，环境变量前缀 `BLANK_`）、`packages/cli/`、`evals/`、`scripts/`。
- 后端 47 个测试文件、500+ 用例，全量跑一遍约 90 秒。
- 工作区路径必须命中白名单，返回前端前脱敏成 `<workspace:name>`。