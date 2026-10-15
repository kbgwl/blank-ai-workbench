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
| `BLANK_SANDBOX_ENABLED` | `true` | 关闭后退回本机子进程，仅适合开发机 |
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
