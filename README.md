# 空白格 AI 操作台

面向开发者的 AI 工程操作台：自然语言下达任务，Agent 读取和修改项目文件、执行命令与测试、
展示 Diff，高风险操作需人工审批。完整方案见 [ai-workbench-plan.md](ai-workbench-plan.md)。

当前进度：**v1.1（核心任务工作台 + 头脑风暴 + 按需功能中心 + CLI 骨架）**。REA Reverse Lab 已有独立入口，
REA MCP Adapter 已接入 `/api/v1/reverse-lab`（4 个端点走 stdio 子进程里的 `blank_api.services.rea.mcp_server`，默认开着、`BLANK_REA_DISABLE=1` 可关）；
界面还没消费这四个端点。stdio 服务端已挂 Windows Job（内存/进程数/生命周期由内核强制，见 `docs/SANDBOX-WINDOWS-DESIGN.md` §7.5），但 Job 不是文件/网络沙箱，这条链路仍不套任务的三档档位。界面是面向开发者的深色三栏操作台：
顶栏 + 左侧导航 + 三栏工作区（项目/任务 · 执行时间线 · 终端与验收），窄屏退化为上下布局。
没有场景选择器：用户只写要说的话，Agent 自己判这一句该直接回答、该只读看几处再给判断，
还是该动手改。判据见 [docs/BOT-VS-AGENT.md](docs/BOT-VS-AGENT.md)，成本控制见 [docs/COST.md](docs/COST.md)。
项目级工作约定（`.blank/harness`）、经验库与离线优化回路见 [docs/HARNESS.md](docs/HARNESS.md)。
端到端演示与评测有两条通道，界面与报告里都会标清楚用的是哪条：一条是「本地脚本化模型（演示用）」，
行为确定、可离线复现；另一条是真实供应商（当前为 DeepSeek），Key 只以密文落库、不外泄。
真实模型这一轮跑出来的数字与它的口径限制（自动化校验一条都没执行）写在下面「已知限制」里。

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
npm install -g blank-ai-cli
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

命令执行默认走沙箱，但 **Docker 是可选的加固档，不是安装前提**：探测不到可用容器后端时退回本机执行
（Windows 上由 Job Object 强制内存上限、活跃进程数上限与整棵进程树回收；文件与网络边界在设置页和
证据包里如实标注「未由系统强制」）。装了 Docker 并启动守护进程后会自动改用容器隔离，不用改配置。
要"只准容器"的部署设置 `BLANK_SANDBOX_ALLOW_LOCAL=0`——那种情况下没有容器就整任务拒绝（409，
`error.code = sandbox_tier_unavailable`），不会退回宿主执行。

想用容器档时，镜像需要先构建一次：

```bash
docker build --build-arg BASE_IMAGE=node:24-alpine -t blank-sandbox:dev sandbox
```

沙箱策略：工作区是唯一可写挂载点，默认 `--network none`，CPU/内存/PID/文件大小/时长均有上限，
超时立即 `docker rm -f` 回收整棵进程树，环境变量按白名单透传且带 KEY/TOKEN/SECRET 等特征的一律拒绝进入。
其余可调项：`BLANK_SANDBOX_IMAGE`、`BLANK_SANDBOX_ALLOW_LOCAL`、`BLANK_SANDBOX_CPUS/MEMORY/PIDS`、
`BLANK_SANDBOX_NETWORK`、`BLANK_SANDBOX_TIMEOUT`、`BLANK_SANDBOX_MAX_FILE_KB`、`BLANK_SANDBOX_TMPFS`、
`BLANK_SANDBOX_READ_ONLY_ROOT`、`BLANK_SANDBOX_ENV_ALLOWLIST`。

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
| `pnpm e2e:team` | 团队链路端到端门禁：stub 模型 + 隔离库 + 真实 HTTP + 浏览器点验（详见 `CONTRIBUTING.md`） |

项目 harness 的校验、经验库浏览与 Propose–Probe–Reflect–Compose 优化用
`python -m blank_api.services.harness.cli`，用法与边界见 [docs/HARNESS.md](docs/HARNESS.md)。

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
- `/team` 系列 — `bootstrap`、`bots` CRUD、`groups` CRUD 与 `groups/{id}/state`（锚点窗口）、群内发消息与 `stop`、
  `messages/{id}/approval`、`/team/search`（跨群全文检索）

