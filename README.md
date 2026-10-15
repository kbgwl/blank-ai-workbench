# 空白格 AI 操作台

面向开发者的 AI 工程操作台：自然语言下达任务，Agent 读取和修改项目文件、执行命令与测试、
展示 Diff，高风险操作需人工审批。完整方案见 [ai-workbench-plan.md](ai-workbench-plan.md)。

当前进度：**v1.1（核心任务工作台 + 头脑风暴 + 按需功能中心 + CLI 骨架）**。REA Reverse Lab 已有独立入口，真实 REA 适配器仍在后续接入。界面是面向开发者的深色三栏操作台：
顶栏 + 左侧导航 + 三栏工作区（项目/任务 · 执行时间线 · 终端与验收），窄屏退化为上下布局。
系统提供六个默认工作场景：开发、文档、研究、头脑风暴、逆向分析、日常。日常场景支持随手分享和用户确认后的本地习惯记忆；场景会影响任务提示词、默认验收条件和后续工具策略，用户可以在顶栏切换。
所有端到端演示与评测都跑在明确标注的「本地脚本化模型（演示用）」上：本机没有可用的真实供应商 Key，
界面里的结论、Diff 与 token 用量都来自这条确定性通道。

## 目录结构

```text
web/              Next.js 16 + React 19 + Tailwind 4 前端
services/api/     FastAPI + SQLAlchemy 2 (async) + Alembic 后端
docker-compose.yml  PostgreSQL 16 + Redis 7
scripts/dev.mjs   一条命令启动全栈
.github/workflows CI（前端类型检查/构建、后端测试、迁移往返）
```

## 环境要求

