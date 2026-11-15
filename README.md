# 空白格 AI 操作台

面向开发者的 AI 工程操作台：自然语言下达任务，Agent 读取和修改项目文件、执行命令与测试、
展示 Diff，高风险操作需人工审批。

当前发布版本：**0.11.2**（真源是 `desktop/package.json`，安装包文件名与「关于」都取这一处；
`pnpm check:versions` 在 CI 里钉住 README 与根 `package.json` 不许各说各话）。

本版重点：**v0.11.2 收口——审批身份、读图链路与自动修复都说真话**。

不是新功能，是把「看起来通过」逐条换成「真的对得上」：

- **审批身份闭环**：校验通道与跨恢复续跑同样按 `approval_id + tool_call_id + tool_name + arguments_hash`
  四元匹配，错一个就 409，不再留「旧卡复用」的口子。
- **读图不再静默降级**：`deepseek-flash` 被正确认出支持图片，附件随 `/turns` 直接进上下文；
  不支持时才明说原因，不再显示成「没有同意」。
- **卡住时说清楚**：任务超时一次打印 13 项现场（任务/运行/派发/供应商/最近事件/解释器/租约…），
  不再只给一句「超时」。
- **触发器边界**：暂停/取消后的状态与按钮一致，watch 根目录之外永不放行。
- **自动修复说真话**：修不好时不再报成功，`verdict` 按实际复核结论给。
- **构建与测试入口**：`web/scripts/build.mjs` 与 `services/api/conftest.py` 清掉宿主批量删除钩子
  造成的假红，原样命令恢复 `EXIT=0`。
- **代理变量不再炸掉所有模型调用**：`NO_PROXY` 里带方括号 IPv6（`[::1]`，curl 的写法）时 httpx 会
  把它拼成非法 URL，构造客户端直接抛 `InvalidURL`。入口处把方括号归一（httpx 自己会给裸地址补回），
  绕过语义不变；回归用真子进程钉住，摘掉归一那行即红。

上一版本：**v0.11.1 通知通道——「到点了」终于真的会弹出来**。

上一版把「会自己醒」修通了（定时到点、盯着工作区、桌面自起跑），但醒来之后**没有任何东西告诉用户**：
任务确实跑了，结果只在界面里，用户切走就看不见。这一版补上最后一段：

- **原生系统通知**：桌面壳直接弹 Windows toast，点一下唤回窗口并跳到那条对话（`?thread=` 深链）。
- **判定仍在后端，壳只负责投递**：要不要打扰由 `agent/notify.py` 判（策略 + 免打扰），结论连同理由
  写进 `notify.decided` 事件；`GET /api/v1/notifications` 只吐 `disturb=true` 的那几条。壳不复判——
  两套规则各判一次，迟早会漂成「设置里说静默、屏幕却在响」。
- **文案按来源分档**：定时起的弹「到点了」，盯梢的弹「有变化了」，审批弹「等你批准」（并出声）。
  同一句「任务完成」扣在定时提醒上，读起来就不像提醒。
- **三条降噪**：跨轮询按 id 去重（秒级游标必然重读）、窗口正被看着就不弹、一分钟超过 3 条先压着
  （审批例外——压着等于让任务白等）。
- **免打扰终于有入口**：设置 → 执行与智能体 →「通知与免打扰」，填 `22:00-08:00`（跨零点也行）。
  免打扰只压「做完/有变化」，审批永远响。

历史版本：**v0.11.0 Super Agent 补齐「会自己醒」**——定时真的到点（不再登记一条永远不响的
待触发）、「盯着工作区」有了真实的事件生产者（每拍扫一次文件 mtime）、到点在桌面形态下自己起跑
（以前只入队、没有 worker 认领，任务永远 `queued`）、企业微信/钉钉/飞书做成配置项（设置 → 消息通道，
填完即用），以及自动更新接上真实下载与摘要校验（默认不碰网络，须配 `BLANK_UPDATE_FEED_URL`）。

更早：**v0.10.0 Autonomous Super Agent**——Chat Supervisor + Goal Tracker 多轮目标、
Autonomous Planner / Task Dispatcher / 独立 Verifier、测试失败自动修复闭环、后台持续巡检、
三种自动化模式与审批矩阵、顶部状态条与 §15 可观测读数。

再上一版：**v0.9.1 Super Agent Completion**——真实 Electron GUI 18 项闭环、coding-035 口径修复、
后台任务暂停/恢复/重试、审批同步、模型/场景切换与日常记忆端到端验证。
更早：**v0.9.0 Super Agent Upgrade**（Router/Worker、事件触发、Watchdog、断点恢复、低打扰交付）。

当前进度：**v1.1（核心任务工作台 + 头脑风暴 + 按需功能中心 + CLI 骨架）**。REA Reverse Lab 已有独立入口，
REA MCP Adapter 已接入 `/api/v1/reverse-lab`（4 个端点走 stdio 子进程里的 `blank_api.services.rea.mcp_server`，默认开着、`BLANK_REA_DISABLE=1` 可关），
`/reverse-lab` 页面已消费这四个端点（适配器状态 / 候选目标 / 字符串线索 / 带台账的调查，见 `web/src/app/reverse-lab/page.tsx`）。
stdio 服务端已挂 Windows Job（内存/进程数/生命周期由内核强制），但 Job 不是文件/网络沙箱，这条链路仍不套任务的三档档位。界面是面向开发者的深色三栏操作台：
顶栏 + 左侧导航 + 三栏工作区（项目/任务 · 执行时间线 · 终端与验收），窄屏退化为上下布局。
场景选择器只改三件事（`services/agent/scenarios.py`）：系统提示末尾附加一段场景规则、
按场景禁用工具（只读场景把 apply_patch/run_shell/run_tests/git 从注册表与执行层同时拦下）、
记忆开关（只有「日常」场景且用户选择记住时开放 `memory_write`）。
它**绝不向用户的正文拼任何前缀**——用户只写要说的话，Agent 自己判这一句该直接回答、
该只读看几处再给判断，还是该动手改。
项目级工作约定（`.blank/harness`）、经验库与离线优化回路由 `python -m blank_api.services.harness.cli` 提供。
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

> Python 依赖只针对**源码运行**：Windows 安装版/便携版自带完整 Python 运行时与后端依赖
> （`desktop/build/runtime`，首启自动迁移数据库），不要求机器上装过 Python。
> 源码模式下仍需 uv 或项目 `.venv`——这一点在桌面打包验收（§9.3）中已实测确认。

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
要"只准容器、AppContainer 或受限令牌"的部署设置 `BLANK_SANDBOX_ALLOW_LOCAL=0`——那种情况下都不可用就整任务拒绝
（409，`error.code = sandbox_tier_unavailable`），不会退回宿主执行。

非管理员的 Windows 上还有第三个后端：`BLANK_SANDBOX_RESTRICTED=1` 打开**受限令牌**执行（默认关）。
启用后先跑一次行为自检——同一个受限子进程里「合成私钥读不到」且「工作区读写成功」，外加宿主令牌、
授权后、去掉限制 SID 三条对照；全绿才会被选中，任何一条对照红掉整次判定作废。之后文件边界由 NTFS
真的拒（读 `~/.ssh`、`~/.aws` 等受保护路径失败），内存/进程数/进程树仍由 Job 强制；网络没有
AppContainer 就拦不住，账本固定写 `network_enforced=false`。可写/可读根与保护路径由规则文件定义
（`BLANK_SANDBOX_RULES_FILE`，不设则用包内 `rules.default.json`：默认拒绝、拒绝任何覆盖 `~/.ssh`
的授权、认不出的字段或占位符直接报错）。

需要管理员权限的 Windows 上可以再上一层：`BLANK_SANDBOX_APPCONTAINER=1` 让命令跑进**零能力
AppContainer**（固定包名 `BlankSandbox`）。工作区/临时目录授 `M`、工具链根授 `RX`，祖先路径不授权
（AC 令牌的 `SeChangeNotifyPrivilege` 绕过遍历检查，实测足够）。准入同样先跑行为自检：同一个 AC
子进程里「合成私钥读不到」「外连 1.1.1.1:443 被 `PermissionError:13` 拒」「回环能 bind 且自连」
「工作区读写成功」，再加宿主令牌对照（读得到私钥、连得上外网）；任一条不成立就不选它，账本写
`appcontainer_unverified`，不会从"能进 AC"推断边界。文件与网络两条边界都由内核拒，账本
`filesystem_enforced` / `network_enforced` 为 true 只因为这份行为证据。`workspace-net` 档在 AC 下
**拒绝**而不是降级（零能力包的方向与"要联网"相反）。命令经 `cmd /d /s /c` 执行，路径型首词会统一
补引号——未引号的绝对路径 exe 在 AC 里会被 cmd 的搜索解析撞成「拒绝访问。」（同一命令在宿主与受限
令牌后端正常）。IO 读写带宽上限由 Job Object 强制（`BLANK_SANDBOX_IO_BPS`，默认 `128m`，
整棵进程树、所有卷合计；Job 接受设置不算数，`_probe_io_rate` 现场写两轮盘证明节流后账本才是
`io_enforced=true`，docker 档没有带宽参数所以恒 false）。沙箱**临时区**（TEMP/TMP；本机后端
还设 `BLANK_SCRATCH`）的容量上限由独立固定容量 VHDX 卷强制（`BLANK_SANDBOX_DISK_MB`，默认 2048，0=关；卷默认放
`%ProgramData%\blank-quota`，可用 `BLANK_SANDBOX_DISK_ROOT` 改；挂载需要管理员）。探针挂一个
64MB 卷写满再追加，必须拿到内核 `errno=28 ENOSPC` 且第二次写仍失败，账本才写 `disk_enforced=true`；
docker 档没有容量参数所以恒 false。**共享工作区仍不设容量上限**（`disk` 只承诺临时区与任务根，见
CHANGELOG「未验证」第 3 项）。

默认关闭的**任务级隔离**（`BLANK_TASK_ISOLATION=1`，管理员 + AppContainer）给每个任务一个独立可写根：
`git clone --shared`（不是 `git worktree`——共享对象库可写等于给任务改历史的钥匙）→ 叠上未提交改动 →
提交基线 → 收紧 ACL 成「宿主 + 本任务包」两方可见。命令与文件工具都换进该任务唯一包执行（文件工具走
一次性 broker：请求/响应落在只授本任务包的 IPC 目录），收尾先按保守规则 `harvest` 合并改动再删副本/卷/包。
建根前先跑 AC 行为自检并确认后端确为 AppContainer，不绿或会被 Docker/受限令牌抢走时整任务拒绝，不建
「没人能进的副本」。`BLANK_TASK_DISK_MB` 给任务根一个固定容量 VHDX 卷（0=关；写满由内核返回 ENOSPC）。

想用容器档时，镜像需要先构建一次：

```bash
docker build --build-arg BASE_IMAGE=node:24-alpine -t blank-sandbox:dev sandbox
```

沙箱策略：工作区是唯一可写挂载点，默认 `--network none`，CPU/内存/PID/文件大小/时长均有上限，
超时立即 `docker rm -f` 回收整棵进程树，环境变量按白名单透传且带 KEY/TOKEN/SECRET 等特征的一律拒绝进入。
其余可调项：`BLANK_SANDBOX_IMAGE`、`BLANK_SANDBOX_ALLOW_LOCAL`、`BLANK_SANDBOX_RESTRICTED`、
`BLANK_SANDBOX_APPCONTAINER`、`BLANK_SANDBOX_RULES_FILE`、`BLANK_SANDBOX_CPUS/MEMORY/PIDS`、
`BLANK_SANDBOX_IO_BPS`、`BLANK_SANDBOX_DISK_MB`、`BLANK_SANDBOX_DISK_ROOT`、`BLANK_SANDBOX_NETWORK`、
`BLANK_TASK_ISOLATION`、`BLANK_TASK_DISK_MB`、
`BLANK_SANDBOX_TIMEOUT`、`BLANK_SANDBOX_MAX_FILE_KB`、`BLANK_SANDBOX_TMPFS`、
`BLANK_SANDBOX_READ_ONLY_ROOT`、`BLANK_SANDBOX_ENV_ALLOWLIST`。

Windows 注意：`uvicorn --reload` 的监视器在本机偶发停止热重载，改代码后若行为没变请重启进程；
子进程事件循环已用线程兜底（`services/tools/shell.py`），不要再依赖 Selector 循环。

## 常用命令（仓库根目录）

| 命令 | 作用 |
| --- | --- |
| `pnpm dev` | 启动前端 + 后端 + PostgreSQL + Redis |
| `pnpm test:api` | 后端 pytest |
| `pnpm test:web` | 前端单测（`node --test`） |
| `pnpm test:desktop` | 桌面壳单测（101 例，只读仓库文件，不装 electron） |
| `pnpm test:evals` | 评测判分器的纯函数单测（`evals/lib/judgement.mjs`） |
| `pnpm check:versions` | 版本口径一致性（`desktop/package.json` 为真源） |
| `pnpm typecheck:web` | 前端类型检查 |
| `pnpm lint:web` | 前端 ESLint |
| `pnpm build:web` | 前端生产构建 |
| `pnpm regression` | 回归门禁：PostgreSQL 迁移往返 + pytest + 前端类型检查/构建（结果写入 `evals/regression/`） |
| `pnpm regression --evals` | 追加评测样例集 |
| `pnpm evals` | 仅跑评测，`node evals/run_evals.mjs --provider <id> --concurrency 5` |
| `pnpm eval:gate` | 评测成功率门禁（退出码 0/1/2，2 表示"今天没法判"） |
| **评测环境要求** | `evals` / `eval:gate` 含 Go 样例（T11/T28）：`run_tests` 识别 `go.mod→go test`，本机没有 Go 工具链时这 2 条按 `skipped` 收尾（`validation.completed` 带缺工具链理由、不空转修复回路）；但**样例最终仍会 `failed`**——脚本化桩的固定交付文案声称「测试通过」，被 `no_false_claim` 拦下。装 Go 后才会真跑并转绿（详见 `evals/gate_config.json` 出处说明） |
| `pnpm e2e:team` | 团队链路端到端门禁：stub 模型 + 隔离库 + 真实 HTTP + 浏览器点验（详见 `CONTRIBUTING.md`） |
| `pnpm runtime:desktop` | 组装随包分发的 Python 运行时（约 129MB，不入库；`--check` 只体检） |
| `pnpm verify:desktop` | 打包后自检 14 项，按包内产物对账 |