安全口径：API Key 用 Fernet 加密落库，响应与日志只回 `api_key_hint`；工作区路径必须命中白名单，
越界返回 403 `path_escape_denied`；对前端一律脱敏成 `<workspace:name>`；高风险工具在执行前经图中断人工审批。

## 版本验收状态

| v1.1 体验层 | 状态 | 说明 |
|---|---|---|
| Codex 风格任务工作区 | ✅ | 全高任务页、右侧文件/Diff/终端/证明 Tab、低噪声布局 |
| 头脑风暴入口 | ✅ | `/brainstorm` 已接真实模型生成、模板降级和转任务 |
| 按需功能中心 | ✅ | `/features` 支持本机功能开关和依赖提示 |
| Reverse Lab 入口 | 🟡 | 后端 4 个端点已接真 REA MCP Adapter（stdio 子进程，29 条判据全绿，见 `docs/REA-WAVE4-CODEX-RUNTIME.md` §7.51）；🟡 的两格是**界面尚未消费这些端点**、**Job 只管内存/进程数/生命周期，不提供文件与网络沙箱**（§7.59） |
| CLI 基础入口 | ✅ | `blank init`、`blank doctor`、`blank add/remove`、本地工作区启动 |
| Bot 团队 `/team` | ✅ | 群聊编排、上下文管家、主 Bot 的团队管理工具、跨群全文搜索与锚点跳转 |

| 版本 | 验收要点 | 证据 |
| --- | --- | --- |
| v0.1 阶段0 工程基线 | 一条命令起全栈、健康检查、统一错误响应、迁移可建可回、前端类型检查/Lint/构建 | `pnpm regression` 全绿；`tests/` 中的健康与错误用例 |
| v0.2 阶段1 项目与工作区 | 项目/工作区 CRUD、路径白名单与越界拦截、主机路径脱敏、删项目级联清理任务与执行痕迹 | `tests/test_projects_v02.py`（含 `test_delete_project_removes_tasks_and_traces`）、`tests/test_auth_v02.py` |
| v0.3 阶段2 多模型 | 供应商 CRUD、Key 加密且不外泄、连通性测试、非流式与流式对话、token 记账 | `tests/test_models_v03.py`、`/api/v1/usage/summary` 实测 |
| v0.4 阶段3 Agent Runtime | 计划 → 执行 → 验证 → 汇总四节点、工具集、SQLAlchemy 检查点、事件流与证据包 | `tests/test_agent_v04.py` |
| v0.5 阶段4 任务控制台 | 深色三栏（任务列表 / 时间线 / 文件·Diff·证明包）、实时轮询与历史恢复、深链 | 浏览器实测创建并运行任务，时间线折叠为计划/工具/终端/审批/结果区块；382px 视口实测三栏 grid 退化为单列（`grid-cols-1`）、横向溢出 0px |
| v0.6 阶段5 审批与恢复 | 高风险操作统一走图中断：批准从断点继续并补记 `approval.granted`，驳回则整步不执行并置 `paused`；自动修复上限 3 轮；进程重启后仍可恢复 | `tests/test_approval_v06.py` 5 项 + 浏览器「批准并继续」实测 |
| v0.7 阶段6 沙箱与安全 | 一次性容器执行 `run_shell`/`run_tests`（工作区唯一可写挂载、默认无网、CPU/内存/PID/时长上限、超时强杀不留残留容器）、密钥不进容器与输出、审计与快照可回滚 | `tests/test_sandbox_v07.py`（含容器内 `run_tests` 用例）、`tests/test_permissions_v10.py`；容器内 `uname` 为 Linux 是当时装有 Docker 的机器上记录的，本机现在没有 `docker` 命令行，这一条无法就地复核（见下方已知限制）；评测 T18/T19 返回 `expected_denied` |
| v1.0 阶段7 质量与评测 | 回归门禁一条命令、评测样例集与成功率报告、成本与权限守卫 | `pnpm regression`、`evals/results/`、`tests/test_cost_v10.py` |
| v1.2 Bot 团队 | 群聊编排（每次发言落一条普通 Task）、上下文管家挑选、主 Bot 的团队管理工具与高风险审批边界、跨群全文检索与锚点跳转 | `tests/test_team.py`、`tests/test_phase2_team_admin.py`、`tests/test_phase2_context.py`、`tests/test_phase2_search.py`；`pnpm e2e:team`（stub 模型 + 隔离库 + Chrome headless 1440×900，38 项判定）；`evals/team_e2e/real_model_check.py`（DeepSeek 真模型 + 隔离实例 :8061，13 项判定，含三次高风险审批连续放行） |