- Node.js ≥ 22，pnpm 10.4.1（`corepack enable pnpm && corepack prepare pnpm@10.4.1 --activate`）
- Python 通过 [uv](https://docs.astral.sh/uv/) 管理，无需预装系统 Python
- Docker Desktop（提供 PostgreSQL 与 Redis）

## 启动

```bash
pnpm install
pnpm dev
```

目标分发方式：发布 CLI 后，用户可以直接安装并启动本地工作台：

```bash
npm install -g @blank-ai/cli
blank
```

CLI 尚未发布到 npm 前，也可以安装本地包：

```bash
npm install ./packages/cli/dist/blank-ai-cli-0.1.0.tgz
```

本地开发 CLI：

```bash
node packages/cli/src/index.mjs doctor
node packages/cli/src/index.mjs
```

`pnpm dev` 依次执行：`docker compose up -d --wait` → `alembic upgrade head` →
uvicorn（:8000）→ next dev（:3000）。打开 http://localhost:3000 即可看到后端健康状态卡片。
前端通过 Next 的 rewrite 代理 `/api/*`，目标地址由 `API_PROXY_TARGET` 决定，默认 `http://localhost:8000`；
后端换端口时必须同步设置，例如 `API_PROXY_TARGET=http://127.0.0.1:8050 pnpm dev:web`。

没有真实模型 Key 时，起本地脚本化模型服务并注册为供应商，即可跑通完整 Agent 链路：

```bash
node scripts/mock-model-server.mjs        # http://127.0.0.1:8099/v1
node scripts/register_mock_provider.mjs   # 写入「本地脚本化模型（演示用）」
```

配置读取后端 `services/api/.env`，变量名见 [.env.example](.env.example)，前缀 `BLANK_`。
默认连接 Docker PostgreSQL：`postgresql+asyncpg://blank:blank_dev_password@localhost:5432/blank`；
不想起 Docker 时可改用 `sqlite+aiosqlite:///./data/blank_dev.db`。

命令执行默认走 Docker 沙箱（`BLANK_SANDBOX_ENABLED` 默认为 true），首次需要构建镜像：

```bash
docker build --build-arg BASE_IMAGE=node:24-alpine -t blank-sandbox:dev sandbox
```

沙箱策略：工作区是唯一可写挂载点，默认 `--network none`，CPU/内存/PID/文件大小/时长均有上限，
超时立即 `docker rm -f` 回收整棵进程树，环境变量按白名单透传且带 KEY/TOKEN/SECRET 等特征的一律拒绝进入。
其余可调项：`BLANK_SANDBOX_IMAGE`、`BLANK_SANDBOX_CPUS/MEMORY/PIDS`、`BLANK_SANDBOX_NETWORK`、
`BLANK_SANDBOX_TIMEOUT`、`BLANK_SANDBOX_MAX_FILE_KB`、`BLANK_SANDBOX_TMPFS`、`BLANK_SANDBOX_READ_ONLY_ROOT`、
`BLANK_SANDBOX_ENV_ALLOWLIST`。

Windows 注意：`uvicorn --reload` 的监视器在本机偶发停止热重载，改代码后若行为没变请重启进程；
子进程事件循环已用线程兜底（`services/tools/shell.py`），不要再依赖 Selector 循环。

## 常用命令（仓库根目录）

| 命令 | 作用 |
| --- | --- |
| `pnpm dev` | 启动前端 + 后端 + PostgreSQL + Redis |
| `pnpm test:api` | 后端 pytest |
| `pnpm typecheck:web` | 前端类型检查 |
| `pnpm lint:web` | 前端 ESLint |
| `pnpm build:web` | 前端生产构建 |
| `pnpm regression` | 回归门禁：PostgreSQL 迁移往返 + pytest + 前端类型检查/构建（结果写入 `evals/regression/`） |
| `pnpm regression --evals` | 追加评测样例集 |
| `pnpm evals` | 仅跑评测，`node evals/run_evals.mjs --provider <id> --concurrency 5` |

数据库迁移（首次需 `pnpm install` 且 Docker 已启动）：

```bash
cd services/api
uv run alembic upgrade head      # 应用迁移
uv run alembic downgrade base    # 回滚
uv run alembic revision --autogenerate -m "描述"
```

## API

统一前缀 `/api/v1`，错误响应固定为 `{"error": {"code": ..., "message": ...}}`。逐字段说明见
[tasks-api.md](docs/tasks-api.md)，部署与运行形态见 [deployment.md](docs/deployment.md)。

- `GET /health` — 服务与数据库连通性
- `/projects` 系列 — 项目 CRUD、工作区绑定与激活、文件树与预览（`tree`、`file`）
- `/providers` 系列 — 模型供应商 CRUD、`presets`、`test` 连通性、`chat` 与 `chat/stream`；`/usage/summary` 汇总 token
- `/tasks` 系列 — 建任务、`run`、`events`（增量）、`evidence`（交付证明包）、`diff`、`approval`、`cancel`

安全口径：API Key 用 Fernet 加密落库，响应与日志只回 `api_key_hint`；工作区路径必须命中白名单，
越界返回 403 `path_escape_denied`；对前端一律脱敏成 `<workspace:name>`；高风险工具在执行前经图中断人工审批。

## 版本验收状态

| v1.1 体验层 | 状态 | 说明 |
|---|---|---|
| Codex 风格任务工作区 | ✅ | 全高任务页、右侧文件/Diff/终端/证明 Tab、低噪声布局 |
| 头脑风暴入口 | ✅ | `/brainstorm` 已接真实模型生成、模板降级和转任务 |
| 按需功能中心 | ✅ | `/features` 支持本机功能开关和依赖提示 |
| Reverse Lab 入口 | 🟡 | `/reverse-lab` 已完成工作台入口和重建任务转换，REA MCP Adapter 尚未接入 |
| CLI 基础入口 | ✅ | `blank init`、`blank doctor`、`blank add/remove`、本地工作区启动 |

| 版本 | 验收要点 | 证据 |
| --- | --- | --- |
| v0.1 阶段0 工程基线 | 一条命令起全栈、健康检查、统一错误响应、迁移可建可回、前端类型检查/Lint/构建 | `pnpm regression` 全绿；`tests/` 中的健康与错误用例 |
| v0.2 阶段1 项目与工作区 | 项目/工作区 CRUD、路径白名单与越界拦截、主机路径脱敏、删项目级联清理任务与执行痕迹 | `tests/test_projects_v02.py`（含 `test_delete_project_removes_tasks_and_traces`）、`tests/test_auth_v02.py` |
| v0.3 阶段2 多模型 | 供应商 CRUD、Key 加密且不外泄、连通性测试、非流式与流式对话、token 记账 | `tests/test_models_v03.py`、`/api/v1/usage/summary` 实测 |
| v0.4 阶段3 Agent Runtime | 计划 → 执行 → 验证 → 汇总四节点、工具集、SQLAlchemy 检查点、事件流与证据包 | `tests/test_agent_v04.py` |
| v0.5 阶段4 任务控制台 | 深色三栏（任务列表 / 时间线 / 文件·Diff·证明包）、实时轮询与历史恢复、深链 | 浏览器实测创建并运行任务，时间线折叠为计划/工具/终端/审批/结果区块；382px 视口实测三栏 grid 退化为单列（`grid-cols-1`）、横向溢出 0px |
| v0.6 阶段5 审批与恢复 | 高风险操作统一走图中断：批准从断点继续并补记 `approval.granted`，驳回则整步不执行并置 `paused`；自动修复上限 3 轮；进程重启后仍可恢复 | `tests/test_approval_v06.py` 5 项 + 浏览器「批准并继续」实测 |
| v0.7 阶段6 沙箱与安全 | 一次性容器执行 `run_shell`/`run_tests`（工作区唯一可写挂载、默认无网、CPU/内存/PID/时长上限、超时强杀不留残留容器）、密钥不进容器与输出、审计与快照可回滚 | `tests/test_sandbox_v07.py`（含容器内 `run_tests` 用例）、`tests/test_permissions_v10.py`；实测容器内 `uname` 为 Linux、验证命令为 `python3 -m pytest -q`；评测 T18/T19 返回 `expected_denied` |
| v1.0 阶段7 质量与评测 | 回归门禁一条命令、评测样例集与成功率报告、成本与权限守卫 | `pnpm regression`、`evals/results/`、`tests/test_cost_v10.py` |

已知限制（不粉饰）：

- 验收条件的「是否覆盖」由模型自报（脚本化模型只看测试是否通过），平台无法判断语义：极端情况下会出现
  「文件没改但条件判 ✓」。时间线里的工具卡片和 Diff 才是可核对的证据，验收结论只作为模型的自我声明展示。
- 真实供应商 Key 在本机返回 401，端到端演示与评测全部依赖脚本化模型，行为是确定性的但不等于真实模型质量。
- `run_shell` 与 `run_tests` 默认在 `blank-sandbox:dev` 容器内执行（已实测：容器内 `uname` 为 Linux、
  测试命令为 `python3 -m pytest -q`）；但本机没有 Docker 或镜像缺失时会自动退回宿主进程（LocalSandbox），
  那条退路只保证时长与进程回收，不承诺隔离。
- 沙箱镜像带 Python/Node/Git，但没有预装各语言依赖，也没有 Go 工具链：需要 `npm install` 或 Go 的样例仍跑不出真实结果。
- 仓库尚未初始化 git，`.github/workflows/ci.yml` 已就位但没有实际运行过。
- 最近一轮评测（32 样例，并发 5，命令执行走 Docker 沙箱）：`success_rate 0.875`、`acceptance_coverage 0.913`、
  平均耗时 3.6s、平均 3,880 tokens；26 成功、2 `files_missing`（脚本化模型不会新建文件）、
  2 `failed`（Go 夹具）、2 `expected_denied`（越界与写密钥）。报告见 `evals/results/`。

## 路线图

- v0.1 工程基线（阶段 0）✅
- v0.2 项目与工作区管理（阶段 1）✅
- v0.3 多模型接入（阶段 2）✅
- v0.4 Agent Runtime 与基础工具（阶段 3）✅
- v0.5 任务控制台（阶段 4）✅
- v0.6 审批、恢复与自动修复（阶段 5）✅
- v0.7 沙箱与安全加固（阶段 6）✅（无 Docker 时退回宿主进程，见「已知限制」）
- v1.0 质量、评测与发布（阶段 7）✅