项目 harness 的校验、经验库浏览与 Propose–Probe–Reflect–Compose 优化用
`python -m blank_api.services.harness.cli`（`--help` 看用法）。

数据库迁移（首次需 `pnpm install` 且 Docker 已启动）：

```bash
cd services/api
uv run alembic upgrade head      # 应用迁移
uv run alembic downgrade base    # 回滚
uv run alembic revision --autogenerate -m "描述"
```

## API

统一前缀 `/api/v1`，错误响应固定为 `{"error": {"code": ..., "message": ...}}`。

- `GET /health` — 服务与数据库连通性
- `/projects` 系列 — 项目 CRUD、工作区绑定与激活、文件树与预览（`tree`、`file`）
- `/providers` 系列 — 模型供应商 CRUD、`presets`、`test` 连通性、`chat` 与 `chat/stream`；`/usage/summary` 汇总 token
- `/tasks` 系列 — 建任务、`run`、`events`（增量）、`evidence`（交付证明包）、`diff`、`approval`、`cancel`
  - `evidence` 分两段读：摘要类事件全量 + 时间线尾部窗口（默认 200 条，`timeline_limit` 最大 2000，
    用 `timeline_before_seq` 往前翻）；只要摘要走 `/tasks/{id}/evidence/summary`，返回体里的
    `timeline_meta` 会说清"总共多少条、这次装了哪些、还有没有更早的"
