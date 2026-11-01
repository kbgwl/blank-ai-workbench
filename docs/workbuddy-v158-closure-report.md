# v158 发布闭环报告

> 依据 `docs/workbuddy-v158-release-closure.md` §9/§10 口径。所有数字来自本机真实执行（2026-11-01），
> Benchmark 全量基线 provider 为智谱 `glm-4.5-flash`（OpenRouter 免费额度已枯竭，见"过程事实"§B）。

## 自动化测试

- Desktop：57/57 通过（gpu-guard / api-base / shell-policy / update-policy / main-wiring）
- Web：258/258 通过（官方命令 `node --test --import ./test-bootstrap.mjs "src/**/*.test.ts"`）
- Lint：0 error / 0 warning（eslint src）
- TypeScript：tsc --noEmit 通过
- Evals：判分器 10/10；`check:versions` 全部一致（发布版本 0.8.17，CLI 0.1.0）
- Backend：v153 子集 23/23 + handoff e2e 10/10（含本轮新增 2 例记忆开关用例）
- CLI：`node --test tests/cli.test.mjs` 5/5；`node --check src/index.mjs` 通过
- `git diff --check` 干净

## Benchmark（glm-4.5-flash 全量 55 例，`evals/benchmarks/coding/last-run.json`）

- 总任务数：55
- 完成率：71%
- 通过率：71%（39/55）
- 测试通过率：95%
- 误修改文件数：3
- 错误声称完成（falseDone）：3
- 未授权高风险操作：1
- 平均耗时：约 141s/例；总步数 324
- 口径缺口：glm 流式响应未回 usage，`totalTokens` 记 0（记账断点，不属于判分口径）
- 过程事实：首轮全量被智谱 429（错误码 1302）打穿 28 例 setup_failed——后端生产 429 退避
  （2 次，0.5s/1s）保持不动，在评测层加 `--pause`（任务间隔 20s）重跑解决；2 例瞬时
  `fetch failed` 复跑后 1 例转 PASS、1 例确认为真实模型失败。
- 失败任务（16 例，真实原因逐例如下）：
  - 模型中途放弃/误判失败（status=failed、测试可修）：002、008、014、023、035、042（4-4 步放弃）；027（22 步烧完）；028（3 步即弃且没动预期文件）；039（测试已绿但自称失败）
  - 假完成（noFalseDone）：024、038（交付声称完成但判分器核实未达成）
  - 误修改（noUnexpectedChanges）：040、041（修对了目标文件但多动了别的文件）
  - 真实测试失败：043（TypeError）、045（AttributeError，改坏了 docstring 断言目标）
  - 审批挂起超时：037（waiting_approval 等到 runner 超时）
- §2 收尾项（012/034/035）均已在 #142/#143 修复并复跑 PASS：034 fixture 断言与
  小数比例语义自相矛盾（改为 `==200.0`）、012 pytest save() 副作用文件加入
  `testSideEffectFiles` 白名单、035 标注「可加测试」。

## npm 真实安装

- tarball：`blank-ai-cli-0.1.0.tgz`（npm pack 产物）
- 干净 prefix 安装：`npm install --global --prefix <clean>` 成功，shim 指向安装版代码
- help：通过；doctor：通过（环境自检输出正常）
- start：通过（--workspace 拉起后端 + 静态站点）
- 服务启动：/health 200、数据库 connected；安装版 CLI 无迁移步骤的缺陷已修
  （launchApi 前跑 `alembic upgrade head`，重装 tarball 后干净库启动验证通过）
- 附带修复：CLI 静态服务加 `/api/v1` 同源反代 + 目录深链/SPA 回退；
  `web/src/lib/http.ts` 无壳注入时改用相对路径（烘焙绝对地址只在桌面壳成立）；
  composer「未绑定目录」误报改判 `activeWorkspace`

## Desktop 真实运行

- 包内 Python：`resources/backend/runtime/python.exe` 可执行（0.8.17 包，verify-package 14 项通过）
- 无系统 Python：PATH 收敛到 `C:\Windows\System32;C:\Windows` 后迁移 + 启动仍成功
- 后端启动：alembic 迁移到 head + uvicorn 起服务成功
- 页面打开：静态导出页可由包内后端/CLI 服务；**GUI 壳（Electron 窗口）在本机无法启动**——
  与 v153 以来的历史记录一致，属本机环境限制，未在本轮解决（见"未完成"）