已知限制（不粉饰）：

- 验收条件的「是否覆盖」由模型自报，平台无法判断语义：极端情况下会出现「文件没改但条件判 ✓」。
  时间线里的工具卡片和 Diff 才是可核对的证据，验收结论只作为模型的自我声明展示。
- 真实供应商已经接上并跑通（DeepSeek，配置里的 `deepseek-chat`，服务端回包模型名 `deepseek-flash`）：原生
  tool_calls 可用，评测样例集和工作台对话用的是真模型。Key 只以 Fernet 密文落库，接口只回 `****2ec1`
  这样的尾号提示，不进仓库、不进安装包、不进日志。团队链路的真实模型端到端判分器在
  `evals/team_e2e/real_model_check.py`，需要实例在跑时执行。
- 真实模型的团队链路**已在 2026-10-22 端到端点验通过**：`evals/team_e2e/real_model_check.py` 对隔离实例
  :8061 跑满 13 项判定、跳过 0 项，其中「拉人进群」真实挂起并连续放行了三次高风险工具
  （`team_create_bot` → `team_add_member` → `team_assign_task`），放行后成员名单真的变成
  `小白管家 / 工程 / 评审`、轮次末态 `completed`；写文件是磁盘上核对的（`file.changed` 事件 + 读回内容），
  全文检索命中成员产出，重名建 Bot 被拦截。注意这批判定用的是**新建的隔离库**，不代表用户 :8000 的数据。
- **但真实模型这一轮评测里，一行自动化测试都没跑成**：32 个样例的 `validation.status` 全是 `skipped`
  （28 个「命令执行未获授权」、4 个「没改动文件」）。原因是本机没有 Docker、`SANDBOX_ALLOW_LOCAL` 默认关闭，
  `run_shell`/`run_tests` 一律被策略拒掉。所以 `success_rate 1.0` 只能读成「任务收尾且产物文件存在」，
  不等于「测试通过」。这一轮读数照原样保留；#144 之后默认会退回本机受限执行，缺 Docker 不再导致这类跳过。
- 跳过校验时证据曾经写 `passed: true`——那是把没验过说成验过。现在这类情形记 `passed: null`，
  控制流照旧靠 `status == "skipped"` 放行（不会把模型推进修复回路空转），界面三处展示统一到
  `web/src/components/tasks/verdict.ts`，一律显示「未验证」并给出跳过原因。
  守卫见 `services/api/tests/test_validation_gate.py`、`web/src/components/tasks/verdict.test.ts`。
- 审批/提问的 5 分钟 TTL 现在只有一处判定（`services/api/src/blank_api/services/agent/evidence.py`
  里的 `approval_is_expired`），读侧
  （`/evidence`、`/team/groups/{id}/state` 的 `approval_state` 与 `expired`）和写侧（提交审批时的 409）
  共用它。此前同一个比较有三处各写各的，出现「卡片写着待审批、点下去 `approval_expired`」，界面等于
  给用户一个必然失败的按钮；现在过期项显示「已过期」并撤掉批准/驳回按钮，只能重跑这一轮。
  **一处行为收紧**：带 `ToolCall` 行的任务级审批过去压根没查 TTL，现在同样按 5 分钟拒绝。
  守卫见 `services/api/tests/test_approval_v06.py`（正反两极 + 对读侧打过变异，确认它能红）。