- `/maintenance` 系列 — `storage`（artifact / 快照 / 事件 / 用量四类占用与可回收量）、
  `purge`（分 `artifacts` / `snapshots` / `events` / `orphans` 四段，**默认 dry-run**）、
  `metrics`（进程内计数：模型超时 / 重试 / 切备用、worker 接管、SSE 连接与轮询、产物写入与压缩；
  进程重启归零，跨重启的历史在事件表与用量表里）
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
| Reverse Lab 入口 | ✅ | 后端 4 个端点已接真 REA MCP Adapter（stdio 子进程，29 条判据全绿）；前端 `/reverse-lab` 三栏真调这四个端点（挑目标 / 抽线索 / 出结论 + 台账），策略层 8 例见 `web/src/components/reverse-lab/api.test.ts`。🟡 仅剩一格：**Job 只管内存/进程数/生命周期，不提供文件与网络沙箱**，界面右下角如实标注 |
| CLI 基础入口 | ✅ | `blank init`、`blank doctor`、`blank add/remove`、本地工作区启动 |
| Bot 团队 `/team` | ✅ | 群聊编排、上下文管家、主 Bot 的团队管理工具、跨群全文搜索与锚点跳转 |
| Claude 式编程工具面 | ✅ | `edit_file` 精确串替换 + `write_file` 整文件写入，两工具与 `apply_patch` 共用「读后写」硬闸门（没读过不许改、读过被改过要重读）；shell 写文件与 git 完整性硬拦截；独立只读调用默认并行。判据 `services/api/tests/test_claude_parity_v24.py`（60 例），变异 8/8 红 |
| 场景模式对齐 Claude Harness | ✅ | 驳回不再暂停整轮（该步记未执行、模型换个做法继续）；工具回执直接续跑、不再每步塞合成「请继续」；后台子代理结束时注入一次性系统通知（结论由主代理转述）；`update_plan` 同时只允许一个 `in_progress`；系统提示补齐 act / 推荐 / 系统提醒边界 / 专职工具优先。判据 `services/api/tests/test_claude_loop_parity_v27.py`（6 例），变异 7/7 红 |
| Claude 工具面 Glob / Grep / ListAgents | ✅ | 新增 `glob`（`**/` 命中根部、花括号、mtime 倒序、扫描预算）；`search_code` 补齐 `output_mode` / `glob` / `type` / `-i` / `multiline` / `-A/-B/-C` / `-o` / `-n` / `head_limit` / `offset`；fs-broker 与任务级隔离转发同一套参数；新增 `list_agents` 列出后台子代理状态。判据 `services/api/tests/test_claude_tool_surface_v28.py`（21 例），变异 8/8 红 |
| 子代理默认后台与续聊 | ✅ | `spawn_agent` 不传参即在后台跑（`run_in_background=false` 才同步等）；任务收尾先给后台子代理 90 秒窗口，结束的结论写进交付提示词、仍在跑的显式取消并点名；新增 `send_message` 续聊（保留上一轮读/写/结论，新 `spawn_agent` 是全新上下文；隔离子代理与仍在跑的句柄如实拒绝）。判据 `test_claude_subagent_default_v29.py`（6 例）+ `test_claude_send_message_v30.py`（9 例），变异 8/8 与 10/10 全红 |
| fork 子代理（Agent(fork)） | ✅ | `spawn_agent(subagent_type="fork")` 以当前会话的分身运行：继承派发这一刻的完整对话与系统提示（剪掉尚未拿到回执的那次工具交换）、复用父会话的 `PermissionPolicy` 与工具表（减去派发类与 REPL/LSP/workflow、场景禁用），子网关沿用父网关的场景文件类型闸门与 `write_root`；只能前台，显式 `run_in_background=true` 直接拒绝，`spawn_agents` 并行批也拒绝；拿不到父对话 fail-closed；`subagent.started/completed` 带 `fork` 标记，续聊保留 fork 工具面。remote 形态冻结（理由与重开条件见「已知限制」）。判据 `services/api/tests/test_claude_fork_v30.py`（15 例），变异 11/11 红 |
| 后台 shell | ✅ | `run_shell(run_in_background=true)` 立即返回 `shell-…` 句柄，命令在后台真的跑；宿主路径边跑边流式写日志（系统临时目录、2MB 上限、读回尾部 64KB），结束时在模型调用前注入一次性系统通知（不轮询、不 sleep）；`task_output` / `task_stop` 统一句柄名 `task_id`（旧名 `agent_id` / `shell_id` 继续接受）；`task_stop` 与任务收尾都按进程树杀（cmd 外壳下的孙进程不会残留）、日志删除，等审批那一档不杀；子代理上下文禁用后台命令。判据 `services/api/tests/test_background_shell_v31.py`（20 例），变异 21/21 红 |
| 任务级工作树会话 | ✅ | `enter_worktree` 用 `name` 在 `<repo>/.blank-worktrees/<name>` 新建 `blank/worktree-<name>` 分支，或用 `path` 进入 `git worktree list` 里已注册的树，之后本任务所有工具（shell cwd、文件工具、git、安全策略路径基准）都换到那棵树；`exit_worktree(action=keep|remove)` 回到原工作区：`remove` 只对本任务创建的树生效、`path` 进入的只能 keep，遇未提交改动/未合并提交默认拒绝并逐文件点名，显式 `discard_changes=true` 才丢弃；审批恢复后仍在同一棵树，任务收尾只摘记录不删树。判据 `services/api/tests/test_worktree_tools_v32.py`（34 例），变异 28/28 红 |
| Skill 工具 | ✅ | 系统提示只列「名字 + 一句话说明」（取正文首行、剥前导 markdown、截到 120 字符），模型判断任务属于某个 Skill 时调用 `skill(skill, args)` 按需加载正文；参数替换 `$ARGUMENTS`、无占位符则附在正文末尾；名字必须精确命中，前导斜杠直接拒绝并给出修正名，未知名字 fail-closed 并列出真实清单；同一任务里重复调用只回一行「已加载」、不再贴正文；没有可加载的 Skill 时 `skill` 从工具表里整条移除（子代理同口径）；子代理拿目录副本，加载状态不写回父会话。Skill frontmatter 的 `agent` / `background` 已于 #303 补齐，派发闸门于 #312 收口为 `context: fork`：只有声明了 `context: fork` 才把正文作为 goal 派发子代理，`agent` 选子代理类型（缺省 general-purpose）、`background: true` 后台派发并返回句柄（`status="forked"`、`background=True`、`agent_id`）；没写 `context: fork` 时即使写了 `agent` / `background` 也保持主线程注入正文；未知类型 fail-closed 并列出可用类型，无 `subagent_runner` 明确报错。判据 `services/api/tests/test_skill_tool_v33.py`（30 例，含真实 HTTP 任务运行）+ `services/api/tests/test_skill_agent_background_v303.py`（29 例）+ `services/api/tests/test_command_context_fork_v312.py`（34 例），变异 24/24 + 4/4 + 6/6 红 |
| ReportFindings 工具 | ✅ | `report_findings(level?, findings[])` 按 Claude 语义补齐：只在当前生效的代码审查说明点名时使用，一批上报、按严重程度从高到低、最多 32 条，空数组表示没有发现通过核验；每条必填 `file`（仓库相对路径）/`summary`/`failure_scenario`，可选 `line`（从 1 起）、`short_summary`（≤60）、`category`（≤40，kebab-case）、`verdict`（CONFIRMED/PLAUSIBLE）、`outcome`（fixed/skipped/no_change_needed，只用于修复后重报）；任一条非法即整批拒绝并点名第几条，未知字段剥掉、顺序保留；回执一行（`已上报 N 条审查发现。`），明确要求不要再把同一批发现以正文打印。只读、可并行、计划模式放行、不进子代理工具表（由主代理统一上报）；清单同时落 `review.findings` 事件与工具产物（沿用打码/gzip/上限）。判据 `services/api/tests/test_report_findings_v34.py`（36 例，含真实 HTTP 任务运行），变异 28/28 红 |
| 定时工具（Cron + Wakeup） | ✅ | `cron_create / cron_delete / cron_list / schedule_wakeup` 对齐 Claude CronCreate/Delete/List 与 ScheduleWakeup：5 段本地 cron，日/星期同时受限按 Vixie 语义取 OR；`recurring=false` 只触发一次、到点自动结束；重复任务 7 天后结束（期限从排定那一刻算，不随重排往后滚）；抖动确定性（重复最多晚周期的 10%、封顶 900 秒，一次性落在 :00/:30 最多早 90 秒）；`durable=false` 只活到本次进程结束（启动时作废，避免重启后凭空补跑），`durable=true` 跨重启保留并在到点后 24 小时内补跑一次；第一次到点才建任务（prompt 即 goal），后续每一跳沿同一条任务续跑；绑定任务正忙时顺延 60 秒再看，不把这次触发吞掉；执行路径仍只有 `launch_task` 一条。`schedule_wakeup` 的 delaySeconds 夹到 [60,3600]，stop 结束循环，noop 连击折叠。四个工具常驻；`cron_list` 只读、可并行、计划模式放行，子代理工具表不含。判据 `services/api/tests/test_cron_tools_v35.py`（28 例，含到点放行/重排/忙时顺延/启动清理），变异 26/26 红 |
| Workflow 工具（脚本即编排） | ✅ | `workflow` 对齐 Claude Workflow 契约：脚本必须以 `export const meta = { name, description, phases? }` 纯字面量开头（变量/调用/插值拒绝），正文用 `agent() / parallel() / pipeline() / phase()`；脚本跑在 Node 子进程（`--permission --allow-fs-read=<temp> --disable-proto=throw`），只走 stdout/stdin JSON 行；`agent()` 复用既有后台子代理，`parallel` 保序、`pipeline` 维度内串行维度间并发、`phase` 必须命中 `meta.phases`，`args` 传真正的 JSON 值，`schema` 要求 JSON 并做 type/required/properties 骨架校验；最多 50 个子代理、同时在飞 4 个；脚本不能写文件、不能起进程，网络入口在预置层替换为报错；high 风险走审批且只在用户明确要求多代理编排（workflow / 工作流 / fan out / ultracode 等）时允许调用；启动即返回 `wf_` 句柄，`task_output` / `task_stop` 认该前缀，结束时注入一次系统通知，任务收尾（等审批除外）取消仍在跑的运行；`scriptPath` 优先于 `script`/`name`，脚本幂等落到 `.blank/workflows/.runs/` 并写入 `.git/info/exclude`；`resumeFromRunId` 仅本进程、按 `(prompt, options)` 哈希续跑；缺 Node 报 `skipped` + `data.missing_command=node` 并接入停滞检测。判据 `services/api/tests/test_workflow_tool_v36.py`（50 例，含真实 Node 编排/边界/缓存/收尾），变异 39/39 红 |
| NotebookEdit 工具（按单元格编辑） | ✅ | `notebook_edit(notebook_path, cell_id?, new_source, cell_type?, edit_mode?)` 对齐 Claude NotebookEdit：`.ipynb` 用单元格视图读（`<cell id="...">` 标签，read_file 默认渲染、`offset/limit` 仍回原始 JSON 行）；`replace` 按 id 换源（可顺带改类型，代码→markdown 清空执行字段、反向补默认值）、`insert` 放在目标格之后（省略 `cell_id` 插最前，自动生成 id）、`delete` 删格；老 notebook 没有真实 id 时合成 `cell-<序号>` 只用于定位、不写回；同样过「没读过不许改、被改过要重读」硬闸门（与 `edit_file`/`write_file`/`apply_patch` 同一套 size+mtime+sha256），写前备份、写回保留原缩进与 CRLF、8MB 上限，非法 JSON/缺少单元格/无实际变更都点名拒绝；工作区改写（逆向场景落分析区）、计划模式拦截、父任务变更记录全部按既有写入路径接上。判据 `services/api/tests/test_notebook_edit_v37.py`（30 例，含读渲染/闸门/CRLF/接线），变异 38/38 红 |
| Monitor 工具（逐行事件流） | ✅ | `monitor(description, command, timeout_ms)` 对齐 Claude Monitor 的 command 变体：命令仍过 safety（`policy_gate("run_shell")`）、shell allowlist 与沙箱档位，不是绕开执行边界的旁路；每条 stdout/stderr 行是一个事件（按字节切行再解码，多字节字符被 chunk 切开不会变乱码；空行不投递），命令没结束就能取到事件，结束/超时/取消/`task_stop` 都只以最后一条事件收尾；事件在下一次模型调用前作为系统通知注入（明确「不是用户发言、不要轮询、不要 sleep」），每次最多 25 行，积压留在队列下一轮继续，队列上限 200 行、只丢最旧并如实上报丢弃数；`task_output` / `task_stop` 认 `mon-` 句柄（与 `shell-`/`wf_` 同一张表、同一套日志与收尾）；子代理直接拒绝，文档/逆向/日常/头脑风暴场景按命令工具一并禁用；手写 allow/deny 规则、高风险动作分类、缺命令停滞检测、占位内容审计也都覆盖 monitor。判据 `services/api/tests/test_monitor_tool_v38.py`（20 例，含真实进程逐行/跨块/边跑边投/积压），变异 17/17 红（附带一致性改动 4/4 红） |
| REPL 工具（持久 JavaScript 组合） | ✅ | `repl(code, description?, timeout?)` 对齐 Claude REPL 契约：脚本跑在每任务一个的持久 Node 子进程里，密封 `vm` 上下文（没有 `require`/`process`/`import`），变量跨调用保留、支持顶层 await、最后一条表达式即返回值、`console.log/error` 分别回 stdout/stderr；工具按本地名字当异步函数用，每次内层调用回宿主走同一条 `ToolGateway.execute(..., inner=True)`（场景禁用/安全策略/权限/审计/沙箱一个不少）；会话/编排类工具不注入、场景排除的工具也不注入；需要审批的内层调用直接拒绝（REPL 里弹不出审批卡）；`timeout` 默认 30000ms、上限 600000ms，同步死循环由 `vm` 超时打断且会话保留，异步挂起由宿主截止时间杀掉进程并清空状态；缺 Node 报 `skipped` + `data.missing_command=node`；没有网关（子代理、非 runtime 入口）直接拒绝；任务收尾摘会话，等审批那一档保留。判据 `services/api/tests/test_repl_tool_v39.py`（36 例，含真实 Node 持久/密封/超时/内层工具），变异 16/16 红 |
| 任务清单 Task*（依赖图 + 钩子） | ✅ | `task_create / task_get / task_update / task_list` 对齐 Claude Task* 契约：任务落盘（默认 `~/.blank/tasks/<task_id>/`，`BLANK_TASKS_DIR` 可改根；团队任务按 group_id 共享一份），id 是十进制数字、单调递增，删除后不复用（高水位单独记，崩溃丢了水位文件也会在删除最高号时抬回去），列表按数字 id 升序而不是文件名字典序；`addBlocks` / `addBlockedBy` 写双向引用、已存在的去重、删任务时把别处对它的引用一并清掉；`metadata` 按键合并、值为 null 删键；`task_list` 隐藏 `metadata._internal` 为真的任务、把已完成阻塞者从 `blockedBy` 里过滤；`task_created` / `task_completed` 是可阻断钩子（创建被拒把刚建的任务删回去，完成被拒任何字段都不落盘）；团队任务标 `in_progress` 未带 owner 时自动署名。写盘走 scratch + `os.replace`，同目录 asyncio 锁 + 独占创建，id 只认 `[A-Za-z0-9_-]{1,64}`（路径穿越直接拒绝）。判据 `services/api/tests/test_task_tools_v40.py`（33 例，含并发创建/坏文件/崩溃窗口/真实子进程钩子），变异 16/16 红 |
| MCP 资源四工具（列举/读取/目录/热刷新） | ✅ | `list_mcp_resources / read_mcp_resource / read_mcp_resource_dir / refresh_mcp_tools` 对齐 Claude Code 2.1.271：没声明 `capabilities.resources` 的服务端连 `resources/list` 都不发，没声明目录扩展的连非标准 `resources/directory/read` 都不发（「没能力」与「能力坏了」分开说）；列举跟 `nextCursor` 翻页，目录首页 `InvalidParams`＝不是目录、后续页坏掉保留前几页、页数上限 20 并回报 `truncated`；一家服务端失败只进 `data.errors`，全部有能力服务端都失败时明确报错而不是说 `No resources found`；读取文本原样返回，二进制落系统临时目录（不进用户工作区）并给路径，非法 base64 如实标「未保存」，`-32601` / `-32002` 文案与 Claude 一致（后者只在声明目录能力时补 `ReadMcpResourceDir` 提示）；刷新只重查已连接客户端、绝不重拨，连过但失败报 `not_connected`，同名工具就地换定义并保留 `tool_search` 激活状态、只摘本服务端前缀、场景禁用回放。四个工具默认按需（`shouldDefer` 同口径），三读可并行、refresh 不在。判据 `services/api/tests/test_mcp_resources_v41.py`（34 例，含真实 stdio 子进程/分页/目录损坏/二进制落盘/运行时注入），变异 37/37 红 |
| LSP 工具（九操作代码智能） | ✅ | `lsp(operation, filePath, line, character, query?)` 对齐 Claude 的单工具九操作：位置 1-based（只在发请求处减一次），`workspaceSymbol` 用 `query`；结果文本逐字对齐（单条定义 `Defined in`、多条 `Found N definitions:` 各占一行、单条引用 `Found 1 reference:`、多引用/工作区符号按文件空行分组、hover `Hover info at l:c:`、documentSymbol 两空格递进缩进 + `- Line N`、调用层级 `Found N incoming/outgoing call(s):` 与 `[calls at: l:c]`/`[called from: l:c]`），8 条空结果文案照抄；`resultCount`/`fileCount` 按 Claude 口径（definition/implementation/references/workspace 按有效条目与唯一 URI，hover 1/1，documentSymbol 递归符号数且 fileCount 只有 0/1，call hierarchy 按条目与各端点唯一）；定义/引用/实现/工作区符号四类结果先过 `git check-ignore`（每批 50、5s 超时，不是仓库就不过滤）；文件上限 10,000,000 字节、提示用 ceil MB；文件不存在/不是文件（含目录）直接拒绝，运行时失败（初始化、超时、畸形数据）回 `Error performing <op>: ...` 而不是抛异常，没配服务端回 `No LSP server available for file type: .ext`。语言服务器按扩展名懒启动（`lsp.json`，`BLANK_LSP_FILE` 可改；认 `extensionToLanguage` 与 `extensions` 两种写法，写坏一条只跳过该条），stdio 走 `Content-Length` 帧，服务端反向 `workspace/configuration` 按项回 `null`；每次请求前全文 `didChange`（不装文件 watcher，永不读到旧内容）；一台起不来会记住失败、不在每次调用重拉；每任务一个管理器、任务收尾（含等审批）关进程。默认按需（`tool_search` 拉出）、可并行、子代理工具表不含。判据 `services/api/tests/test_lsp_tool_v42.py`（40 例，含真实 stdio 假服务端/九操作/失败缓存/注入/收尾关闭），变异 34/34 红 |
| 交互问答 AskUserQuestion | ✅ | `request_user_input(questions[])` 对齐 Claude AskUserQuestion：1-4 题、每题至多 4 个选项（下界不卡，事实题可以没有选项）、header 截到 12 字、question 截到 500 字、选项 label/description 截到 120 字；`multiSelect` 与 `preview` 互斥（多选时 preview 一律丢掉），preview 只收片段（拒绝 `html/body/script/iframe` 等整页与脚本标签、注释、代码围栏，≤4000 字），单选「默认/推荐」每题只留一个（多选保留全部），显式 `default` 必须是已列出的 label（多选可逗号分隔）；回执 `{answers: {问题文本→答案}, response?, annotations?}`，多选答案列表按 ", " 合并，注解按问题文本或 header 归位，`notes` 以「备注：」进模型、`response` 以「补充：」进模型、preview 不回贴；问题卡前端多选可切换、自由文本可覆盖、选中项带 preview 时按纯文本 `<pre>` 渲染（不进 innerHTML），每题都有推荐项才给「按默认继续」。判据 `services/api/tests/test_ask_goal_skills_v43.py`（51 例，含真实 HTTP 多选/注解/自由文本往返），变异 14/14 红 |
| 会话目标提案 ProposeGoal | ✅ | `propose_goal(condition, requiresConfirmation?)` 对齐 Claude ProposeGoal：condition ≤500 字（超出直接拒绝而不是截断），`requiresConfirmation` 缺省为真、只有显式 `false` 才直接设置（本机没有确认通道时如实回 `unanswered`，不假装用户点过头）；确认走普通问题卡（id 合成 `goal1`、header「会话目标」、选项「设为本次会话目标 / 不用」、刻意不设默认值——不答就是不设置）；接受后把目标写回会话（`threads.session_goal`，2000 字截断），下一轮起每轮从库重读并注入「以下是用户在本次会话中确认的目标」（明确不得覆盖安全/审批/工作区边界）；自由文本答案当作用户改写后的目标接受，拒绝或空答不落任何东西；`GET/DELETE /threads/{id}/session-goal` 可读可清，`goal.set` 事件留痕。判据同文件，变异 14/14 红 |
| 技能提案 ProposeSkills | ✅ | `propose_skills(proposals[])` 对齐 Claude ProposeSkills：一次最多 3 条，每条 `kind` 为 `new` 或 `improvement`、`name`/`description`/`skillMd`（≤20000 字）必填、`evidence` ≤20 条；`new` 落成 `skills.status="proposed"` 的新行，`improvement` 必须 `target` 命中本任务已加载过正文的 Skill（没加载过就不让提改进），否则整批拒绝并点名第几条；frontmatter 的 `description` 等进 `metadata` 留审计；`POST /skills/{id}/accept` 采用：`new` → `draft`（不自动启用），`improvement` 把旧正文 append 进 `history`、`version+1`、换成提案正文并删掉提案行；非提案 409 `skill_not_proposed`、缺 target 409 `skill_proposal_invalid`、target 不存在 409 `skill_target_not_found`；技能页新增「待采用的提案」分区（采用 / 丢弃，丢弃二次确认）。判据同文件，变异 14/14 红 |
| 文件型子代理定义（`.blank/agents`） | ✅ | `spawn_agent` / `spawn_agents` 的 `subagent_type` 对齐 Claude 的 `.claude/agents`：项目层 `<workspace>/.blank/agents/**/*.md` + 用户层 `~/.blank/agents`（`BLANK_USER_AGENTS_DIR` / `BLANK_USER_INSTRUCTIONS_DIR/agents` 可改），同名项目胜、同层深层文件覆盖浅层；frontmatter 的 `name` / `description` 必填（无 frontmatter 或无 name 静默跳过，声明了 name 但校验失败必须点名），name 不得以 `-` 开头、NFKC 后不得含 `:`；正文即子代理专用说明（≤20000 字，超出截断并标注），只能补充做法、不得放宽规则/审批/工作区边界；`tools` / `disallowedTools` 按 Claude 的 `fyn` + `Wd` + `VW` 口径解析：逗号串或列表、空格与逗号都切、括号内保持一条、数组里的非字符串项丢弃并告警；字段缺省或 `tools:"*"` 表示不限，显式 `null` / 假值表示「没有工具」；`disallowedTools` 里的 `*` 与上游一致归一成 `undefined`（等同不写这条限制）；非法类型按「没有可用工具」fail-closed；`maxTurns` 必须是正整数（非法忽略并告警），没显式传 `max_steps` 时作为步数上限（受子代理全局 8 步约束）；`skills` 按 Claude 的 `OI` 解析（缺省与显式 null 都是空表，`*` 保留成字面名字而不是通配符），派发前把每个 Skill 正文按 `formatSkillLoadingMetadata` 头（`<command-message>` / `<command-name>` / `<skill-format>`）拼成一条 user 消息、排在 goal 之前；名字没找到只告警 `Skill '<x>' specified in frontmatter was not found`、不阻断派发（前端事件 `skill.preload.failed`），预加载成功落 `skill.preloaded`；预加载即记为已加载，子代理再调 `skill` 工具返回「已加载」、不重贴正文；续聊扫历史里的同名预加载头，已经给过的不再重复；`subagent.started` 带 `agent_skills`；工具池只与只读/隔离基线取交集，定义里写 `run_shell` 也不会让非隔离子代理拿到写工具；未知类型整批拒绝并列出可用名字，不传就是内置 `general-purpose`；运行时每任务在最终运行根上发现一次，只把自定义类型（名字 + 一句话说明）列进系统提示，发现结果与每个子代理实际拿到的工具表分别落 `agents.loaded` / `subagent.started` 事件，续聊保留原定义与工具边界；`permissionMode` / `isolation` / `background` 按 Y 批生效——声明档只相对派发基线收紧（隔离副本基线 `accept_edits` 可被 `plan` 压过，默认档基线压不过 `acceptEdits` / `bypassPermissions`；上游允许从 default 父档升到 acceptEdits，本机收窄为只收紧，见下方偏离），`manual` 当 `default` 别名，`auto` / `dontAsk` 上游合法但本机无对应审批档（告警 + 进 `agent_ignored`），生效/忽略落 `permission.mode.applied|ignored`、`subagent.started` 带 `agent_permission_mode`；`isolation: worktree` 压过调用方 `isolate:false`，`remote` 未移植（按本地跑并告警）；`background: true` 压过调用方 `run_in_background:false`（`false` 等同没写）；插件 agent 的名字回退文件名、描述回退 `when_to_use` → `when-to-use` → `Agent from <plugin> plugin`，`permissionMode` / `hooks` / `mcpServers` 只告警不生效、非法 `isolation` 静默丢弃；`model` / `hooks` / `effort` / `memory` 已生效（`model` 见 #315 条：`inherit` / 空串 / fork 都回落到父会话模型，续聊沿用第一轮；`effort` 走与主代理同一套档位裁决；`hooks` 见 #316 条：解析期原样保留事件名（含 `Stop`），派发时在 SubagentStart 之后注册进子代理自己的作用域、`Stop` 改写为 `SubagentStop`、`disableAllHooks` 下不复活、续聊保留），`memory` 见 #317 条：声明 `user` / `project` / `local` 时派发前建目录，把 `# Persistent Agent Memory` 段与内联 `MEMORY.md` 注入子代理系统提示（三条 scope 指导语逐字保留、空文件给空态文案），并给一个只能写本作用域 `MEMORY.md` 的 `memory_write` 工具（低风险、目录固定在闭包里、无路径参数；显式 `disallowedTools` 仍可禁掉；fork 形态不建目录）；`project` → `<workspace>/.blank/agent-memory/<name>`、`local` → `<workspace>/.blank/agent-memory-local/<name>`、`user` → `~/.blank/agent-memory/<name>`（`BLANK_USER_AGENT_MEMORY_DIR` 可改），名字按上游 `nl` 消毒（非 `[A-Za-z0-9-_]` → `-`，空串 → `unknown`）；条目 300 字上限、同句去重、文件超 12000 字丢最旧并留截断标记；审计落 `subagent.memory_enabled`（scope / 打码目录 / 字符数）与 `subagent.memory_failed`；非法 scope 只告警丢弃、不拒绝整份定义（插件 agent 同口径）；续聊保留作用域，`mcpServers` / `color` / `initialPrompt` 仍只解析不生效、逐条进 `agent_ignored` 审计。判据 `services/api/tests/test_agent_defs_v44.py`（62 例，含真实运行时发现/提示/按类型派发/续聊、工具规格解析、Skill 预加载、Y 批审批档收紧与 worktree/background 强制）与 `services/api/tests/test_plugins_v49.py`（30 例，含插件 agent 字段回退与忽略口径），Y 批变异 13/13 红（此前 23/23 见 X 批条目），`hooks` 见 `services/api/tests/test_agent_hooks_v316.py`（20 例），变异 14/14 红；`memory` 见 `services/api/tests/test_agent_memory_v317.py`（28 例），变异 20/20 红 |
| 文件型 Skill 定义（`.blank/skills`） | ✅ | 对齐 Claude 的 `.claude/skills`：一目录一 Skill（`<name>/SKILL.md`，文件名大小写不敏感），项目层 `<workspace>/.blank/skills` + 用户层 `~/.blank/skills`（`BLANK_USER_SKILLS_DIR` / `BLANK_USER_INSTRUCTIONS_DIR/skills` 可改），同名项目胜；调用名就是目录名，frontmatter 的 `name` 只作显示名；没有 SKILL.md 的目录静默跳过，`synced` 保留目录、超 1 MiB、非 UTF-8 读失败逐个点名且不拖垮其余 Skill；frontmatter 坏掉（坏 YAML / 未闭合 / 非映射）按 Claude 的口径**降级而不是跳过**——字段全丢、名字回落到目录名、说明回落到正文首行，并进告警；`description` 缺省时同口径回落、清单里截到 120 字；正文超过 20000 字截断并标注；`allowed-tools` / `disallowed-tools`（含 `disallowedTools` 别名）按 Claude 的 `fyn` + `Wd` 口径解析后进审计，不改变工具池——逗号与空格都是分隔符、括号内不切（`Bash(git add, git commit)` 算一条）、数组里的非字符串项丢弃并告警、出现 `*` 整体归一成 `["*"]`；`allowed-tools` 里的 `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}` 按 Claude 的 `Iot` 口径替换成 Skill 目录与工作区根（反斜杠统一成 `/`，`disallowed-tools` 不替换），插件来源的 Skill 正文额外展开 `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}`（对齐上游 `C9`，发现时 mkdir 副作用对齐 `Bwe`），非插件来源保持字面量；`disable-model-invocation` 双闸生效——不进模型清单、`skill` 工具拒绝并提示不要绕道复刻；`user-invocable` 只挡用户 `/名字` 入口（S3 起生效），与模型侧无关；`paths` 按 Claude 的 `conditionalSkills` 生效（AA 批）：有 paths 的 Skill 先不进清单、`skill` 工具按名调用会被拒（报错里的「当前可用」不泄漏名字），文件工具（`read_file` / `edit_file` / `write_file` / `apply_patch` / `notebook_edit`）成功碰到匹配路径后，下一次模型调用前注入「现在可以调用」的系统提醒并落 `skills.activated`；归一化按上游 `y1r`（字符串按逗号拆、`{}` 内不拆、列表拍平、非字符串丢弃并告警、尾部 `/**` 去掉、空或全 `**` 为无条件），匹配按 `p1r` 的语义做 gitignore 近似（`**` 跨目录、`*` / `?` 不跨、无 `/` 的 pattern 任意层级、`!pattern` 取反、绝对路径与含 `..` 的段一律不匹配）；激活幂等且由工具台账反推（审批恢复重放检查点即可复原），执行节点把刚跑完的台账并进本次 `ask` 的视图，同一轮里读完文件接着调 `skill` 也能立刻解锁；只要目录里还有一个模型可调用的 Skill（含条件项）`skill` 工具就不从工具表拿掉，子代理 frontmatter 的 `skills:` 预加载 `include_conditional` 绕过闸门；`shell` 按上游 `Osn` 解析（trim + 小写后精确匹配 `bash` / `powershell`，`null` / 空串按未声明、非法值告警丢弃），正文里的 `!` 块按声明的解释器执行（未声明时仍走 host shell，与上游「默认 bash」的偏离见下方）；`fallback: true` 的薄指针按上游 `q9` / `v2e` 在合并层让位给同后缀的插件技能（丢弃并落 `skills.fallback_shadowed` 事件；本机没有 bundled / MCP prompt 技能链路，只认 `plugin:` 来源）；`model` / `effort` / `hooks` / `mcpServers` / `lspServers` 等未支持字段进 `ignored`；合并顺序为库 → 用户级 → 项目级，且「模型不可调用」的同名项不能遮住可调用的那一份；运行时每任务在最终运行根上发现一次，发现结果落 `skills.loaded`，系统提示只列模型可调用项，正文等 `skill` 工具调用再加载；子代理拿目录拷贝，没有一个模型可调用时连 `skill` 工具一起拿掉。判据 `services/api/tests/test_skills_disk_v45.py`（39 例，含真实运行时发现/提示/加载/工具移除、子代理门控、两个占位符与工具规格分词）与 `services/api/tests/test_skill_paths_v51.py`（25 例，含五步真实运行时端到端与 `skills.activated` 载荷），变异 28/28 + 12/12 红 |
| 自定义斜杠命令（`.blank/commands`） | ✅ | 对齐 Claude 的 `.claude/commands`：一个 md 一条命令，嵌套目录用 `:` 连接（`db/migrate.md` → `/db:migrate`），子目录里的 `SKILL.md` 用目录名（根目录的 `SKILL.md` 没有名字、跳过），同目录多 md 且含 `SKILL.md` 时只用它并告警；项目层 `<workspace>/.blank/commands` + 用户层 `~/.blank/commands`（`BLANK_USER_COMMANDS_DIR` / `BLANK_USER_INSTRUCTIONS_DIR/commands` 可改），同名项目胜；frontmatter 复用 Skill 口径（`description` / `argument-hint` / `arguments` / `allowed-tools` / `disallowed-tools` / `disable-model-invocation` / `user-invocable`，其中工具规格分词与 Skill 共用同一套 `fyn` + `Wd` 口径），坏 frontmatter 降级不跳过，超 1 MB 跳过，正文超 20000 字截断；`allowed-tools` 里的 `${CLAUDE_PROJECT_DIR}` 按上游 `C9` 口径替换成工作区根（用户层与项目层都展开，反斜杠统一成 `/`），插件命令额外展开 `${CLAUDE_PLUGIN_ROOT}` 为插件根目录，插件命令正文额外展开 `${CLAUDE_PLUGIN_DATA}` 为 `<plugin_root>/data/<plugin_name>`（发现时 mkdir 副作用对齐上游 `Bwe`），非插件来源保持字面量（门控对齐上游 `C9` 的 `n.source`），`${CLAUDE_SKILL_DIR}` 保持字面量（那是 `isSkillMode` 专属），`disallowed-tools` 不替换；参数替换与 Claude 的 `qW` 逐条对齐——`$ARGUMENTS`、`$ARGUMENTS[n]`、`$n`（越界保留原文且不算「用过」）、`$name`（按 `arguments` 声明取位、最长名字优先、后接字母或 `[` 不算占位）、`\$` 转义、替换值不被二次展开、没有占位符时追加 `\nARGUMENTS: <args>`；正文里的 `${CLAUDE_PROJECT_DIR}` / `${CLAUDE_SESSION_ID}` / `${CLAUDE_SKILL_DIR}` 按上下文替换；用户侧 `/name args` 在创建任务、会话回合、运行中插话三个入口展开（标题与界面保留原话，`goal` / 模型读展开正文），未知名字按普通文本；模型侧与 Skill 共用一份目录（同名命令胜），`disable-model-invocation` 只挡模型、`user-invocable: false` 只挡用户斜杠；`commands.loaded` / `command.expanded` 事件与 `GET /projects/{id}/commands` 列表。判据 `services/api/tests/test_commands_v46.py`（43 例，含真实 HTTP 三入口与运行时加载）与 `services/api/tests/test_command_allowed_tools_placeholders_v302.py`（13 例，含项目/用户层展开、插件根展开、SKILL_DIR 保持字面量、反斜杠归一、disallowed 不替换），变异 43/43 + 5/5 红 |
| 输出样式（`.blank/output-styles`） | ✅ | 对齐 Claude 的 `.claude/output-styles`：一个 md 一个样式，项目层 `<workspace>/.blank/output-styles/**/*.md` + 用户层 `~/.blank/output-styles`（`BLANK_USER_OUTPUT_STYLES_DIR` / `BLANK_USER_INSTRUCTIONS_DIR/output-styles` 可改）递归扫描（深度 32、目录数 4096 上限），同名项目胜；扩展名匹配大小写不敏感、名字只删小写 `.md`（`Foo.MD` 保留后缀）；`name` 默认文件名、`description` 默认正文首行（字符串 trim、布尔/数字转字符串、对象/数组告警后回退首行）；`keep-coding-instructions` 按 Claude `m1` 口径解析（true/false 集合，`1` / `0` 也认），`force-for-plugin` 只对 plugin 样式生效、在用户/项目样式上告警并忽略；未知键进 `ignored` 并告警、样式仍加载；坏 frontmatter 降级不跳过；非普通文件/超 1 MiB 跳过；`settings.output_style` 精确查表，空名/未知名字静默回落 `default`；选中样式且 `keep-coding-instructions !== true` 时，系统提示里的默认编程纪律段被样式正文替换，`true` 时两段都在，样式段固定排在安全/审批/工作区边界之后并写明不得覆盖；没有样式时系统提示与旧版逐字节一致（sha256 基线钉在判据里）。`output_styles.loaded` 事件与 `GET /projects/{id}/output-styles` 列表（只给审计信息与字符数，不回正文）。判据 `services/api/tests/test_output_styles_v47.py`（31 例，含真实运行时系统提示与 HTTP 列表），变异 21/21 红 |
| 设置层级（用户 < 项目 < 本机 < 管理策略） | ✅ | 对齐 Claude 的 settings 级联：用户层 `BLANK_SETTINGS_FILE`（桌面壳指到用户数据目录）+ 项目层 `<workspace>/.blank/settings.json` + 本机层 `<workspace>/.blank/settings.local.json` + 管理层 `BLANK_MANAGED_SETTINGS_FILE`（不配就没有这一层），从低到高叠加；高一层只覆盖写了的键，嵌套对象逐键合并、数组与标量整段替换，合并后仍走同一套 `normalize` 夹取；**安全段（`safety` / `permissions` / `sandbox` / `guardian` / `features` / `hooks` / `disable_all_hooks`）只认用户层与管理层**——项目/本机文件写了也进 `ignored`（带原因），有效值不变，否则克隆一个仓库就等于一次提权；单层坏 JSON、顶层不是对象、超 1 MiB 只跳过该层并进 `errors`，其余层照常叠加；运行时在任务根上叠一次（沙箱档位、审批模式、guardian、压缩水位、输出样式、工具检索、后台预算都读这份生效值），层/来源/ignored 落 `settings.layers` 事件（路径打码、不含设置值）；`GET /projects/{id}/settings` 返回生效值与来源，`GET /projects/{id}/output-styles` 的 `selected/active` 也改用工作区级联。判据 `services/api/tests/test_settings_layers_v48.py`（28 例，含真实运行时与 HTTP 端点），变异 27/27 红 |
| MCP 配置变量展开（`mcp.json`） | ✅ | 与 Claude 的 `.mcp.json` 同口径：`${VAR}` 取环境值（设成空串也算「设了」），`${VAR:-default}` 在变量未设**或为空**时用默认值（bash `:-` 语义，默认值不递归展开）；变量未设且无默认值时**保留原文**并告警，而不是像 shell 那样替成空串——「忘了配」不该伪装成「配了个空 token」；占位符变量名必须是 `[A-Za-z_][A-Za-z0-9_]*`，非法名原样保留且告警只说「名字不合法」、绝不回显占位符原文（`${sk-live-...}` 这类疑似密钥不进日志）；展开作用于 `command` / `args` / `env` / `url` / `headers` 五处（另含本机的 `cwd` 与 `auth` 段）；告警按服务端去重，随 `MCPStatus.warnings` 出现在 `GET /settings/mcp`，连不上/被跳过的服务端也带。判据 `services/api/tests/test_mcp_tools.py`（52 例，含真实 stdio 子进程与 HTTP 状态端点），本批 11 例展开判据，变异 10/10 红 |
| settings.json 权限规则（allow/deny/ask） | ✅ | 对齐 Claude 的 `permissions.allow` / `deny` / `ask` 三组规则（与 `permissions.rules` 文本文件叠加，文件里的先命中）：工具名认 Claude 的 `Bash` / `Read` / `Edit` / `Write` / `Glob` / `Grep` / `WebFetch` / `Task` 等别名，也认本地名字；`Bash(cmd:*)` 是前缀（token 边界，等价上游 `startsWith(f+" ")`）、`Bash(cmd)` 是整条子命令 glob（大小写不敏感、空白折叠）、`Bash(*)` 是整工具；命令规则逐段判定（含 `cmd /c "…"` 内层载荷），allow 要求每段全覆盖、deny/ask 任一段命中；路径规则按工作区相对通配、`*` 跨目录，绝对路径/`~`/盘符/`..` 一律不生效；deny 压过 bypass、计划模式、`approve_high_risk`、本会话批准与 allow，ask 压过 bypass / `approve_high_risk` / 会话批准与 allow 且卡片不给「本会话总是允许」；写不出来的条目 allow 记 note 跳过、deny/ask 整份 settings 规则停用并报错；`permissions.defaultMode` 认 `default` / `acceptEdits` / `plan` / `bypassPermissions`；运行落 `permission_rules.settings` 与 `permission_rules.loaded`。判据 `services/api/tests/test_settings_permissions_v50.py`（34 例，含真实运行时三条端到端），变异 10/10 红 |
| MCP 配置三作用域与项目层批准（`.mcp.json`） | ✅ | 对齐 Claude 的 user < project < local 级联：用户层 `mcp.json`（`BLANK_MCP_FILE` 或设置目录旁）、项目层 `<workspace>/.mcp.json`、本机层 `<workspace>/.blank/mcp.local.json`，同名服务端**整条覆盖**、字段不跨层合并（高优先级层写 `disabled` 就是把同名服务端整条关掉），坏掉的一层只贡献一条 `failed` 状态、不拖垮其余层；项目层/本机层条目在批准前**不启动进程**——批准记录按「作用域 + 工作区路径哈希 + 服务端名 → 整条定义的 sha256」存 `mcp-approvals.json`（`BLANK_MCP_APPROVALS_FILE` 可改），只存哈希不存配置（`env` / `headers` 里的密钥不会被复制第二份），改一个字节或换一个工作区都会退回 `pending`，坏批准文件按「什么都没批准」fail-closed；`MCPManager` 按工作区分桶缓存（用户/项目/本机三份配置 + 批准文件任一改动即重连），运行时用原始工作区根（不是任务隔离副本）读配置；`GET /settings/mcp?project_id=` 返回 `scope` / `approval` / 三层路径与是否存在，`POST /settings/mcp/approvals` 与 `DELETE /settings/mcp/approvals/{name}` 批准/撤销并立即重连，批准用户层、批准未知名字、批准被更高层覆盖的定义分别给 400/404/409；设置页 MCP 面板加项目下拉、作用域徽标、待批准/已批准与批准/撤销。判据 `services/api/tests/test_mcp_scopes_v50.py`（25 例，含真实 stdio 子进程、HTTP 端点与运行时装配），变异 17/17 红 |
| 插件包（`.claude-plugin/plugin.json`） | ✅ | 对齐 Claude 的插件包契约：用户层 `~/.blank/plugins/<name>/.claude-plugin/plugin.json`（`BLANK_USER_PLUGINS_DIR` / `BLANK_USER_INSTRUCTIONS_DIR/plugins` 可改）+ 项目层 `<workspace>/.blank/plugins/...`，同名项目覆盖并告警；坏清单（坏 JSON / 顶层非对象 / 缺 name / 名字以 `-` 开头或含 `:` / 超 1 MiB）只跳过该插件并点名，不拖垮整张目录；`commands` / `agents` / `skills` / `outputStyles` 四类提示词组件接进既有加载器，名字按 `<plugin>:<name>` 命名空间（裸名字不能命中），清单路径覆盖替换默认目录、必须 `./` 开头且不得逃出插件目录（违规告警后回退默认目录或跳过该项）；`hooks` / `mcpServers` / `lspServers` 与自带 `.mcp.json` 只进 `ignored` 报告、不加载——仓库里丢进来的插件包不会获得进程执行或外连；启用跟随 `enabledPlugins`（顶层）或 `plugins.enabled`（同层后者优先），键可为 `name` 或 `name@marketplace`（按基名匹配），没写时取清单 `defaultEnabled`（默认 true），停用插件不注入任何组件，设置里指到不存在的插件给告警；`GET /projects/{id}/plugins` 返回清单、组件计数与启用状态（插件条目路径打码、项目层只给工作区相对路径；用户层根给本机绝对路径，便于用户直接放置插件），设置页「外部工具与插件」新增插件面板（清单模板、命名空间说明、停用原因、忽略段与告警）。判据 `services/api/tests/test_plugins_v49.py`（26 例，含真实 HTTP 列表），变异 15/15 红 |
| 钩子事件面（对齐 Claude hooks，AB 批） | ✅ | 事件全集扩到 `SessionStart/SessionEnd/UserPromptSubmit/PreToolUse/PostToolUse/PostToolUseFailure/PostToolBatch/PermissionRequest/PermissionDenied/PreCompact/PostCompact/Notification/Stop/SubagentStop/TaskCreated/TaskCompleted/WorktreeCreate/WorktreeRemove`，stdin 一律带 `hook_event_name`；可阻断集合只多 `permission_request` / `stop` / `subagent_stop`（通知/批处理/压缩前后不许拦流程，异步配置不会被逼着同步等）；`PermissionRequest` 接在**真实发卡点**（`_approval_gate` 的 `interrupt` 之前，而不是网关的 ASK 分支——图形路径的 ASK 走图中断）：allow 视同用户批准并记 `SCOPE_ONCE`（计划模式忽略 allow，`approval.granted.rule=hook:permission_request`），deny 直接拒绝不弹卡并把理由按 call key 并进 `pre_tool_use` 的拒绝表，两条都按调用触发 `PermissionDenied`；审批卡出现前跑 `Notification(permission_prompt, title=需要审批：<tool>)`；一批工具跑完发 `PostToolBatch`（载荷 `tool_calls[{tool_name, tool_input, ok, mode}]`）；`Stop`/`SubagentStop` 阻断后把理由交回模型继续，第二起 `stop_hook_active=true`，上限 `BLANK_STOP_HOOK_BLOCK_CAP`（默认 8）且 `stop_blocked/stop_blocks` 是声明的图状态（不声明时 langgraph 静默丢弃，钩子会把自己的阻断当第一次）；`PreCompact` 输出拼进压缩提示词且用完即清；`SessionStart` 按 `source=startup`、`SessionEnd` 按 `reason=completed|waiting_approval|interrupted` 分派；子代理收尾发 `Notification(agent_completed)`。偏离：`UserPromptExpansion` / `TeammateIdle` 两个事件已由 AF 批移植，`session_crons` 恒空、`background_tasks` 形状是 `{id, description, status}`、SubagentStop 的 `agent_transcript_path` 已由 #305 批补齐（触发点把 `messages` 写成 `.blank/context/subagent-transcript.jsonl`，载荷带路径）、Notification 只接 `permission_prompt`/`agent_completed` 两类、SessionEnd 只有三种 reason。判据 `services/api/tests/test_hook_events_v52.py`（24 例，含真子进程钩子的图内与网关端到端），变异 9/9 红 |
| settings.json 钩子配置（对齐 Claude hooks，AC 批） | ✅ | settings.json 的 `hooks` 接进同一个 `HookRunner`：Claude 的嵌套形状 `{事件: [{matcher, hooks:[{type, command, timeout, async}]}]}` 与 `BLANK_HOOKS` 的扁平形状都收，PascalCase 事件名折成内部蛇形名；未支持的事件（如 `UserPromptExpansion`）/ 非 `command` 类型 / 空命令 / 非法 matcher 正则告警后跳过（告警只带事件与序号，不带命令正文），timeout 夹到 0.5–60 秒；两处配置合并后按「事件 + 命令 + matcher + 超时」去重，同一条不会跑两次；`disableAllHooks`（含下划线写法，下划线优先）为真时一个钩子都不加载。matcher 语义对齐上游 `_zr`：空 / `*` / `.*` 全中，纯名单（字母数字下划线、`|`、逗号、空格、连字符）按 `|` / 逗号拆分后**精确**比对，其余当未锚定正则搜，非法正则不匹配；旧实现的子串匹配已不存在（`Write|Edit` 不再命中 `WriteFile`）。`hooks` 与 `disable_all_hooks` 进安全段：项目/本机层写了进 `ignored`（钩子就是任意命令，且我们还没有 Claude 的工作区信任对话框），只有用户层/管理层生效。运行时从生效设置构建钩子并在有钩子/告警/总开关时落 `hooks.loaded`（每事件条数、来源、告警，不含命令正文）。偏离：管理层钩子也受 `disableAllHooks` 约束（上游规定非管理层关闭不影响 managed hooks），我们还没有办法按来源区分；钩子类型 `command` / `prompt` / `agent` / `http` / `mcp_tool` / `script` 六类均已实现（AF 批补全，`script` 按上游同口径报"不可用"的非阻断错误）。判据 `services/api/tests/test_settings_hooks_v53.py`（20 例，含 settings 来源的真子进程钩子任务端到端），变异 9/9 红 |
| SubagentStart 钩子与 SessionStart 附加上下文（AD 批） | ✅ | 对齐 Claude 的 `SubagentStart`：事件注册为 `subagent_start`（别名 `SubagentStart`），**不在阻断集合**——上游把它的退出码 2 渲染成 `hook_non_blocking_error`（父会话里一条可见提示），子代理照常启动；matcher 比对 `agent_type`（`request.agent_name`，未选中定义时 `general-purpose`），钩子 stdin 带 `agent_id` / `agent_type` / `agent_path` / `depth` / `goal` / `parent_thread_id` / `root_thread_id` / `workspace` / `permission_mode` / `fork` / `session_id` / `transcript_path` / `scratchpad_dir` / `prompt_id` / `caller_session_id` / `effort`（`effort` 是 `{"level": ...}` 对象，与 `hooks.run(effort=...)` 的 `CLAUDE_EFFORT` 子进程环境变量同源）；`additionalContext` 以独立一条 user 消息注入子代理启动上下文（标记头 `<subagent-start-hook>`，位置在 Skill 预加载之后、goal 之前，单条 `MAX_CONTEXT`、合并后截断，正文写明"只补充信息，不得放宽规则/审批/工作区边界"）；退出码 2 的正文**不注入**（`usable_context` 按 `exit_code` 分流，`_execute` 在非阻断事件上会把 stderr 并进 `additional_context`），只进 `hook_warnings` → `SubAgentResult.render()`（前台工具回执与后台 `task_output` 都能看到）并落 `hook.non_blocking_error`；新派发落 `hook.context_received`；续聊（`send_message`）不是新派发、不重跑（上下文已在上一轮对话里），fork 仍按新派发跑。`SessionStart` 的 `additionalContext` 同样不再"只记审计、正文被丢"：独立一条 user 消息（在会话历史之后、本轮目标之前，写明只作参考、不得覆盖安全/审批/工作区边界），落 `hook.context_injected`；退出码 2 走 `hook.non_blocking_error` 且不注入。偏离：上游的 `isolatedContext` 子代理跳过注入、`hookContextCarried`（续聊已带过）去重未移植（续聊整轮不重跑，效果等价；隔离副本仍会收到）；stdin 未提供 `served_call`（语义不明、不在官方文档里，留作文档偏离）。SessionStart 四字段（`sessionTitle` / `initialUserMessage` / `watchPaths` / `reloadSkills`）见下条 #304。判据 `services/api/tests/test_subagent_start_hooks_v54.py`（9 例：事件面与别名、exit 2 分流单元、真子进程按 agent_type 分发、注入位置与 goal 原文、exit 2 可见但不注入、无钩子零改动、续聊不重跑、SessionStart 正反两路 HTTP 端到端），变异 7/7 红（逐条实测红集合）；`services/api/tests/test_subagent_start_stdin_v308.py`（8 例：session_id / transcript_path / scratchpad_dir / prompt_id / caller_session_id / effort 六字段各一条可失败判据 + 一次性到位 + CLAUDE_EFFORT 子进程环境变量），变异 8/8 红 |
| SessionStart 钩子输出四字段（#304） | ✅ | 对齐上游 `Sn()` 校验函数定义的 SessionStart 钩子输出四字段：`sessionTitle`（string）→ 经 `Task.thread_id` 更新 `Thread.title`（检查点 thread_id 是 `task:<id>:attempt:<n>` 格式，不是 UUID，必须经 Task 行桥接）；`initialUserMessage`（string）→ `initial_messages()` 在系统提示之后、历史之前插入一条 user 消息（`[:8000]`）；`watchPaths`（string[]，经 `i1t()` 校验）→ 写入 `.blank/context/watch.json`；`reloadSkills`（bool，只认 `true`）→ 重扫磁盘 Skill 与命令目录、重建 `SkillCatalog` 并替换 `session.skill_catalog`。四字段各自独立消费、互不影响，一个钩子只给其中一两个也照常生效，各自落审计事件（`hook.session_title` / `hook.watch_paths` / `hook.skills_reloaded`）。`parse_hook_output` 对齐上游 `Sn()` 类型校验：字符串字段非字符串即错型不解析、数组字段含非字符串元素即错型不解析、`reloadSkills` 只认真布尔 `true`。判据 `services/api/tests/test_session_start_output_v304.py`（22 例），变异 8/8 红 |
| agent_transcript_path 补齐（#305） | ✅ | 对齐上游 `SubagentStop` / `StopFailure` 载荷里的 `agent_transcript_path`：本机此前没有 transcript 落盘机制，钩子拿到路径也没东西可读。新增 `hooks.write_transcript(messages, path)` 把对话消息写成 JSONL 快照（每行一条 `{"role", "content"}`，content 非字符串时 `str()` 兜底，空消息列表写空文件，父目录自动创建），返回绝对路径。两个触发点各自落盘：`subagent.py` 的 `stop_gate` 把子代理的 `messages` 写到 `.blank/context/subagent-transcript.jsonl`，`runtime.py` 的 `_fire_stop_failure` 把检查点里的 `messages` 写到 `.blank/context/agent-transcript.jsonl`，两处都在载荷里带 `agent_transcript_path`。没配钩子时 SubagentStop 路径惰性（`stop_gate` 直接返回不写文件），StopFailure 路径照常写但无钩子接收。判据 `services/api/tests/test_agent_transcript_path_v305.py`（10 例：write_transcript 四单元、事件面注册、SubagentStop 端到端载荷带路径且文件内容逐行 JSON、无钩子不写文件、StopFailure 端到端载荷带路径、transcript 反映检查点消息、无钩子零改动），变异 4/4 红（摘 SubagentStop 路径 → 1 红；摘 StopFailure 路径 → 2 红；write_transcript 不写文件 → 7 红；去掉非字符串 coercion → 1 红，红集合均实测） |
| StopFailure 钩子事件（AE 批） | ✅ | 对齐 Claude 的 `StopFailure`：回合**因为 API 错误**结束时发它、**顶掉** `Stop`（上游是"替代"而不是既发 Stop 又发它），事件注册为 `stop_failure`（别名 `StopFailure`，**不在阻断集合**——上游明说 fire-and-forget、钩子的输出与退出码一律忽略，所以退出码 2 不会变成"拒绝执行"）；matcher 的比对对象是**错误分类**（上游 `matcherMetadata.fieldToMatch = error`），新增翻译层 `stop_failure_error_category(code, status)` 把本机 `ModelError` 翻成上游十三个取值之一（**状态码优先**：429→`rate_limit`、401/403→`authentication_failed`、402→`billing_error`、404→`model_not_found`、400→`invalid_request`、503→`overloaded`；再按 code：`model_timeout` / `model_unreachable` / `model_stream_error` / `model_stream_empty` / `model_http_error`→`server_error`、`model_output_not_json` / `model_tools_unsupported`→`invalid_request`、`circuit_breaker_open`→`overloaded`、`all_models_failed`→`unknown`；都落不上→`unknown`；分类表与上游逐字相同）；触发点在两条 `except ModelError` 终态（runtime.py:1215 run 路径、runtime.py:1306 续跑路径），都在 `_finish` 之后、`_close_hooks` 之前调 `_fire_stop_failure`（runtime.py:823），载荷带 `error` 分类、`error_details`（错误文本前 500 字）、`last_assistant_message`（图里最近一条助手文本前 4000 字，读不到就省略该字段、事件照发）；退出码 2 / 非 0 一律忽略：不注入上下文、不落 `hook.turn_blocked`、不叫模型再跑一轮，只留 `hook.completed` 审计（任务仍按 API 错误失败收尾）。偏离：上游 `error_details` 是结构化错误对象，本机只有错误文本（前 500 字截断）；`last_assistant_message` 仅在图里已有助手回复时给；`agent_transcript_path` 已由 #305 批补齐——触发点把检查点里的 `messages` 写成 `.blank/context/agent-transcript.jsonl`，载荷带路径，钩子可读。判据 `services/api/tests/test_stop_failure_hooks_v55.py`（8 例：事件面与别名、21 格分类表、matcher 按错误分类分流、API 错误只发 StopFailure 不发 Stop、载荷带 last_assistant_message、fire-and-forget 四点、续跑路径同样触发、无钩子零改动），变异 13/13 红（逐条实测红集合） |

