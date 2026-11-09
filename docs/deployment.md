# 部署指南

面向第一次接触本仓库的开发者：从零到可运行的空白格 AI 操作台。

## 1. 前置条件

| 依赖 | 版本 | 检查命令 |
| --- | --- | --- |
| Node.js | ≥ 22 | `node -v` |
| pnpm | 10.4.1 | `corepack enable pnpm && corepack prepare pnpm@10.4.1 --activate` |
| uv | 最新版 | `uv --version`（没有则 `curl -LsSf https://astral.sh/uv/install.sh \| sh`） |
| Docker | 提供 PostgreSQL 16 / Redis 7 / 沙箱镜像 | `docker info` |

不需要预装系统 Python，后端通过 `uv run` 使用 uv 自带的 Python 3.12。

## 2. 安装

```bash
pnpm install                          # 安装前端依赖并建立 workspace 链接
cp .env.example services/api/.env     # 后端配置，变量前缀 BLANK_
uv sync --project services/api        # 安装后端依赖
```

## 3. 数据库

默认使用 Docker 里的 PostgreSQL：

```bash
docker compose up -d --wait
cd services/api
uv run alembic upgrade head
```

不想用 Docker 时改成 SQLite（`services/api/.env`）：

```ini
BLANK_DATABASE_URL=sqlite+aiosqlite:///./data/blank_dev.db
```

迁移必须同时在 SQLite 与 PostgreSQL 上往返验证：

```bash
uv run alembic downgrade base && uv run alembic upgrade head
```

## 4. 密钥与供应商凭据

模型的 API Key 以 Fernet 加密存库，接口只回显 `api_key_hint`，不会出现在响应或日志里。
加密主密钥默认落在 `services/api/data/.blank_secret.key`；生产环境请显式设置：

```ini
BLANK_SECRET_KEY=<base64 的 32 字节 Fernet key>
BLANK_API_TOKEN=<对外访问令牌；留空表示本机单用户模式>
```

## 5. 启动

```bash
pnpm dev
```

依次执行 `docker compose up -d --wait` → `alembic upgrade head` → uvicorn (:8000) → next dev (:3000)。
打开 http://localhost:3000 ，顶栏健康指示灯变绿即代表后端与数据库连通。

### 5.1 把执行挪到独立进程（可选）

桌面单机默认由 API 进程内执行（`BLANK_INPROCESS_RUNNER=1`）。要让 API 与执行解耦：

```bash
BLANK_INPROCESS_RUNNER=0 uv run --project services/api python -m blank_api.worker
```

- API 只建 run 并返回 `queued`，启动恢复也不认领；worker 轮询 `queued` 与租约已过期的 run。
- 同一条 run 只认一个 worker：租约 90 秒、心跳续租；失去租约的执行会被取消，终态只有持约人能写。
- 进程被杀后，另一个 worker 等租约过期即接管；已完成的工具调用按 `tool.completed` 执行日志复用，
  不会重复执行（设计与读数见 `docs/REA-WAVE4-CODEX-RUNTIME.md` §7.56）。
- worker 与 API 必须共用同一个数据库与工作区根；`data/artifacts` 也要一起可见，否则接管时
  取不回已完成的结果，只能再执行一次。

## 6. 沙箱执行环境（阶段6）

Agent 的 shell 命令默认在一次性 Docker 容器里执行：工作区是唯一可写挂载点，默认无网络，
CPU/内存/进程数/文件大小/时长都有上限，超时即销毁容器。

```bash
docker build --pull=false -t blank-sandbox:dev sandbox
# 本机无法访问 Docker Hub 时，用已有 alpine 基座构建：
docker build --pull=false --build-arg BASE_IMAGE=postgres:16-alpine -t blank-sandbox:dev sandbox
```

