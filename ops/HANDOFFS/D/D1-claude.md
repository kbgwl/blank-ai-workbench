# D1 开发交接单

## 任务与范围

- 已实现：
  - 修掉生成应用 `data/_shopbench_rerun` 的「AI 味」：去掉渐变、卡片阴影、圆角胶囊与
    图标墙，改成纸面/石墨/单一琥珀强调色的工业工作台；AI 视图改成「任务 / 结果 / 日志」
    三栏；KPI 改成带分隔线的读数条；监控读数补上被漏掉的单位。
  - 生成应用的安全与健壮性：
    - 静态页与接口统一安全头：CSP（`default-src 'self'`、`frame-ancestors 'none'`）、
      `nosniff`、`DENY`、`no-referrer`、`same-origin`、`Permissions-Policy`；
    - 静态路径解码失败 / 空字节返回 400，路径必须在 `public/` 内；
    - 请求体上限 256 KiB；
    - AI 配置只接受 http(s)、拒绝带账号密码的 URL、长度上限；配置写盘后 `chmod 600`；
    - `patch` / `code` / `level` / `task` 等输入做类型与取值校验，非法输入返回 400 而非 500。
  - 把这两类要求写回生成侧的系统提示：`CODING_INSTRUCTIONS` 新增「前端不要做通用 AI
    仪表盘」与「后端/脚本安全底线」两条，后续生成任务默认带上。
- 明确未实现：
  - 没有对主仓库（Next.js 前端 / FastAPI 后端）做全量代码规范与安全审计；
    本轮只覆盖生成应用 + 生成侧提示约束。全仓审计需要单独立卡。
  - 没有做像素级视觉验收；以浏览器探针 + 截图人工确认为准。

## 修改内容

- `data/_shopbench_rerun/public/styles.css`：工业风视觉覆盖层（纸面/石墨/琥珀、平面面板、
  细线分隔、等宽读数、隐藏图标墙、三栏工作台、响应式收窄）。
- `data/_shopbench_rerun/public/index.html`：AI 视图改为 `.ai-workbench` 三栏；液位罐配色
  从青绿改琥珀。
- `data/_shopbench_rerun/public/app.js`：LCD 单位固定为 `m`；曲线配色改琥珀/钢灰/石墨。
- `data/_shopbench_rerun/server.js`：安全头、请求体上限、静态路径防护、输入校验、400 语义。
- `data/_shopbench_rerun/lib/ai.js`：Base URL 只允许 http(s)、禁止账号密码、长度上限、
  配置写盘 `chmod 600`。
- `data/_shopbench_rerun/test/server.test.js`：安全头、非 http(s) URL、patch 类型、
  未知故障码四组回归。
- `services/api/src/blank_api/services/agent/graph.py`：`CODING_INSTRUCTIONS` 增加反 AI
  仪表盘与安全底线两条。
- `services/api/tests/test_output_styles_v47.py`：钉住的 SYSTEM_PROMPT 哈希随提示扩充更新，
  注释记录本次加入的两条。

## 验证结果

| 命令 | 结果 | 摘要 |
|---|---|---|
| `node --test`（`data/_shopbench_rerun`） | 22 passed / 0 failed | 含新增安全头、URL 协议、patch 类型、未知故障码用例 |
| 浏览器探针（`http://127.0.0.1:4173`） | 9/9 断言通过 | 监控视图单位 `m` 且无 `undefined`；AI 三栏；六视图切换正常；0 控制台错误 |
| `pytest tests/test_output_styles_v47.py tests/test_coding_discipline_v26.py tests/test_prompt_parts_v427.py tests/test_two_scenes_v401.py` | 99 passed | 提示词合成、样式替换、默认组合逐字节一致 |

## 风险与需要审查的点

- 生成的 app 仍在 `data/_shopbench_rerun`（未纳入主仓库），仅供查看与复跑；
  默认端口 3000 与主应用冲突，本次用 `PORT=4173 node server.js`。
- 安全头里的 CSP 为 `'self'` + 内联样式白名单外无外部资源；若后续加外部字体/CDN 会被拦。
- `chmod 600` 在 Windows 上可能无效，密钥首选环境变量。
- `CODING_INSTRUCTIONS` 变长会增加每次请求的固定 token；本次仅两条，未做压缩。

## 建议人工验收

1. 打开 `http://127.0.0.1:4173`，看总览 / 监控 / AI / 参数 / 设置五页是否还有「AI 味」。
2. 在 AI 页跑一次诊断，确认三栏内容与日志正常、无控制台错误。
3. 用 `curl -I http://127.0.0.1:4173/` 看 CSP / nosniff / frame 头是否生效。
4. 建一条新的编码任务，确认生成提示带上反 AI 与安全底线。