| 版本 | 验收要点 | 证据 |
| --- | --- | --- |
| v0.1 阶段0 工程基线 | 一条命令起全栈、健康检查、统一错误响应、迁移可建可回、前端类型检查/Lint/构建 | `pnpm regression` 全绿；`tests/` 中的健康与错误用例 |
| v0.2 阶段1 项目与工作区 | 项目/工作区 CRUD、路径白名单与越界拦截、主机路径脱敏、删项目级联清理任务与执行痕迹 | `tests/test_projects_v02.py`（含 `test_delete_project_removes_tasks_and_traces`）、`tests/test_auth_v02.py` |
| v0.3 阶段2 多模型 | 供应商 CRUD、Key 加密且不外泄、连通性测试、非流式与流式对话、token 记账 | `tests/test_models_v03.py`、`/api/v1/usage/summary` 实测 |
| v0.4 阶段3 Agent Runtime | 计划 → 执行 → 验证 → 汇总四节点、工具集、SQLAlchemy 检查点、事件流与证据包 | `tests/test_agent_v04.py` |
| v0.5 阶段4 任务控制台 | 深色三栏（任务列表 / 时间线 / 文件·Diff·证明包）、实时轮询与历史恢复、深链 | 浏览器实测创建并运行任务，时间线折叠为计划/工具/终端/审批/结果区块；382px 视口实测三栏 grid 退化为单列（`grid-cols-1`）、横向溢出 0px |
| v0.6 阶段5 审批与恢复 | 高风险操作统一走图中断：批准从断点继续并补记 `approval.granted`；驳回则该步记「未执行」、模型换个做法继续（对齐 Claude Code：拒绝是结论，不是重试信号），只有用户手动暂停才会 `paused`；自动修复上限 3 轮；进程重启后仍可恢复 | `tests/test_approval_v06.py` 8 项 + 浏览器「批准并继续」实测 |
| v0.7 阶段6 沙箱与安全 | 一次性容器执行 `run_shell`/`run_tests`（工作区唯一可写挂载、默认无网、CPU/内存/PID/时长上限、超时强杀不留残留容器）、密钥不进容器与输出、审计与快照可回滚 | `tests/test_sandbox_v07.py`（含容器内 `run_tests` 用例）、`tests/test_permissions_v10.py`；容器内 `uname` 为 Linux 是当时装有 Docker 的机器上记录的，本机现在没有 `docker` 命令行，这一条无法就地复核（见下方已知限制）；评测 T18/T19 返回 `expected_denied` |
| v1.0 阶段7 质量与评测 | 回归门禁一条命令、评测样例集与成功率报告、成本与权限守卫 | `pnpm regression`、`evals/results/`、`tests/test_cost_v10.py` |
| v1.2 Bot 团队 | 群聊编排（每次发言落一条普通 Task）、上下文管家挑选、主 Bot 的团队管理工具与高风险审批边界、跨群全文检索与锚点跳转 | `tests/test_team.py`、`tests/test_phase2_team_admin.py`、`tests/test_phase2_context.py`、`tests/test_phase2_search.py`；`pnpm e2e:team`（stub 模型 + 隔离库 + Chrome headless 1440×900，38 项判定）；`evals/team_e2e/real_model_check.py`（DeepSeek 真模型 + 隔离实例 :8061，13 项判定，含三次高风险审批连续放行） |

