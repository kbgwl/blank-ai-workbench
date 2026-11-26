# D4 开发交接单

## 任务与范围

- 已实现：
  - **依赖漏洞审计与修复**
    - 前端：`pnpm audit --prod`（官方 registry）发现 7 条 moderate，全部在
      `next@16.4.1`；升级 `next` / `eslint-config-next` 到 **16.4.6**，审计归零。
    - 后端：`pip-audit` 发现 13 条（3 个包）：`certifi`、`opentelemetry-api`、
      `pypdf`；升级到 `certifi 2026.11.20`、`opentelemetry-api 1.45.2`、
      `pypdf 6.21.4`（不上 pypdf 7 大版本），审计归零。
  - **严格 CSP（Next / 桌面）**
    - `web/scripts/csp.mjs`：构建后按页面实际内联脚本算 SHA-256，注入
      `<meta data-blank-csp http-equiv="Content-Security-Policy">`。
      `script-src 'self' 'sha256-…'`，**不含 `'unsafe-inline'`**；覆盖
      `out/`（桌面静态导出）与 `.next/server/app|pages`（`next start` 直接读的
      预渲染 HTML）。
    - `web/scripts/build.mjs`：每次构建成功后自动执行；15/15 桌面页、15/15
      server app 页、2/2 pages 页均注入。
    - 主题引导保留内联（首帧防闪白），由构建期哈希放行；没有为它开
      `'unsafe-inline'`。
    - `style-src` 仍保留 `'unsafe-inline'`：React style 属性需要；脚本边界未放宽。
    - 其余：`object-src 'none'`、`base-uri 'self'`、`form-action 'self'`、
      `frame-ancestors 'none'`（Next 由 `X-Frame-Options: DENY` 补，meta 里不写）、
      `frame-src 'self' blob: data:`、`connect-src` 限本机回环。
  - **Python 弃用迁移**：3 处 `datetime.utcnow()` 改为
    `datetime.now(timezone.utc).replace(tzinfo=None)`，保持库里 naive UTC 语义，
    不改变比较口径。
  - `web/package.json` 的测试脚本包含 `scripts/csp.test.mjs`；`check.sh` 同步。
- 明确未实现：
  - 动态 SSR 页面（当前应用全部静态预渲染）不在构建期哈希 CSP 覆盖范围；
    以后新增动态页面需要另走 nonce 或运行时哈希。
  - 开发模式（`next dev`）不带 CSP；生产构建才注入。
  - 未做 SRI；未把 `style-src` 收到哈希（React 内联样式会大面积破）。
  - `pnpm audit` 需要官方 registry；项目默认的 npmmirror 不提供 audit 端点，
    本次用 `--registry https://registry.npmjs.org`，没有改项目 registry 配置。

## 修改内容

- `web/package.json`：Next 16.4.6、eslint-config-next 16.4.6；测试脚本含 CSP 测试。
- `pnpm-lock.yaml`、`services/api/uv.lock`：依赖升级。
- `web/scripts/csp.mjs`（新）、`web/scripts/csp.test.mjs`（新）、
  `web/scripts/build.mjs`：构建期严格 CSP。
- `web/src/app/layout.tsx`：主题引导内联脚本保留，注释说明由构建期哈希放行。
- `services/api/src/blank_api/api/routes/team.py`、`services/team.py`、
  `services/threads.py`：`utcnow` 迁移。
- `check.sh`：测试命令包含 `scripts/csp.test.mjs`。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `pnpm audit --prod --registry https://registry.npmjs.org` | **0 vulnerabilities** | 升级前 7 条 moderate（全在 next） |
| `pip-audit -r logs/api-requirements-audit.txt` | **0 vulnerabilities** | 升级前 13 条（certifi / opentelemetry-api / pypdf） |
| 前端 ESLint `--max-warnings=0` | 0 problems | — |
| 前端 `tsc --noEmit` | 0 | — |
| 前端单测（含 CSP 测试） | **443 passed** | — |
| 服务端构建 `node scripts/build.mjs` | 退出码 0 | CSP 注入 `.next/server/app 15/15`、`pages 2/2` |
| 桌面静态导出（`BLANK_DESKTOP_BUILD=1`） | 退出码 0 | CSP 注入 `out 15/15` |
| `next start :3010` 浏览器探针 | `ok:true`、**0 控制台错误、0 失败请求** | 严格 CSP 下页面正常、数据正常 |
| 静态导出探针（:4180） | 页面正常渲染 | 仅静态服务器没有 `/api` 代理的 404，无 CSP 违规 |
| 桌面单测 | **104 passed** | Next/依赖升级后 |
| 评测脚本单测 | **25 passed** | — |
| `bash check.sh --no-smoke --no-acceptance` | 全部通过 | tsc / eslint / 443 单测 |
| 后端相关测试（上传/附件/团队/会话） | **66 passed** | pypdf/certifi 升级与 utcnow 迁移后 |
| 后端全量 `pytest -q` | **5001 passed / 0 failed / 28 skipped，17:15，退出码 0** | 依赖升级后全量复跑；warning 从 60 降到 20（utcnow 已清） |

### 附：CSP 产物形状

- `out/index.html` / `.next/server/app/index.html` 的 `<head>` 首部有
  `<meta data-blank-csp ...>`；`script-src` 无 `'unsafe-inline'`，含 4 个
  `sha256-…`（含主题引导）。
- 重新构建时旧 meta 会被替换，不会叠加。

## 风险与需要审查的点

- Next 16.4.1 → 16.4.6 是 patch 级升级，但仍建议人工点验一次主要页面；
  前端 443 单测、服务端/静态构建、浏览器探针均通过。
- pypdf 6.21.4 是 6.x 最后一个修复版；未来若升 7.x 需要单独评估 API 兼容。
- `csp.mjs` 依赖构建产物的 HTML 形态（`<script>` 正则）；Next 大版本若改
  内联脚本形状，需同步更新并跑 `csp.test.mjs`。
- CSP 的 `style-src 'unsafe-inline'` 是有意保留；脚本边界仍是严格哈希。
- 动态 SSR 页面不在覆盖范围（当前无此类页面）；新增时需补 nonce。
- 旧 `out` 目录中残留页面也会被注入；打包前建议先清理再构建。

## 建议人工验收

1. `pnpm audit --prod --registry https://registry.npmjs.org` 与
   `pip-audit -r logs/api-requirements-audit.txt` 看 0。
2. 打开 `http://127.0.0.1:3000`（开发）与 `http://127.0.0.1:3010`（生产构建）
   各点一遍主要页面；生产页面的 meta CSP 不应产生控制台 CSP 错误。
3. 用桌面静态导出 `out/` 打开页面，确认页面正常、无 CSP 违规。
4. 检查 `out/index.html` 的 `script-src` 不含 `'unsafe-inline'`。