- 桌面包是两段式产物：必须先 `BLANK_DESKTOP_BUILD=1 next build` 生成 `web/out`，`electron-builder` 只是
  把它整份拷进 `resources/web`，**不会**自己构建前端。上一版打包漏了这一步，`/team` 页面根本没进安装包
  ——导航点进去 404，而打包日志一片正常。现在 `desktop/verify-package.cjs` 按产物断言（`team.html` 存在
  且 chunk 里有团队页文案），并检查包里不有本机开发痕迹（`.env`、`_verify/`、本地库、`.key`）与明文 Key。
  抽样文案只挑「仅该页独有」的字符串，版本号字面量那类会随改版过期，过期断言比没有更糟。
- 最近一轮真实模型评测（32 样例，并发 5）：`success_rate 1.0`、`acceptance_coverage 0.466`、平均耗时 13.99s、
  平均 47,803 tokens（合计 1,483,613 prompt + 46,078 completion）；30 成功、2 `expected_denied`（路径越界、写密钥）、
  0 失败。报告见 `evals/results/results-2026-10-22T07-43-54-994Z.json`。
- 本机没有 `docker` 命令行，容器隔离这条路径当前无法就地复核；没有容器时默认走宿主退路（LocalSandbox）：
  Windows 上内存/进程数/进程树由 Job Object 强制，文件与网络边界只靠路径闸门、审批与证据链，界面上如实标注。
- **数据库：普通用户什么都不用装。** 这里要说的是**服务端**：默认与桌面版都是单个 SQLite 文件（开发库
  `sqlite+aiosqlite:///./data/blank_dev.db`；桌面包内由壳层把 `BLANK_DATABASE_URL` 指到数据目录下的
  `blank.db`，见 `desktop/main.cjs`）。`MySQL` / `PostgreSQL` 是**可选部署形态**——形态的代价是服务端要你自己起。
  驱动侧的实话分两句，按 0.8.5 包内运行时（`desktop/dist/win-unpacked/resources/backend/runtime/Lib/site-packages`）
  实测：`pymysql` / `aiomysql` **不在包里**（挂在 `pyproject` 的 `mysql` extra，`uv sync` 默认不装）；
  `asyncpg` **在包里**（它是核心依赖，`pyproject` 第 9 行）——装了驱动不等于用了它，产品默认连的还是那个
  SQLite 文件，这一条只是「包里多一份没人调用的代码」。
  这两种方言的迁移历史、两条建库路径的 schema 对账都实测过，真 HTTP 端到端装置在 MySQL / PostgreSQL
  上各有 **83 步全绿**的实物（见 `docs/REA-WAVE4-CODEX-RUNTIME.md` §7.13–§7.29）；第 84 步（任务 #82 新增）
  **目前只有 SQLite 一格跑过**，两种方言的那一格还没补。这些不改变一句实话：单机产品用不到它们。
  方言兼容层自 2026-10-26 起**冻结**：只保留 CI 的 `api-dialect` 作为回归防线，不再开新批次。
- 沙箱镜像带 Python/Node/Git，但没有预装各语言依赖，也没有 Go 工具链：需要 `npm install` 或 Go 的样例仍跑不出真实结果。
- `.github/workflows/ci.yml` 已就位，但本机没有 `gh` 命令行，无法核对 Actions 是否真的跑过；仓库本身已是 git 仓库。
- `services/api/tests/test_parallel_tools_v14.py::test_read_only_calls_overlap` 有墙钟依赖：它要求两个 0.25s 的
  只读工具在 0.45s 内跑完。electron-builder 正在压 `7za` 的那一轮全量里它红过一次，同一份代码空机重跑两次
  都绿、单跑也绿（695 passed / 14 skipped）。当时截了 `tail`，断言原文没留下，所以**这条偶发的根因没有定论**。
  打包时不要并行跑回归。

## 路线图

- v0.1 工程基线（阶段 0）✅
- v0.2 项目与工作区管理（阶段 1）✅
- v0.3 多模型接入（阶段 2）✅
- v0.4 Agent Runtime 与基础工具（阶段 3）✅
- v0.5 任务控制台（阶段 4）✅
- v0.6 审批、恢复与自动修复（阶段 5）✅
- v0.7 沙箱与安全加固（阶段 6）✅（无 Docker 时退回宿主进程，见「已知限制」）
- v1.0 质量、评测与发布（阶段 7）✅