已知限制（不粉饰）：

- **Claude 式编程工具面已落地，但有两项刻意偏离**（N 批，判据
  `services/api/tests/test_claude_parity_v24.py`）：Read 不读 PDF/图片、
  路径仍要求工作区相对路径而不是绝对路径。两项都是产品边界（附件读取是另一套能力）。
  链式编辑已核对上游原文：`old_string` 只要求「本会话内读过、精确匹配且唯一」
  （`E:/tmp/rea7/claude_tools_dump.txt` 第 230–231 行），**没有**「`old_string` 是上一次
  `new_string` 子串就拒绝」这条规则；我们每次写成功后重记文件状态，读一次即可连续编辑，
  与本条一致（判据 `test_three_chained_edits_after_one_read`）。
  NotebookEdit（Q6）与后台 shell（Q1）已不在偏离项里：都已落地，见上表。

- **remote 子代理冻结（外部条件，不是待办）**：Qoder 的配置里 `kind` 字段只区分
  `local` / `remote`，公开文档没有给出 remote 的传输协议、端点与鉴权约定；没有可复现的
  对端，写不出能失败的判据，硬做只会得到「看起来支持、实际连不上」的假功能。因此
  `spawn_agent` 不接受 remote，类型名按未知类型拒绝并列出可用类型（`general-purpose`、
  `fork`）。**重开条件**：拿到一个可复现的 remote 端点（含鉴权方式与版本协商），并能在
  本机跑通一次派发、当场记录请求/响应；满足后再按 fork 同样的方式补判据与变异电池。

