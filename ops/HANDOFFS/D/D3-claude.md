# D3 开发交接单

## 任务与范围

- 已实现（主仓库全量代码规范与安全审计的第一轮，不含新功能）：
  - **前端规范清零**：ESLint 从 6 error / 4 warning 清到 **0**；`lint` 与
    `check.sh` 都改为 `--max-warnings=0`（以后一个 warning 也会让门禁失败）；
    `tsc --noEmit` 0。
    - `app-rail.tsx`：侧栏收窄改用 `useSyncExternalStore` 读 localStorage；浮层按
      `pathname` 记账，切页自动失效；`window.location.href` 改 `router.push`。
    - `turn.tsx`：思考计时器不再在 effect 体内同步 setState。
    - `agent-profile-panel.tsx` / `trigger-panel.tsx` / `file-panel.tsx`：用
      「这份数据属于哪个 key」推导加载态，换项目/换文件不靠 effect 里清空。
    - 清掉两个未使用类型导入。
  - **Markdown 链接白名单**：`[x](javascript:...)`、`data:`、`file:`、`//host`
    不再渲染成可点链接；只放行 http(s)/mailto/站内路径/锚点。
  - **API 安全头**：新增 `SecurityHeadersMiddleware`，所有响应带
    `nosniff`、`DENY`、`no-referrer`、`same-origin`、`frame-ancestors 'none'`。
  - **可执行文件降级**：上传附件与工作区 `/file/raw` 的 html/svg/xml/js/css
    一律按 `text/plain` 返回，避免在 API 源上执行脚本。
  - **供应商地址校验**：`base_url` 只允许 http(s)、拒绝 URL 内账号密码。
  - **鉴权比较**：`require_token` 改用 `secrets.compare_digest`。
  - **校验错误脱敏**：422 不再回显 Pydantic 的 `input`（可能含 API Key）。
  - **桌面壳**：
    - `window.open` / 主窗口导航只把 http(s) 交给系统浏览器；
      `file:`、`ms-msdt:` 等一律拒绝；站内 `app://bundle` 的 `target=_blank`
      改为当前窗口导航，不会被当成外链丢掉；
    - `blank:open-path` 只允许目录，避免桥变成任意程序启动器；
    - `app://` 静态资源边界按路径分隔符对齐（`web-evil` 不算 `web` 子目录），
      非法百分号编码/空字节直接 404；
    - `app://` 响应补 nosniff/DENY/no-referrer。
  - **权限修正（全量测试抓到的真问题）**：`host_bypass` 原先被误加进会话级授权，
    与「越界执行只能逐条批」的安全不变量冲突；已从 `SESSION_GRANT_LABELS` 移除，
    `session_grant_category` 对越界执行返回空。T-A1 交接单已同步修正。
  - **工具说明预算**：压缩 `set_goal` 的 schema 文案，常驻工具总量回到
    22500 字符预算内（原 22626）。
  - **bash 解析**：跳过 WindowsApps 的 WSL 启动器（`bash -c` 会挂住），
    继续在 PATH 里找 Git Bash/MSYS2；找不到才回落 host shell。
  - **HTTP 钩子回环不走代理**：本机钩子地址用 `trust_env=False`；某些环境里
    HTTP_PROXY 会把 127.0.0.1 也拦走，连不上就变成 503，看起来像钩子服务坏了。
    修复后 `test_hook_kinds_af3c` 18 passed，没有改测试。
  - **自检入口**：`check.sh` 增加前端 ESLint 步骤，和 CI 的 `lint:web` 对齐。
- 明确未实现：
  - 未引入 ruff/mypy（venv 里没有，按合同不安装新依赖）；Python 侧用全量测试
    + 手工审计。
  - 未做依赖漏洞扫描（需要联网/新工具）。
  - 未迁移 `datetime.utcnow()` 等 59 条弃用告警（涉及 naive/aware 语义，单独立卡）。
  - 未给 Next 页面加严格 CSP（需要 nonce/构建改造，先加 nosniff/DENY/referrer）。

## 修改内容