策略开关（`services/api/.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BLANK_SANDBOX_ENABLED` | `true` | 只关掉整个沙箱档：命令在宿主上跑、不挂 Job（仅适合开发机） |
| `BLANK_SANDBOX_ALLOW_LOCAL` | `true` | 没有可用容器后端时退回本机 Job 受限执行；设 `0` = 只准容器，无容器则整任务 409 |
| `BLANK_SANDBOX_IMAGE` | `blank-sandbox:dev` | 沙箱镜像 |
| `BLANK_SANDBOX_CPUS` | `1.0` | CPU 上限 |
| `BLANK_SANDBOX_MEMORY` | `512m` | 内存上限，同时禁止 swap |
| `BLANK_SANDBOX_PIDS` | `128` | 进程数上限（含 `--ulimit nproc`） |
| `BLANK_SANDBOX_MAX_FILE_KB` | `262144` | 单文件大小上限（`--ulimit fsize`） |
| `BLANK_SANDBOX_NETWORK` | `false` | 需要联网装依赖时再打开 |
| `BLANK_SANDBOX_TIMEOUT` | `120` | 单条命令超时秒数 |
| `BLANK_SANDBOX_ENV_ALLOWLIST` | 空 | 追加允许透传的环境变量名，逗号分隔；带 KEY/TOKEN/SECRET/PASSWORD 的一律拒绝 |

残留容器可用 `docker ps --filter label=blank.sandbox=1` 检查，正常情况下为空。

## 7. 验证与回归

```bash
pnpm test:api        # 后端 pytest
pnpm typecheck:web   # 前端类型检查
pnpm build:web       # 前端生产构建
pnpm regression      # 迁移往返 + 后端测试 + 前端类型检查/构建
pnpm regression --evals   # 追加跑 32 个评测样例（需要后端已在运行）
```

## 8. 生产部署要点

- 用 `uv run uvicorn blank_api.app:app --host 0.0.0.0 --port 8000` 起后端，前置反向代理并强制 `BLANK_API_TOKEN`。
- 前端 `pnpm build` 后按 Next.js 自托管方式运行，`/api/:path*` 反代到后端。
- PostgreSQL 换成独立实例并做定时备份；`data/snapshots`（覆盖前快照）与 `data/audit`（审计日志）需要留存。
- 沙箱容器不要与数据库同宿主机共享敏感挂载点；工作区目录只挂载项目本身。
- 多进程部署时（`BLANK_INPROCESS_RUNNER=0` 或 uvicorn 多 worker），`data/artifacts` 必须共享可见：
  worker 接管别人的 run 时要从这里取回已完成的工具结果，取不到就只能再执行一次。
- `BLANK_RATE_LIMIT_RPM` 默认 **120**（每 IP 每分钟）。批量客户端（评测装置、迁移脚本、集成方 SDK）
  几十秒就能把窗口打满，然后看到一片 429——那不是服务挂了，是装置和限流在打架。给这类受控实例
  显式放宽或设 0 关掉；**产品默认值不要改**，那是对外暴露面的保护。

## 9. 桌面版（Windows 安装包）

桌面包是**两段式产物**：先静态导出前端，再由 electron-builder 打壳。少了第一段，打包照样
"成功"，但包里是上一版前端——界面能开、点进去 404，这类事故以前真发生过。

```bash
# 1. 前端静态导出（这两个变量缺一不可）
BLANK_DESKTOP_BUILD=1 NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000 pnpm build:web
# 2. 组装随包 Python 运行时（约 123MB，不入库；本机第一次要下载独立 CPython）
pnpm runtime:desktop
# 3. 出包（版本号只改 desktop/package.json）
cd desktop && node node_modules/electron-builder/cli.js --win nsis portable
# 4. 打包后自检 44 项：产物齐不齐、asar 里是不是仓库这一版、包内有没有本机痕迹
pnpm verify:desktop
```

- **打包器需要 `npm` 在 PATH 里**：electron-builder 收集依赖时会调 `npm ls --json`，
  找不到 `npm` 只报 `⨯ No JSON content found in output`（看起来像构建坏了，实际是 PATH 少一项）。
  本机 Node 装在 `E:/env/node/node-v24.20.0-win-x64` 时，先把该目录加进 PATH 再出包；
  包内 Electron 也可以用本地解压目录（`--config.electronDist=<dir>`）省掉联网下载。
- **版本号只有一处**：`desktop/package.json`。安装包文件名、托盘「关于」都取它；
  根 `package.json` 与 README 的「当前发布版本」必须同值，`pnpm check:versions` 会拦。
- **运行时不再靠手搓**：`scripts/prepare_desktop_runtime.mjs` 用 uv 的独立 CPython +
  `uv.lock` 冻结导出依赖现场组装，布局与 `verify-package.cjs` 断言的完全一致
  （`python.exe` / `Lib/site-packages` / `blank_api.pth` = `../../../api/src`）。
  这一步以前只存在于打包人的机器上，所以"打包自检"进不了 CI——现在能了。