- **P 批把 Glob / Grep 的参数面补齐了，子代理也改成默认后台并支持续聊，Claude 工具表
  至此已无未移植项**（判据 `services/api/tests/test_claude_tool_surface_v28.py`、
  `test_claude_subagent_default_v29.py`、`test_claude_send_message_v30.py`）：`glob` 已按
  「无斜杠按文件名、带斜杠按相对路径」实现，`search_code` 补齐了
  `output_mode / glob / type / -i / multiline / -A/-B/-C / -o / -n / head_limit / offset`，
  `list_agents` 能看到当前任务的后台子代理，`send_message` 能带着上下文续聊。
  NotebookEdit 已于 Q6 补齐（见上表；四处偏离：Read 对 `.ipynb` 默认回单元格视图，
  而不是 Claude 的「原文 + id 显示行」；没有真实 id 的老 notebook 用合成的 `cell-<序号>`
  定位、只用于本次编辑不写回文件；写回保留原缩进与 CRLF，而不是统一重排；文件写上限 8MB）。
  Workflow 已于 Q5b 补齐（见上表；偏离：脚本落在项目
  `.blank/workflows/.runs/` 而不是 Claude 的会话目录；续跑缓存只在进程内，重启即失效；
  网络边界靠预置层替换 + Node 权限模型，不是内核级网络沙箱；每个 `agent()` 仍是既有的
  隔离只读子代理工具集，不共享父会话工具面；`schema` 只做骨架校验；工作流随任务收尾取消，
  不跨任务/跨进程恢复）。定时工具已于 Q5a 补齐（见上表；三处偏离：任务编号
  用稳定的 `job_id` 而不是行主键，重排后仍能用最初那个 id 取消；没有 REPL「空闲」这层
  对象，用「绑定任务正在跑就顺延 60 秒」近似；`durable` 用库里的触发器行承载，而不是
  `.claude/scheduled_tasks.json`）。ReportFindings 已于 Q4 补齐（见上表；三处偏离：工具名用 snake_case
  `report_findings`、回执文案为中文；清单同时落 `review.findings` 事件与工具产物，
  便于界面和证据包读取，而不只是一条文本回执；子代理工具表刻意不含它，发现由主代理
  一次性上报）。Skill 工具已于 Q3 补齐（见上表；两处偏离：一句话说明取正文首行，
  库里没有单独的 description 字段；加载状态只在本任务进程内保留，重开任务会重新加载）。
  EnterWorktree/ExitWorktree 已于 Q2 补齐（见上表；两处偏离：建在
  `.blank-worktrees/` 而不是 `.claude/worktrees/`、没有暴露 `baseRef`，一律从 HEAD 创建；
  `path` 只认已注册的 git 工作树）。Monitor 已于 R1 补齐（见上表；四处偏离：只支持
  `command`，Claude 的 WebSocket 变体未移植；`timeout_ms=0` 的「不设期限」仍受安全设置的
  单条命令超时约束（默认 120 秒），回执会如实说明；每轮最多投 25 行、内存队列上限 200 行；
  `run_shell` 的会话内「总是允许某前缀」不会自动扩展到 monitor，同一命令从 monitor 走仍会
  按当前档位重新判定）。REPL 已于 R2 补齐（见上表；六处偏离：内层工具用本地名字
  （`read_file` / `run_shell` / `search_code` 等）而不是 Claude 的 Read/Bash 别名；Claude 的
  顶层 `let/const` 持久是语言级作用域，本机用「扫描顶层声明 + `Object.assign(globalThis, ...)`」
  近似回填（同名遮蔽与解构声明不在近似范围内）；REPL 里没有 web/Agent/Workflow 等会自己开进程
  或等审批的入口；需要审批的动作只能在顶层调用里批，REPL 内层会直接拒绝；超时或进程崩溃会
  清空整个会话状态，下一次调用从新会话开始；`vm` 不是内核级沙箱，脚本的能力边界靠「不注入
  `require`/`process`/`import` + 内层调用走网关」，真正的高风险动作仍由网关权限与沙箱档位
  把关）。任务清单 Task* 已于 R3 补齐（见上表；五处偏离：四个工具常驻而不是 Claude 的
  `shouldDefer`，纯展示差异；更新不存在的任务回 `Task #<id> not found` 而不是 error，
  读侧保持与 TaskGet 同口径；空 `subject` / `description` 明确拒绝，Claude 的 zod 允许空串；
  团队任务按 group_id 共享同一份清单，但没有移植 `task_assignment` 消息那一路；
  钩子事件名用 snake_case `task_created` / `task_completed`，与本地其余事件同一命名，
  不存时间戳、不发 UI 通知）。MCP 资源四工具已于 R4 补齐（见上表；七处偏离：目录能力门控
  键名是 Claude 的编译期常量、无法从产物恢复，按语义放宽为 `capabilities.extensions` 里任一
  `directoryRead === true`；二进制落系统临时目录而不是 Claude 的内部存储，但「给路径、不展开
  正文」的契约一致；列表/读取结果以 JSON（`{"resources"}` / `{"contents"}`）输出而不是 Claude
  的文本模板；全部失败时回明确错误而不是 `No resources found`；目录截断只以 `data.truncated`
  表达、不在文本里追加说明；工具名沿用本地 snake_case（`list_mcp_resources` 等）而不是 Claude
  的 `ListMcpResourcesTool`；刷新是否保留激活状态在拆解产物里读不出来，这里显式钉成「保留」）。
  LSP 已于 R5 补齐（见上表；七处偏离：工具名沿用本地 snake_case `lsp` 而不是 Claude 的
  `LSP`；语言服务器清单读本机 `lsp.json`（跟设置文件同目录，`BLANK_LSP_FILE` 可改），
  不是从 Claude 的客户端配置源读；没有文件 watcher，每次请求前把当前全文重发一次
  `didChange`，换来「永不拿旧内容答新问题」但大文件要多传几次；初始化失败按扩展名记住、
  后续调用直接回报而不是每次重拉（Claude 是否重拉在产物里读不出来，这里选了不留一堆
  半死进程）；子代理工具表不含 `lsp`（语言服务器管理器是任务级事实、绑定主工作区根，
  子代理跑在隔离副本里，另起一套没人收）；`lsp.json` 写坏一条只跳过该条并列出原因，
  不拖垮整份配置；非 runtime 入口没有注入管理器时走模块级兜底（按根共用一份，宿主进程
  结束才回收），生产路径上由运行时每任务注入并在收尾关闭）。
  后台 shell 已于 Q1 补齐
  （`run_in_background` + `task_output`/`task_stop`，见上表），但它有两处与本机实现绑定
  的偏离：沙箱路径不流式（只在结束时整段落日志）、完整输出走 `task_output` 而不是
  Claude 的「Read 输出文件」（临时日志在系统临时目录，Read 只读工作区相对路径）。
  续聊本身的边界：对话只在本进程内保留（每任务最多 20 个会话、每会话 60 条消息、
  单条 8k 字符），隔离子代理副本结束即清理因此明确拒绝续聊，仍在后台跑的句柄要先
  `task_output`；任务收尾的等待窗口是 90 秒，过时未完成的会被取消并在交付正文点名。
  AskUserQuestion 已于 R6 补齐（见上表；四处偏离：工具名沿用本地 snake_case
  `request_user_input` 而不是 Claude 的 `AskUserQuestion`；`afkTimeoutMs`（用户离开
  自动超时）未落地，问题卡没有倒计时；preview 前端只按纯文本 `<pre>` 渲染，不原样执行
  模型给的 HTML/Markdown；问题卡没有单独的注解/补充输入框，`annotations.notes` 与
  `response` 目前只走 API 侧，判据里有真实 HTTP 往返）。ProposeGoal 与 ProposeSkills
  也于 R6 补齐（见上表；四处偏离：目标确认卡的 id 固定合成 `goal1`，模型自己写的问题 id
  不会与它抢位、但工具调用 id 不透传；`session_goal` 写入按 2000 字截断，提案正文本身
  的上限是 500 字；提案落库到 `skills` 表而不是写 skill 文件，采用 `new` 提案后状态是
  `draft`、要用户在技能页再启用才生效；会话目标按 thread 记，不跨团队成员共享，
  Claude 的队友目标那一路没有移植）。文件型子代理定义已于 S1 补齐（见上表；六处偏离：
  目录用 `.blank/agents` 而不是 `.claude/agents`，用户层是 `~/.blank/agents` 而不是
  `~/.claude/agents`；Claude 的 flagSettings / policySettings 两层没有移植，只保留
  用户层与项目层（plugin 层已于 S7 接上：插件里的 agents 按 `<plugin>:<name>` 加载）；工具名沿用本地 snake_case，定义里写 `Read` / `Bash` 这类 Claude
  别名不会命中本地工具（结果是拿不到工具，不是悄悄放宽）；`mcpServers` /
  `color` / `initialPrompt` 只解析不生效、逐条进 `agent_ignored`（`model` 已于 #315
  生效、`effort` 已生效、`hooks` 已于 #316 生效、`memory` 已于 #317 生效，四者都只改
  子代理自己的调用、不改父
  会话；两处偏离：工具入参的 `model` 四别名枚举
  `sonnet` / `opus` / `haiku` / `fable` 没有移植——本机供应商模型名不同，照搬会
  要么放宽、要么误发 400，定义里的 `model` 原样透传；
  `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` 会剥离调用方显式模型的链路未移植——本机没有
  这个环境变量的对等物）；`hooks` 的移植口径见 #316 条与下方偏离；
  `permissionMode` / `isolation` / `background` 已于 Y 批生效（见上表；
  两处偏离：声明档只收紧——隔离副本基线是 `accept_edits`，所以 `plan` / `default` 可
  生效而 `acceptEdits` / `bypassPermissions` 的声明被忽略，Claude 允许从 default 父档
  升到 acceptEdits 的行为没有移植；`remote` 隔离形态冻结（见下方已知限制），解析时
  按本地运行并告警）；插件 agent 的 `permissionMode` / `hooks` / `mcpServers` 按上游
  `Skn` 只告警忽略。定义 `hooks` 已于 #316 生效（三处偏离：本机没有工作区信任对话框，
  工作区/用户来源的定义一律视为可信，钩子命令会直接执行——这与 settings 层「只有用户层
  / 管理层能配钩子」的收口不同，定义文件本身就是用户自己放进工作区的；未知事件键里若
  藏着守卫内容（比如拼错成 `PreToolUSe`）上游会整份拒绝该 Agent，本机只按未知事件丢弃
  并告警；子代理工具循环不执行 PreToolUse / PostToolUse（父会话的这两类钩子在子代理里
  同样不跑，是既有缺口），所以定义里写它们只会在子代理内静默不生效，只有
  `permission_request` / `subagent_stop` / `notification` / `worktree_remove` 等走
  真实管线的事件可观测）。定义 `memory` 已于 #317 生效（三处偏离：不自动补
  Read/Write/Edit 工具——上游给声明了 `memory` 的 agent 补读写工具，本机子代理工具池
  不因定义扩大，改为注入一个只能写本作用域 `MEMORY.md` 的 `memory_write` 工具；
  上游「一文件一记忆 + MEMORY.md 索引」的布局压缩成单一 `MEMORY.md` 条目流，格式与主
  代理的项目长期记忆一致；远程记忆目录（`CLAUDE_CODE_REMOTE_MEMORY_DIR`）与
  `primedEntrypoint` 未移植——本机没有远程记忆存储）。正文超过 20000 字截断
  （Claude 原样使用），fork 已于 T 批补齐（见上表），remote 形态冻结（见下方已知限制）。
  子代理 `skills` 预加载已于 X 批补齐（见上表；两处未移植：账户同步 Skill 的
  `syncedSkills` 分支与「非 prompt 型 / 策略禁用」两类上游 warning——本机 Skill 目录
  只装载 prompt 型、也没有账户同步这条链路，没有对应状态可判）。
  文件型 Skill 定义已于 S2 补齐（见上表；六处偏离：目录用 `.blank/skills` 而不是
  `.claude/skills`、用户层是 `~/.blank/skills`，且刻意只读 `.blank`、不把仓库里的
  `.claude/skills` 顺带加载；`allowed-tools` / `disallowed-tools` 只解析留审计，不改变
  工具池（工具池由网关与场景决定，不因一份提示文件放宽或收紧），但 `allowed-tools`
  里的 `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}` 已按上游 `Iot` 替换成 Skill
  目录与工作区根（正斜杠，`disallowed-tools` 原样保留，U 批）；正文超过 20000 字截断
  并标注（Claude 原样使用）；`model` / `effort` / `hooks` / `agent` / `background` /
  `context: fork` / `shell` / `fallback` 已分别移植（见各批记录；`shell` 驱动正文里
  `!` 块的解释器，未声明时仍走 host shell，与上游「默认 bash、不分平台」的偏离见
  #313 条；`fallback: true` 的薄指针在合并层让位给同后缀的插件技能并落
  `skills.fallback_shadowed` 事件，本机没有 bundled / MCP prompt 技能与
  `syncedSkills` 链路，后缀集合只看 `plugin:` 来源，见 #314 条）；
  剩余未支持字段（`version` / `created_by` / `improved_by` /
  `mcpServers` / `lspServers`）只进 `ignored`；
  `paths` 条件激活已于 AA 批
  补齐（见上表；偏离：匹配器是 gitignore 近似，不是上游 `ignore()` 库的同款实现，
  罕见边界可能有出入）；`synced` 同步根（只跳过、未实现同步）未移植。
  S2 当时「本机还没有斜杠
  命令入口、`disable-model-invocation` 的 Skill 用户侧也没有触发路径」这句话已由 S3 改写：
  Skill 与命令共用 `/名字` 入口，`disable-model-invocation` 只挡模型、不再挡用户）。
  自定义斜杠命令已于 S3 补齐（见上表；七处偏离：目录用 `.blank/commands` 而不是
  `.claude/commands`，用户层是 `~/.blank/commands`；只认 `.md` 后缀、`SKILL.md` 匹配
  大小写不敏感，命令名本身按文件名原样（Windows 大小写不敏感文件系统下两条只差大小写的
  文件不会同时存在）；`!` 前缀的 shell 执行块已移植（行首 ``!`` 开头的行当
  shell 命令跑，stdout 嵌回正文，行中 ``!`` 不动；解释器按 frontmatter 的
  `shell: bash|powershell` 选择、未声明走 host shell（#313）；``root`` 为空时原样保留）；未知 `/名字` 按普通文本入库而不是报「未知命令」——本机没有补全/选择器，
  把 `/path/to/file` 当错误会误伤正常消息；团队群聊消息与定时/工作流等非手输任务不走
  用户侧展开（群消息是发言，不是 slash 调用）；`allowed-tools` / `disallowed-tools` 只审计
  不改变工具池，且 `allowed-tools` 里的 `${CLAUDE_SKILL_DIR}` / `${CLAUDE_PROJECT_DIR}`
  不替换——现有转储只能看到插件命令把 `allowed-tools` 交给 `lme`、看不到 `lme` 是否与
  Skill 共用 `Iot` 构造器，按未验证处理（Skill 侧已于 U 批替换）；
  `${CLAUDE_EFFORT}` 已移植（命令定义的 `effort` 字段经 `effort_for_prompt` 裁决后透传
  到 `render_prompt`，`expand_user_input` 与 Skill 渲染两条路径都覆盖；子代理侧同样
  携带同一档位），`${CLAUDE_SKILL_DIR}` 只对 `SKILL.md` 命名的命令有值（与
  Claude 一致：普通命令没有这个占位符的值）；`qW` 的越界占位符语义按上游实现——保留原文、
  不算「用过」，因此没有别的占位符时仍会追加 `ARGUMENTS:` 行）。
  输出样式已于 S4 补齐（见上表；五处偏离：目录用 `.blank/output-styles` 而不是
  `.claude/output-styles`，用户层是 `~/.blank/output-styles`，且刻意只读 `.blank`；
  plugin 清单已于 S7 接上（插件 outputStyles 按 `<plugin>:<name>` 加载），marketplace
  那层没有；`force-for-plugin` 只解析并告警、不生效；
  Claude 内置的 Explanatory / Learning / Concise 三个英文 persona 不移植，`default`
  就是本机的默认系统提示；没有每轮注入的 `"<name>" output style is active.` 元提醒——
  本机每次任务/回合重建系统提示，样式在合成处已经生效，重复提醒只会多一份可漂移的副本；
  设置键名是 `output_style` 而不是 `outputStyle`，归一化时空串/超过 256 字回落 `default`）。
  设置层级已于 S5 补齐（见上表；五处偏离：目录用 `.blank/settings.json` /
  `.blank/settings.local.json` 而不是 `.claude/...`；Claude 允许项目/本机层改
  `permissions.defaultMode` 等安全段，本机只认用户层与管理层——克隆来的仓库不能
  替用户放宽审批/安全边界，写了也进 `ignored`；`settings.json` 本身不展开 `${VAR}`
  （与 Claude 一致；MCP 配置 `mcp.json` 的五字段展开已按 `.mcp.json` 口径补齐，见上表）；
  管理层只认 `BLANK_MANAGED_SETTINGS_FILE`，没有平台默认管理路径与 `policySettings`
  那套；设置页仍只读写用户层，叠加结果走 `GET /projects/{id}/settings` 只读展示）。
  插件包已于 S7 补齐（见上表；五处偏离：目录用 `.blank/plugins` 而不是
  `.claude/plugins`，用户层是 `~/.blank/plugins`，且刻意只读 `.blank`；没有
  marketplace（网络安装 / 更新源）这一层，只认本地目录，`name@marketplace` 键只按
  `@` 前的基名匹配；没有 `/plugin` 交互命令与启用写回，开关只从设置文件读；
  `hooks` / `mcpServers` / `lspServers` 与自带 `.mcp.json` 只报告不加载——这是刻意
  的安全边界，克隆仓库里的插件包不能换来进程执行或外连；清单里的
  `force-for-plugin` 沿用 S4 口径只解析、不生效）。
  MCP 三作用域已于 S8 补齐（见上表；四处偏离：项目层用 `<workspace>/.mcp.json`
  与 Claude 一致，但本机层放在 `<workspace>/.blank/mcp.local.json`，而不是 Claude
  把它塞进 `~/.claude.json` 的项目键——本机的项目私有配置随工作区目录走，拷走工作区
  不会带走批准；批准记录单独存 `mcp-approvals.json`，只存整条定义的 sha256 并按
  「作用域 + 工作区路径哈希 + 服务端名」绑定，不像 Claude 那样把批准状态连同原始
  配置写进项目私有段（因此 `env` / `headers` 里的密钥不会多一份副本）；没有 `/mcp`
  交互命令与 OAuth 浏览器授权流程，批准只走设置页与 `POST/DELETE /settings/mcp/approvals`；
  没有服务端级工具 allow/deny 过滤（Claude 能逐工具审批与禁用），本机只按整条定义
  批准或撤销）。
  settings.json 权限规则已于 Z 批补齐（见上表；五处偏离：项目层与本机层的
  `permissions` 仍被安全段保护忽略，克隆来的仓库不能替用户放宽审批；MCP 工具
  （`mcp__server__tool`）与 `WebFetch(domain:)` 规则没有接入本机审批链——allow 只记
  note 并跳过、deny/ask 让整份 settings 规则停用；`Bash` 前缀按 token 边界判定，
  与上游 `startsWith(f+" ")` 等价但不是逐字节比较（多余空白不影响命中）；
  `Task` / `Agent` 都映射到本机 `spawn_agent`、`MultiEdit` 与 `Edit` 都映射到
  `edit_file`，因此无法按 Claude 的独立工具名分别写规则；子代理沿用 #112 的口径，
  策略对象不带 ruleset——settings 规则只作用于主会话的审批链）。

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
- **真实模型评测的「校验全是 skipped」已经关闭**（2026-10-29，`results-2026-10-29T03-16-11-053Z.json`）：
  同一份 32 样例、DeepSeek、隔离库 :8060，`validation.status` 变成 **23 ok / 5 skipped / 4 未走到验证**，
  `success_rate 0.903`、`acceptance_coverage 0.84`。5 个 skipped 各有明确理由：T11/T28 要 Go 工具链
  （本机没有，见下一条），T18 是被正确拒绝后本来就没有改动，T20/T26 是纯查询样例。
  历史那条「32 个全 skipped」的口径作废，读数保留在 `results-2026-10-22T07-43-54-994Z.json`。
  这一轮还顺带量出一条真缺陷并修掉：本机档用 POSIX 规则拼 argv，`cmd.exe` 不认单引号，
  `python -m pytest -q` 这类带绝对路径的命令一律报「文件名、目录名或卷标语法不正确」——
  T01 修复前 `validation.passed=false`，修复后 `exit 0 / 7 passed`。
  跑法有讲究：**别加 `--approve-high`**，它会把 T18/T19 两个「期望被拒」样例直接放行，
  `not_denied` 会看起来像安全缺口（本轮实测过一次）。标准口径是让它挂起、由判据驳回。