- 前端：`web/src/components/app-rail.tsx`、`bot/turn.tsx`、`bot/bot-app.tsx`、
  `bot/markdown.ts`、`bot/markdown-view.tsx`、`bot/markdown.test.ts`、
  `settings/agent-profile-panel.tsx`、`settings/trigger-panel.tsx`、
  `tasks/file-panel.tsx`、`tasks/timeline-blocks.tsx`、`web/next.config.ts`。
- 后端：`helpers/middleware.py`、`app.py`、`api/auth.py`、`api/exception_handlers.py`、
  `api/routes/uploads.py`、`api/routes/files.py`、`schemas/model.py`、
  `services/permissions.py`、`services/tools/plan.py`、`services/prompt_args.py`。
- 桌面：`desktop/main.cjs`、`desktop/shell-policy.cjs`、
  `desktop/tests/main-wiring.test.cjs`、`desktop/tests/shell-policy.test.cjs`。
- 测试：新增 `services/api/tests/test_security_headers_v493.py`；
  `test_session_approval_grants.py` 修正越界类别期望；
  `test_fs_broker_v93.py` 改用 `.bin` 验证二进制拒读（`.png` 走图片语义）。
- 自检：`check.sh` 增加 ESLint。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| 后端全量 `pytest -q`（审计首轮） | 4990 passed / 5 failed / 28 skipped，17:55 | 5 条：host_bypass 会话档、工具预算、bash WSL 挂起、fs_broker 测试用错文件、hook 回环被代理；已全部修复 |
| 后端全量 `pytest -q`（第二次） | 5000 passed / 1 failed / 28 skipped，17:40 | 唯一失败是 hook 测试；该轮启动于 `trust_env` 修复之前，属于旧代码，单跑已 18 passed |
| 后端全量 `pytest -q`（第三次，最终） | **5001 passed / 0 failed / 28 skipped，17:13，退出码 0** | 启动于全部代码改完之后；60 条 warning 均为既有 `datetime.utcnow()` 弃用告警 |
| 本轮相关后端 12 个测试文件 | 167 passed | 安全头/鉴权/权限/上传/工具预算/shell/fs_broker 全过 |
| `test_hook_kinds_af3c.py` | 18 passed | 回环钩子不走系统代理，产品侧修复 |
| 前端 `eslint src` | 0 problems | 原 6 error / 4 warning |
| 前端 `tsc --noEmit` | 0 | — |
| 前端单测 | 440 passed | 新增链接白名单用例 |
| 前端 `node scripts/build.mjs` | 退出码 0 | `next.config.ts` headers 配置通过构建 |
| 桌面单测 | 104 passed | 新增外链协议、openPath 目录限制、路径边界用例 |
| 评测脚本单测 | 25 passed | — |
| `bash check.sh --no-smoke --no-acceptance` | 全部通过 | 新增 ESLint 步骤后仍全过（tsc/eslint/440 单测） |

## 风险与需要审查的点

- `host_bypass` 不再提供会话档：这是有意的安全收紧，用户会在每次越界执行时重新
  看到审批卡；T-A1 的「完全访问少弹卡」仍覆盖删除/推送/迁移等类别。
- `SecurityHeadersMiddleware` 对所有响应加 `frame-ancestors 'none'`；如果未来要做
  可嵌入的页面，需要为那条路由单独放宽。
- Next 的安全头只在非静态导出的服务端生效；桌面静态导出由 `app://` 协议补同组头。
- bash 解析会扫描 PATH 找非 WindowsApps 的 bash；如果机器只有 WSL，则回落 host
  shell，`shell: bash` 的 `!` 块会按 host shell 语义执行（不会挂起）。
- 最终全量 5001 passed / 0 failed 已复跑确认；60 条 warning 是既有
  `datetime.utcnow()` 弃用告警，不是失败。

## 建议人工验收

1. 运行 `./check.sh --no-smoke --no-acceptance`，确认 ESLint/tsc/单测全过。
2. `curl -I http://127.0.0.1:8000/api/v1/health` 看安全头；上传一份 `.html`
   附件，直接打开链接应显示纯文本而不是执行。
3. 在桌面壳里点一个 `file:///...` 外链，应被拒绝且只把 http(s) 交给系统浏览器。
4. 在对话里让模型输出 `[x](javascript:alert(1))`，应只显示文字，不生成可点链接。