- 装到本机：`node desktop/install-local.cjs`；只想同步源码不重装：
  `node desktop/self-deploy.cjs --api|--web`。**不要手改 `resources/` 下的副本**，
  客户端读的是它。
- CI 两格：`desktop`（单测 + 版本口径）与 `desktop-package`（静态导出 → 组装运行时 →
  `--dir` 出包 → `verify-package`）。

### 9.1 代码签名（发公开版前必做）

electron-builder 原生认两个环境变量，不需要改 `package.json`：

```bash
CSC_LINK=<pfx 文件路径或 base64> CSC_KEY_PASSWORD=<口令> \
  node node_modules/electron-builder/cli.js --win nsis portable
```

签名是否真的生效，按产物断言，不看构建日志（日志里即使没证书也会打印 `signing with signtool.exe`）：

```powershell
Get-AuthenticodeSignature 'dist\Blank-AI-安装版-<version>.exe' | Select-Object Status
# 期望 Valid；没有证书时是本机的 NotSigned
```

没有证书也能出包、能运行，但用户首次安装会撞 SmartScreen「未知发布者」。
**这是外部采购项（OV/EV 证书），仓库里没有、也不应有证书与口令。**

### 9.2 自动更新源

桌面壳只在配置了 `BLANK_UPDATE_FEED_URL` 时检查一次更新；feed 必须给
`version` / `url` / `sha256` 三样，缺一样就拒装（`desktop/shell-policy.cjs::decideUpdate`）。
用脚本生成，别手写：

```bash
BLANK_UPDATE_BASE_URL=https://dl.example.com/blank pnpm feed:update
# → desktop/dist/latest.json（含 version / url / sha256 / size）
```

脚本会把安装包的实际 sha256 算进 feed，并用壳端同一套纯函数自检
（旧版本能升级、同版本不提示、缺摘要被拒、半包被拒）；自检不过不写文件。
把 `latest.json` 与安装包一起放到 `BLANK_UPDATE_BASE_URL` 指向的目录，
客户端侧再配同名环境变量即可。

已知边界：下载与安装由用户点托盘确认（不静默替换），摘要不符会丢弃；
本仓库未内置更新服务器，托管地址属于部署方配置。

## 10. 数据生命周期与磁盘占用

四类数据会随使用增长，处理方式是**一类的规则写在同一个地方**
（`services/api/src/blank_api/services/maintenance.py`），不散落在业务路径里。

| 数据 | 位置 | 增长原因 | 规则 |
| --- | --- | --- | --- |
| artifact | `data/artifacts/<task>/<call>.json[.gz]` | 每次工具调用的完整输出 | 32KB 以上 gzip；单份上限 8MB（`BLANK_ARTIFACT_MAX_BYTES`），超出按 output → data → 骨架逐级裁；**过期 30 天且无 ToolCall 引用**才可删 |
| snapshot | `data/snapshots/<id>/` | 覆盖工作区前的副本 | 每任务至少留最近 3 份；任务被删后的快照随孤儿一起收 |
| event | 表 `agent_events` | 每步一条时间线 | 超过 90 天的**终态**任务：payload 里的大值裁短并留标记，**行不删**（删行等于让界面上那一步消失） |
| usage | 表 `model_usage` | 每次模型调用一行账 | **不自动清理**：行即历史事实，删行等于改账 |

```bash
# 看占用（artifact / 快照 / 事件 / 用量四块，含可回收量）
curl -s http://127.0.0.1:8000/api/v1/maintenance/storage
# 清理默认 dry-run：不写 dry_run: false 就只报"会删什么"
curl -s -XPOST http://127.0.0.1:8000/api/v1/maintenance/purge \
  -H 'content-type: application/json' \
  -d '{"artifacts":{"ttl_days":30},"snapshots":{"keep_per_task":3},"events":{"retention_days":90}}'
```

三条纪律，改这一块之前先读：

1. **引用保护优先于时间**：被 `ToolCall.artifact_path` 指着的东西，再旧也不删
   （报表里单列 `expired_but_referenced`，那是"该涨保留期"的信号，不是"该删"的信号）。
2. **默认 dry-run**。这些文件是用户排障时的证据，自动删掉之后"为什么这条任务打不开"就再也查不出来了。
3. **能续跑的数据一个字不动**：`running` / `queued` / `waiting_approval` / `paused` 的
   payload 是崩溃续跑与重放的材料，压缩它们等于把 `tool.completed` 执行日志裁短。