- **T23 / T32 仍停在 `waiting_approval`**：模型用 `request_user_input` 提问，而评测装置不会回答，
  这不是产品缺陷，是装置缺一环（要等用户答）。T16 同样因模型自问自答收尾而记 `error`。
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
  管理员权限下显式打开 `BLANK_SANDBOX_APPCONTAINER=1` 时改走 AppContainer（零能力包），
  文件与网络边界由内核拒，两条账本只认同一个子进程里的行为自检（见下条）。
- **内核升级里已落地与仍冻结的项**：CPU 限速（Job `CpuRate` + 行为探针，账本要求
  "Job 接受限速"与"本机实测压得住"同时成立）、IO 速率上限（Job IO rate 48 字节
  `NATIVE_V1` 布局，默认 128MB/s；行为探针写两轮盘，限速侧 ≤ 对照×0.7 才写
  `io_enforced=true`；docker 命令行没有带宽参数，容器档恒 false）、**沙箱临时区容量上限**
  （固定容量 VHDX 卷，`BLANK_SANDBOX_DISK_MB` 默认 2048；探针 64MB 写满拿到内核
  `errno=28 ENOSPC` 且第二次写仍失败才写 `disk_enforced=true`，只约束 TEMP/TMP/`BLANK_SCRATCH`）、
  **Windows 网络出口强制**（管理员 + `BLANK_SANDBOX_APPCONTAINER=1`：零能力包里外连被
  `PermissionError:13` 拒、回环自连正常、宿主对照外连成功；文件与网络两条账本只认这份行为
  证据）、远程 MCP 的 OAuth（RFC 9728/8414 发现 → `client_credentials` → 401 刷新一次；
  受众与过期在本机核对，令牌只进内存）已落地；更新源改成 Ed25519 **分离签名**（公钥钉在
  桌面壳里，下载前验签、装前复验）并随包生成 CycloneDX SBOM（由 `package-lock.json` 与
  `uv.lock` 生成，带安装包摘要）。**任务级隔离与任务根容量卷已落地**（见上文；同机另一任务包
  用绝对路径与 `..\` 相对路径都被 `PermissionError:13` 拒，64MB 任务卷写满拿到内核 `errno=28`，
  释放后无目录/卷/包残留——2026-11-11 生产路径探针 17/17）。仍冻结的是：**共享工作区**的磁盘
  容量配额（未开任务隔离时工作区写盘不受卷约束）、第二台机器差异对账、以及任务级隔离还没验的
  两格（另一 OS 用户不可达、junction/符号链接绕过）；Linux 原生后端按项目决定
  **移出范围**，判据只在 CHANGELOG 留档，不计入待办；
  另有一项发布外部条件：exe 的 Authenticode 代码签名需要公司证书（本机 `sign:check`
  退出码 2，只出未签名包）。它们依赖尚不可用的内核接口、第二台机器或真实证书，
  全部冻结在 `CHANGELOG.md` 的「未验证」一节，逐项写了判据、红了长啥样与哑弹风险，
  **不计入「已完成」**。
- **数据库：普通用户什么都不用装。** 这里要说的是**服务端**：默认与桌面版都是单个 SQLite 文件（开发库
  `sqlite+aiosqlite:///./data/blank_dev.db`；桌面包内由壳层把 `BLANK_DATABASE_URL` 指到数据目录下的
  `blank.db`，见 `desktop/main.cjs`）。`MySQL` / `PostgreSQL` 是**可选部署形态**——形态的代价是服务端要你自己起。
  驱动侧的实话分两句，按 0.8.5 包内运行时（`desktop/dist/win-unpacked/resources/backend/runtime/Lib/site-packages`）
  实测：`pymysql` / `aiomysql` **不在包里**（挂在 `pyproject` 的 `mysql` extra，`uv sync` 默认不装）；
  `asyncpg` **在包里**（它是核心依赖，`pyproject` 第 9 行）——装了驱动不等于用了它，产品默认连的还是那个
  SQLite 文件，这一条只是「包里多一份没人调用的代码」。
  这两种方言的迁移历史、两条建库路径的 schema 对账都实测过，真 HTTP 端到端装置在 MySQL / PostgreSQL
  上各有 **85 步全绿**的实物（2026-10-29 CI 五格全绿那一轮，三方言各自 85 步 0 失败）。
  这些不改变一句实话：单机产品用不到它们。
  方言兼容层自 2026-10-26 起**冻结**：只保留 CI 的 `api-dialect` 作为回归防线，不再开新批次。