- 用户数据目录：桌面壳写 `app.getPath("userData")`（DB/secret key/settings 均在用户目录，
  升级不覆盖、卸载不触碰）；CLI 默认 `~/.blank`；NSIS 无删用户数据配置

## GUI 端到端（独立干净库实例，端口 8000）

- 开发场景：PASS——发送真实指令 → 审批 → succeeded；`src/calc.py` 除零 bug 真实修复，
  pytest 3 passed，零误改
- 文档场景：PASS——2 步、零写类工具，交付使用文档
- 日常不记住：PASS（修复后复验）——任务成功、记忆文件字节数不变、无 `memory.consolidated` 事件
- 日常记住：PASS——`memory.consolidated` added:1，真实落盘
  `data/memory/sample-project-5eb6e9fe.md`（"已记住：代码注释一律用中文写"）
- 模型切换：PASS——事件级证据：切前 3 个任务真实发到 anthropic（claude-3-5-sonnet-latest，
  因无 key 失败），切到 glm 后 3 个任务 `turn.context/model` 全为 glm-4.5-flash；不是只看下拉框
- 真实文件修改：PASS（开发场景 `calc.py` 即为真实文件修改）
- 测试失败修复：PASS（同上，含 run_tests 验证）
- 逆向实验室：PASS——`/reverse-lab` 四端点全链路：status ready（rea-artifacts 0.1.0，3 工具，
  stdio 挂 Job Object）；targets 真实扫描（PE 文件识别 format=pe、runnable_guess=true）；
  PE 正例调查 succeeded、4 条结论全带 Evidence ID；无解析器认领的对象如实报
  "按 asar/pe/elf 幻数逐一比对不符"（不冒充结论）；不存在目标 404 真实错误；
  前端措辞为「候选实物」「像是可执行」，不把候选说成已确认

## 本轮新发现并修复的产品缺陷

1. **记忆开关漏管自动固化**：`memory_opt_in=false` 的日常任务成功后，任务结论仍被自动固化
   进项目记忆（开关只挡了 `memory_write` 工具）。修复：`runtime.py` 收尾处「daily 且未选记住」
   跳过 `_consolidate`；补 e2e 正反两例（`test_handoff_e2e_v12.py`），并在真实 GUI 实例重启后复验。
2. CLI 安装版无迁移步骤（干净库启动 failed）→ `packages/cli/src/index.mjs` 补 `alembic upgrade head`。
3. CLI 静态服务跨源/深链 404 → 同源反代 + 回退。
4. `http.ts` 烘焙 API base 在 CLI 场景全废 → 无壳注入时用相对路径。
5. composer「未绑定目录」误报 → 改判 `activeWorkspace`。
6. 评测 runner 无任务间隔 → run.mjs 加 `--pause`（评测层，不碰生产重试参数）。

## 安全检查（§7）

- 密钥扫描：任务书命令仅命中测试假密钥/占位符（sk-abcdefghijklmnop… 等），无真实泄漏
- `.env` / `*.key` / `*.pem`：未被 git 跟踪（仅 .env.example）
- npm 包与桌面产物：无本机绝对路径（E:\projects / C:\Users\22103 均未命中）
- 日志：`log_safety.py`（authorization/api-key/bearer/sk-/ghp_ 等打码）已接入事件、审批卡、沙箱、工具网关

## 未完成

- **GUI 壳（Electron 窗口）在本机无法启动**：包内后端、静态页、CLI、npm 安装链路全部真实验证，
  但"双击 exe 弹出窗口"这一步受本机环境限制无法执行（v153 以来历史一致）。上机验证需换一台机器。
- Benchmark `totalTokens` 记 0：glm 流式未回 usage，token 口径待补（不影响其余判分指标）。

## 发布结论

- 真实安装（npm 干净 prefix）✅、真实 CLI 启动链路 ✅、包内后端隔离启动 ✅、真实 GUI 五场景闭环 ✅
  （模型切换改变真实请求、记忆开关真实控制写入、逆向实验室真实请求）、Benchmark 全量基线已出 ✅、
  自动化测试全绿 ✅、安全检查通过 ✅。
- 唯一未达成项：桌面 GUI 壳窗口在本机环境无法启动（非代码缺陷，历史环境限制）。
- 结论：**除"GUI 壳本机上机启动验证"一项待换机确认外，v158 具备发布条件。**