- 沙箱镜像带 Python/Node/Git，但没有预装各语言依赖，也没有 Go 工具链：需要 `npm install` 或 Go 的样例仍跑不出真实结果。
- `.github/workflows/ci.yml` 的五个 job **2026-10-29 才第一次在 GitHub runner 上真跑**（本机没有 `gh`,
  此前只有"已就位"没有"跑过"）。首轮五个全红，逐条查日志后没有一条是产品缺陷——全是"本地脏树有、
  干净 clone 上没有"或"本机姿势与 runner 姿势不同"：`typecheck:web` 依赖 Next 生成物、
  `sandbox/Dockerfile` 写死 uid 1000 撞上 `node:24-alpine` 自带的 `node` 用户、`../evals/` 少一级、
  `--skip-web` 的退出码 3 被当成失败、`setup-uv@v5` 的 post 步骤 `uv cache prune` 挂掉。
  修完这几条后 web / team-e2e / eval-gate / api 四格已绿；`api` 那格在 Linux 上接着量出
  23 failed / 1601 passed——其中**三处真缺陷**（POSIX 钩子取消只杀外层 `sh`、事件 `seq` 并发撞号、
  MCP 服务端秒退时 stderr 被跳过）、**一处用户名泄漏**（路径掩码只认本平台分隔符），
  其余是只在 Windows 上成立、缺 `skipif` 的判据；修完这些 runner 上 `api` 转为全绿
  （1611 passed / 83 skipped）。
- **桌面那两格是后补的，2026-10-31 才进 CI**：`desktop`（`pnpm test:desktop` 52 例 + 版本口径 +
  判分器纯函数单测）与 `desktop-package`（静态导出 → 组装运行时 → `--dir` 出包 → `verify-package`）。
  此前这两类判定只在打包人的机器上跑过。`desktop-package` 能成立的前提是运行时组装有了脚本
  （`scripts/prepare_desktop_runtime.mjs`），否则 CI 里根本造不出那 129MB 的 `build/runtime`，
  "打包自检"就只能继续靠手工。脚本产出的运行时在本机实测：62 个包 / 113.8MB，包内方式
  `import blank_api` 落在 `api/src`，八条边界探针装置可信、0 个不确定项。
- **评测门禁 2026-10-31 实测是红的，原因不在门禁本身**：同一天用仓库自带脚本化桩跑全集
  （32 例、并发 3、隔离库 :8050），`success_rate 0.875`（succeeded 26 + expected_denied 2 = 28/32，
  阈值 0.938），红的是 T07/T29 `files_missing`（桩的确定形状，与 `evals/gate_config.json` 里记的一致）
  **外加 T11/T28 `failed`**。后两条是 Go 样例：当时代码已能识别 `go.mod`，但缺 Go 工具链时
  执行层把"跑不了"归成了失败，任务据此被判 `failed`。这是**产品缺口**
  （基线 0.938 是 2026-10-26 在工作区另一版上量的，之后「计划持久化/里程碑」几波让这类样例
  真的走到了验证节点），不是阈值该改。
  **2026-11-09 已补齐工具侧那半**：`run_tests` 在执行前探测运行器，缺工具链时返回 `status=skipped` +
  「本机没有 go 工具链」；验证节点把工具侧的 skipped 原样落成 `validation.completed`
  （`skipped_reason` 进事件与 `unverified_risks`），不再把「跑不了」归成代码失败、也不再空转修复回路。
  同日复测（`evals/results/results-2026-11-09T04-47-06-167Z.json`）：T11/T28 的 `validation.status=skipped`，
  但任务最终仍是 `failed`——脚本化桩的固定交付文案照样声称「测试通过」，`no_false_claim` 按实判红
  （`交付文本声称测试通过，实际验证结果不支持`）。门禁本机 **0.875 < 0.938**，红 T07/T29 `files_missing`
  外加 T11/T28；要不要给评测机装 Go 仍由部署决定，**0.938 没动**。
  上面 2026-10-31 的读数保留为当时的实测，不按新代码重写。
- `api-dialect`（三方言端到端，**85 步** × 三方言）**2026-10-29 在 runner 上转绿**：
  判定行是「每一格全绿：对照判据成立」，sqlite / mysql / postgres 各 85 步 0 失败。
  它此前红的 4 步（第 80/83/84/85 步）**不是方言差异**，是"桩每轮都发同一个 `call_1`，
  而产品按调用 id 从执行日志里认回上次结果"叠出来的真缺陷：同一任务的第二条命令被当成
  第一条的重放吞掉——不执行、不进审批门、任务还回 `succeeded`。产品与装置两侧都改了。
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
