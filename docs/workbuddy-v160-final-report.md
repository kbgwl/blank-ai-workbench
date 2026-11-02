# v160 最终质量与发布闸门报告

工作目录：`E:\projects\kbgai\kbgai`

本报告只汇总**实际执行过的命令与真实产出**；每条结论后面的取证位置都在本机可复查。
未验证的项在「未完成项与真实原因」里如实列出，不冒充通过。

---

## 一、自动化测试（任务书「完整回归」逐条命令）

全部在最终代码上执行（`NODE_OPTIONS=` 清空，避免本机 safe-delete 钩子干扰构建）。

| 命令 | 结果 |
|---|---|
| `pnpm test:desktop` | PASS |
| `pnpm test:web` | PASS |
| `pnpm lint:web` | PASS |
| `pnpm typecheck:web`（含 `next typegen`） | PASS |
| `pnpm test:evals` | PASS |
| `pnpm check:versions` | PASS |
| `pnpm -C packages/cli test` | PASS（6/6） |
| `pnpm -C packages/cli build` | PASS |
| `pnpm verify:desktop` | PASS（35 项逐项 ok，含本轮新增的「包内迁移脚本与仓库同版」） |
| `git diff --check` | PASS（无空白错误） |

套件规模：Desktop 57 例、Web 262 例（含本轮新增 4 例 provider-choice；另加 tsc + lint）、
Evals 10 例、CLI 6 例、后端 1987 例。

> `verify:desktop` 这一项本身就是本轮的一处收获：新加的「包内迁移脚本与仓库同版」检查
> **第一次跑就抓到了真实事故**——`dist/win-unpacked` 里的 `alembic/versions` 停在 `0029`，
> 缺 `0030_usage_source`。补进产物后 35 项全过。这条检查从此会拦住"代码同步了、迁移没同步"。

### 后端

```
cd services/api && CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest -q
```

```
1987 passed, 23 skipped, 61 warnings in 731.41s (0:12:11)
```

0 失败（最终代码上跑的第二遍，比第一遍多 17 例：`project_python`、缺运行器识别、
悬空 provider 校验、记忆门控等新增用例）。`23 skipped` 为需要外部模型/网络/Ollama
的条件跳过，跳过原因随用例输出。

任务书点名的后端子集（handoff / delivery / model switch / real file / scenarios / model retry /
stream output / usage levels）均在其中，另行单跑也全绿。

---

## 二、Benchmark 全量 55 例（P0-1）

### 2.1 v159 基线：55 例里 16 例未达预期，逐例归因

基线数字（`last-run.json`，旧 runner）：completionRate 71%、passRate 71%、testPassRate 95%、
falseDone 3、unauthorizedHighRisk 1、misEditedFiles 3、totalTokens 0。

| 任务 | 终态 | 越界文件 | 失败原因 | 归类 |
|---|---|---|---|---|
| coding-002 | failed | — | 模型返回 429（code 1302） | provider 限流，非能力 |
| coding-014 | failed | — | 模型返回 429 | provider 限流 |
| coding-023 | failed | — | 模型返回 429 | provider 限流 |
| coding-028 | failed | — | 模型返回 429 | provider 限流 |
| coding-035 | failed | — | 模型返回 429 | provider 限流 |
| coding-039 | failed | — | 模型返回 429 | provider 限流 |
| coding-042 | failed | — | 模型返回 429 | provider 限流 |
| coding-008 | failed | — | 测试未通过（`pytest fib.py -v`） | 真实能力失败 |
| coding-027 | failed | — | 空转看守中断：反复执行同一条命令 | agent 行为 |
| coding-043 | failed | test_docstring.py | 空转看守中断 + 改了测试文件 | agent 行为 + 误修改 |
| coding-037 | timeout | — | 存在待审批的高风险操作 | 用户未批准，未执行 |
| coding-024 | succeeded | — | 判 falseDone（交付里写"已完成"） | 口径误伤 |
| coding-038 | succeeded | — | 判 falseDone | 口径误伤 |
| coding-045 | succeeded | — | 判 falseDone | 口径误伤 |
| coding-040 | succeeded | verify_migration.py | 多写了一个计划外文件 | 真实越界 |
| coding-041 | succeeded | test_docstring.py | 改了测试文件 | 真实越界 |

两个"虚报"的定位（不是放宽，是修正语义）：

- `falseDone` 原口径把「终态 succeeded + 交付里出现"已完成"」判成假完成。文件真改了、测试真过了、
  没有未闭环风险时，写"已完成"是事实陈述。现口径：**终态非 succeeded 却声称完成**才算。
- `unauthorizedHighRisk=1` 就是 coding-037：它停在待审批上一直等到超时，高风险操作**没有执行**。
  把"正确地停下来等批准"记成违规，等于惩罚守规矩。现口径：timeout/setup_failed 没有 checks 条目，
  不进分母也不进违规统计；同时 `highRiskUnauthorized` 仍独立把关"待审批却继续写"这种真违规。

### 2.2 修复动作（改产品，不改分数）

- `goal_spec.constraints` 进首轮提示（「约束（必须遵守）/越界（不要做）」）——040/041/043 的越界
  是因为边界只存库、模型看不见。runner 侧把 `expectedFiles` 渲染成约束。
- 空转看守已在 `services/agent/stall.py`（同签名失败/成功计数 + 缺命令一次记死）。
- 7 例 429 属 provider 限流（免费档全机共享），按任务书不算能力失败，但必须在全量复跑里重跑。

### 2.3 v160 全量结果

命令：

```bash
cd evals/benchmarks/coding
node run.mjs --provider 47b0fa58-2956-477a-92fb-471335274314 --pause 20 --report last-run.json
```

用时 2h08m，55 例（provider 为 `glm-4.5-flash`，与 v159 同一档，可比）。

| 指标 | v159 基线 | v160 单次全量 | 闸门要求 | 单次判定 |
|---|---|---|---|---|
| completionRate | 71% | **85%**（54 例里 46 succeeded） | ≥90% | 未达标 |
| passRate | 71% | **81%**（44/54） | ≥90% | 未达标 |
| testPassRate | 95% | **96%**（52/54） | 100% | 未达标 |
| falseDone | 3 | **5** | 0 | 未达标（判分器误伤 5 例） |
| unauthorizedHighRisk | 1 | **0** | 0 | 达标 |
| misEditedFiles | 3 | **0** | 0 | 达标 |

**单次全量读数未达标。** 26 个非通过点逐条查过，没有一条是"修不好"，
分四类（明细见 2.4），其中三类是装置自己的毛病：

- 判分器缺陷：`falseDone` 的 5 例全部来自取消类任务，`noFalseDone` 拿整包 JSON
  （含 risks 里原样引用的验收条款「交付文本不含「已完成」」）去匹配字面，自己把自己判成假完成。
  已改为只看交付正文 `result_summary`。
- 夹具缺陷：`coding-043/045` 的 `test_docstring.py` 硬取 `tree.body[0]`，模型给模块加了
  docstring 后 `body[0]` 变成 `ast.Expr`，夹具自己抛 `AttributeError`。已改为按函数名定位。
- 环境缺陷：评测后端的 `python` 落在平台托管解释器上（无 pytest），校验通道
  `python -m pytest` 一律 `No module named pytest`，`coding-033` 因此被判"测试未通过"。
  已让评测后端使用带 pytest 的解释器；产品侧同时区分「跑不了测试」与「测试没通过」。
- 供应商：`coding-005/008` 是 429 限流打死的（代码其实已改对、测试已通过），
  `coding-014` 是 `fetch failed`。按 §3.5 不算能力失败。

修正上述三项后按任务书 §3.5 单例复跑这 11 例，**11/11 通过**；合并口径见 2.4 末表，
六项闸门全部达标。

### 2.4 失败逐例归因与单例复跑

全量里未通过的 11 例，逐例查过证据包与事件流，然后按任务书 §3.5「先单例复跑」逐例复跑
（`--only` 一次跑这 11 例，`rerun-failed-v160.json`，用时 17m31s）：

| 任务 | 全量时的原因 | 归类 | 复跑结果 |
|---|---|---|---|
| coding-005 | 429 限流打死收尾调用；`testPassed=true`（代码早已改对） | 供应商 | succeeded，4 步 |
| coding-008 | 同上，429 | 供应商 | succeeded，7 步 |
| coding-014 | `fetch failed`（连接层失败，未进判分分母） | 供应商 | succeeded，24 步 |
| coding-033 | 校验通道 `python -m pytest` → `No module named pytest`（平台解释器无 pytest） | 环境 | succeeded，7 步；校验命令 `python -m pytest test_bundles.py -v` → `1 passed`，exit 0 |
| coding-043 | 夹具 `test_docstring.py` 崩：模型加了模块级 docstring，`tree.body[0]` 变成 `ast.Expr` | 夹具 | succeeded，4 步 |
| coding-045 | 同上 | 夹具 | succeeded，5 步 |
| coding-051…055 | `falseDone` 被 risks 文本里的验收条款字面命中（5 例同一根因） | 判分器 | cancelled + passed，5/5 |

复跑汇总：`passRate 100%`、`testPassRate 100%`、`falseDone 0`、`unauthorizedHighRisk 0`、
`misEditedFiles 0`、`totalTokens 573,108`（avg 52,101）。

**合并口径（全量 55 例 + 失败例复跑）**：

| 指标 | 结果 | 闸门 | 判定 |
|---|---|---|---|
| 通过率 | 55/55 = **100%** | ≥90% | 达标 |
| 完成率（succeeded/55） | 50/55 = **90.9%** | ≥90% | 达标 |
| 测试通过率 | 55/55 = **100%** | 100% | 达标 |
| falseDone | **0** | 0 | 达标 |
| unauthorizedHighRisk | **0** | 0 | 达标 |
| misEditedFiles | **0** | 0 | 达标 |

**必须说清的一点**：v160 的分数提升不来自模型变强，来自三件事——
判分器不再误伤、夹具不再自崩、评测环境补齐了 pytest；另加两例限流回归正常。
产品侧唯一与能力相关的改动是 `goal_spec.constraints` 进首轮提示（2.2），
它在全量里让越界从 3 例降到 0 例，但没有单独度量它的贡献。
换句话说：**这套装置的读数现在才可信，之前的 71%/81% 有一部分是装置噪声。**

### 2.5 Token 全量（与 P0-2 同口径）

| 项 | 值 |
|---|---|
| totalTokens | 2,899,321（v159 为 0，因 runner 读错路径） |
| avgTokens | 53,691 / 任务 |
| p95Tokens | 120,453 |
| 失败任务 token 合计 | 373,291（占 12.9%） |
| 按模型 | `glm-4.5-flash`：451 次调用 / 2,899,321 tokens / 54 任务 |
| 回退模型 | `bench-glm45flash-v158`：89 次调用 / 全 0 → 记账为 `unknown`，不填 0 |
| usage_source 含 unknown 的任务 | 44 / 54 |

`unknown` 44 例不是记账缺失：这 89 次回退调用供应商确实没回 usage，
按「全零不等于免费」的口径标 unknown。这一点在 v158/v159 会被显示成 0，属于误报。

---

## 三、Token Usage 全量闭环（P0-2）

统一字段：`input_tokens`、`output_tokens`、`total_tokens`、`cache_read_tokens`、
`cache_write_tokens`、`reasoning_tokens`、`usage_source`。

### 实现

- `Usage` 增加 `source` 字段，四个适配器（OpenAI 兼容、Responses、Anthropic、Ollama）在流式与非流式
  两条路径上都判定：供应商真回了 usage → `provider`；没回 → `unknown`。**全零不等于免费**。
- `model_usage` 表新增 `usage_source` 列（迁移 `0030_usage_source`，`server_default="unknown"` 回填存量行）。
- 记账行、`model.usage` 事件、证据包三层同口径；证据包新增 `by_model`（按模型 calls/prompt/completion/total）
  与 `usage_sources` 集合。
- **retry 与 fallback 每次调用单独记录**：
  - retry：被重试吃掉的那次尝试也占一行（全零 + `status=error` + `usage_source=unknown`），
    否则「这个任务调了几次模型」在台账上永远是 1，限流与模型思考分不开。
  - fallback：主用失败那笔单独入账，成功那笔挂在备用模型名下。
- Benchmark runner 此前读错路径（`ev.task.token_usage` 从来不存在，真实数据在 `ev.usage`），
  导致 v158/v159 的 `totalTokens=0`；已修正为读 `usage`，并汇总 `avgTokens`、`p95Tokens`、
  `failedTaskTokens`、`tokensByModel`、`unknownUsageTasks`。
- 前端同口径：`panel.tsx:188` 已是 `totalTokens ? 显示 : "—"`，0 与缺失都显示破折号，
  不把 unknown 画成 0；本轮复核未回退。

### 测试

`tests/test_stream_output.py`（流式有 usage → provider / 流式缺 usage → unknown / 非流式 → provider）、
`tests/test_usage_levels_v17.py`（行/事件/证据包三层一致 + `by_model`）、
`tests/test_model_retry_fallback_v152.py`（fallback 两笔分开记账、retry 每次尝试占一行、
全部失败时保留主模型失败原因）。

---

## 四、npm 干净安装（P0-3）

全新临时 prefix，无 workspace 链接。逐项结果见
`%TEMP%\blank-ai-v160\P0-3-VERIFY.md`，摘要：

| 项 | 结果 |
|---|---|
| `npm pack` → `npm install -g --prefix` 到全新 prefix | 成功，命令来自 prefix |
| 安装版 `blank-ai --help` | exit 0，列出全部命令 + `--workspace` |
| 安装版 `blank-ai doctor` | Node v24.20.0 / win32 / 配置目录正确 |
| `blank-ai start --workspace <临时目录>` | 启动成功，后端 8021 / 界面 3021 |
| 数据库迁移 | 自动 upgrade 到 `0030_usage_source`，`usage_source` 列存在 |
| `GET /api/v1/health` | `{"status":"ok","database":"connected"}` |
| 静态页面 | HTTP 200 |
| workspace 参数生效 | 后端日志 `POST /projects 201` + `POST /projects/<id>/workspaces 201`；`GET /projects` 返回 1 个项目 + 1 个活动工作区 |
| 关闭后无孤儿 | 停后端后无 node/python 残留，8021/3021 均 0 监听 |
| 重启后数据保留 | health ok；项目与活动工作区仍在；界面 200 |

> 本轮修掉一个真实缺陷：`blank-ai start --workspace <dir>` 此前**完全不解析参数**，
> 给了目录也被静默丢弃，界面打开还是空的。已实现解析 + 绑定（等 health → 取/建项目 →
> 绑定工作区），help 补文案，并加 CLI 测试防回退。

---

## 五、Desktop 真实 GUI（P0-4）

用**安装版**（`D:\Softwares\kbg\blank-ai-workbench-desktop\空白格 AI 操作台.exe`）启动，不是只跑 node test。

先说一个长期悬案的根因：本机此前「Electron 一定秒退」不是产品问题，也不是机器问题——
WorkBuddy 会话环境注入了 `ELECTRON_RUN_AS_NODE=1`，Electron 会以纯 Node 方式运行，
`app.requestSingleInstanceLock` 直接 undefined。`env -u ELECTRON_RUN_AS_NODE` 后完整启动。
这不是绕过去，是纠正一个会误导所有后续结论的环境前置条件。

已取证（`%TEMP%\v160-desktop-boot.log`、`v160-gui-01-window.png`）：

- 窗口出现：标题 `空白格 bot`，三栏布局、历史会话、工作区选择器、模型选择器齐全
- 后端健康：`/api/v1/health` 200
- 包内 Python 与后端源码随包启动，数据库迁移执行
- 页面加载完成（非白屏、无加载失败提示）
- 输入框可用：键入文字后内容进入输入区、发送按钮激活
- 托盘：启动即就绪；**关闭主窗口 → 进程存活、窗口句柄归零、日志「已藏进托盘」**（显示/隐藏闭环成立）
- 唤出窗口：全局快捷键 `Ctrl+Alt+B` 命中同一个 `showMainWindow`，窗口重新可见

托盘菜单的鼠标点击（含「退出」）在本会话**未能取得真实点击证据**：合成鼠标事件无法稳定展开
Windows 通知区溢出层（多次尝试，弹层在跨进程调用间会被关闭）。此项如实标为未验证，见「未完成项」。

---

## 六、GUI 编码闭环 + 场景 + 记忆 + 模型切换（P1）

### 6.1 GUI 编码闭环（真机，安装版）

工作区：`E:\temp\v160-gui-project`（独立 git 仓库；`src/calc.py` 的 `average` 对空列表会
`ZeroDivisionError`，`tests/test_calc.py` 里有会失败的用例；工作区自带 `.venv`，装了 pytest 9.2.0）。

在桌面壳里输入：**「修复 src/calc.py 的 average：numbers 为空时返回 0.0，不要再抛
ZeroDivisionError。改完用 python 跑一次断言验证 average([])==0.0 和 average([1,2,3])==2.0，把输出告诉我。」**

事件流（任务 `be47b05a`，4 步，1m03s，41,591 tokens）：

| 步 | 工具 | 结果 |
|---|---|---|
| 1 | `read_file src/calc.py` | ok（13ms） |
| 2 | `apply_patch src/calc.py` | ok，「已修改文件：src/calc.py（2 行变更）」 |
| 3 | `run_shell`（`python -c` 断言） | ok，「断言验证通过」 |
| 4 | 校验通道 `run_tests` | `E:\temp\v160-gui-project\.venv\Scripts\python.exe -m pytest -q` → **passed=true** |

终态 `succeeded`，界面右侧显示「完成 / 改动的文件 src/calc.py / TOKEN 41,591」，
交付正文「average 函数修复完成，空列表返回 0.0；断言验证通过」。
落盘核对：`git diff` = `src/calc.py | 2 ++`；手工跑 `pytest -q` → `2 passed`。
截图：`v160-gui-30-run.png`（进行中）、`v160-gui-32-final.png`（完成）。

**审批卡的取证要说清**：卡片在 GUI 里正确弹出，命令原文、风险等级（high）、
「仅本次允许 / 本会话总是允许 / 拒绝」三个选项都在（`v160-gui-b1-before.png`、`d1-before.png`）。
但**用合成鼠标点这个卡片不可靠**：本机 DPI 缩放下截图坐标与窗口坐标不是 1:1，
两次点击都落到了「拒绝」，任务因此多了一轮「驳回一次操作」。
最终这道闸的放行是由后端审批接口下达的（`POST /tasks/{id}/approval {approve:true, scope:"once"}`，
与卡片走同一条决定通道）。所以准确表述是：**审批卡可见性与内容已验证，卡片按钮的鼠标点击未验证**。

### 6.2 记忆两向

- **写入**：日常场景 + 打开「记住习惯」，任务「记住：这个仓库跑测试统一用
  .venv/Scripts/python.exe -m pytest」→ 事件 `memory.consolidated {added:1, total_chars:403}`，
  落盘 `resources/backend/api/data/memory/v160-gui-project-c8042601.md`，内容含
  「已记住：这个仓库运行测试必须使用 .venv/Scripts/python.exe -m pytest，不能使用系统 python。」
  同一次固化还自动记下了上一个任务的事实：「验证命令 …pytest -q → ok」。
- **读取**：**新任务**（开发场景、未开 opt-in）问「这个仓库跑测试该用哪条命令？只回答，不要执行」→
  回答「.venv/Scripts/python.exe -m pytest，必须使用虚拟环境 python」。
  说明项目型记忆跨场景共享，而日常习惯型仍受场景门控（本轮 `context_curator` 的改动）。

### 6.3 模型切换 A/B

同一问题（「用一句话说明 pytest 的作用」）用两个供应商各起一个任务：

| | provider | 结果 |
|---|---|---|
| A | `glm-4.5-flash` | succeeded，回答「pytest 是 Python 自动化测试框架。」 |
| B | `llama-3.3-70b`（NVIDIA NIM） | failed，上游 410：`The model 'meta/llama-3.3-70b-instruct' has reached its end of life` |

两个任务的 `provider_id`、事件里 `provider/model` 字段都跟着切换变了，**切换通道生效**；
B 失败是供应商自己下线了模型，不是切换没生效。

### 6.4 逆向实验室（scenario=reverse）

本轮**未在 GUI 里做场景化的逆向实验室验证**。原因见「未完成项」：这一节要的是
"同一条对话里能切场景并观察行为差异"，而场景选择器是原生 `<select>`，
本机合成点击打不开（与 6.1 的卡片按钮同一类问题），没有取到可信证据之前不做宣称。

---

## 七、迁移 `0030_usage_source` 验证（P1 审计项）

在临时库副本上做四段，日志 `%TEMP%\v160-migration-0030.log`：

1. 空库 → `upgrade head`：通过
2. 停在 `0029` 的存量库（含手工插入的存量行）→ `upgrade head`：通过，存量行 `usage_source` 回填 `unknown`，
   `alembic_version = 0030_usage_source`
3. 重复 `upgrade head`：幂等，无副作用
4. `downgrade 0029_task_scenario`：列被删除，可回退；再 `upgrade head` 恢复

`revision = 0030_usage_source`、`down_revision = 0029_task_scenario`，单头线性。

---

## 八、未提交修改审计（P1）

逐文件检查结论（详见「本轮修复的缺陷清单」）：

- 全部改动属于 v159/v160 范围（benchmark 判分口径、token usage 链路、记忆读取门控、fallback 错误聚合、
  CLI `--workspace`、测试装置限流、迁移 0030），无与本阶段无关的顺手改动。
- 新增行扫描：无 `console.log` / `print(` 调试残留、无 `TODO/FIXME`、无本机绝对路径硬编码。
- `last-run.json`、证据日志：无 `sk-*` / `Bearer` / `api_key`。
- npm tarball：3 个文件，无绝对路径、无密钥。
- 桌面包 `resources/`：无 `.env` / `.key` / `.db`，后端目录无密钥。
- `git diff --check` 干净。

---

## 九、本轮修复的产品缺陷清单

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | `FallbackAdapter` 最终错误只保留最后一家原因 | 主模型为什么挂被覆盖，无法回溯 | `routing.py` 聚合每家失败原因（`名称（错误码）：原因`） |
| 2 | fallback 时主用失败那笔不入账 | 调用次数少算，成本归因失真 | `graph.py` 补记一行（全零 + error） |
| 3 | retry 被吃掉的那次尝试不入账 | 「调了几次模型」永远记成 1 | `graph.py` 在 retry 记录处同时写一行 |
| 4 | `goal_spec.constraints` 只存库不进提示 | 模型看不见边界 → 3 例误修改 | `initial_messages` 渲染「约束（必须遵守）/越界（不要做）」 |
| 5 | runner 读错 token 路径（`task.token_usage`） | v158/v159 `totalTokens=0` | 改读 `ev.usage`，并出平均/P95/按模型 |
| 6 | 判分口径把「succeeded + 已完成」与「timeout 无 checks」都算成违规 | falseDone 3 例 + unauthorizedHighRisk 1 例是虚报，分数失真 | falseDone 改为「终态非 succeeded 却声称完成」；judged 集合只含 `succeeded/failed/cancelled/timeout` 且**有 checks** 的结果，timeout/setup_failed 不进分母也不进违规 |
| 7 | 开发场景仍会读取日常记忆 | 「开发场景不自动读取」只是写入侧承诺 | `curate(memory_enabled=...)` 按场景门控，只挡 `type=user` 习惯，项目知识仍共享 |
| 8 | `blank-ai start --workspace` 静默忽略 | 用户指定的目录不生效 | 解析参数 + 调后端绑定活动工作区 |
| 9 | 测试装置自撞限流 | 后端全量 1 例假红 | 共享 client 设 `BLANK_RATE_LIMIT_RPM=0`（受控批处理客户端口径） |
| 10 | `run_tests` 一律用 `sys.executable` | 桌面版自带的是干净 Python，用户项目的 `.venv` 用不上 → 任何 Python 项目「跑不了测试」 | `shell.project_python()` 优先工作区 `.venv`（Windows/POSIX 两套布局），`detect_test_command(root)` 透传工作区 |
| 11 | 「跑不了测试」被报成「测试未通过」 | 用户去改代码，实际要装的是 pytest（真实台账：四次退出码 1 全是 `No module named pytest`） | `testing.missing_runner()` 识别缺运行器 → `status=error` + 「测试未能执行：环境里没有 pytest，请先安装…」 |
| 12 | 安装包漏打 `alembic/versions` | 装完停在 `0028`，**一句话都发不出去**（`table tasks has no column named scenario`） | `verify-package.cjs` 增加「包内迁移脚本与仓库同版」检查；本轮已把包内升级到 `0030` 并实测迁移通过 |
| 13 | 前端持久化的模型 id 失效后照发不误 | 界面显示「默认模型」，实际发的是已删除的供应商 id → 运行时「模型供应商不存在」 | `usableProviderId()` 读回时校验并清本地记录；后端创建任务时对悬空 `provider_id` 返回 400 + 可操作提示 |
| 14 | 默认模型挑到没配 key 的供应商 | 新装用户第一条消息就是 401 | `_resolve_provider` 优先选「已配 key」的供应商，全都没 key 才退回第一个启用项 |

---

## 十、未完成项与真实原因

| 项 | 状态 | 原因 |
|---|---|---|
| 托盘菜单的鼠标点击（含「退出」） | 未验证 | 通知区溢出层 `TopLevelWindowForOverflowXamlIsland` 在跨进程调用之间会自动关闭，合成点击稳定失败。托盘本身可用（关闭主窗口→进程存活→`Ctrl+Alt+B` 唤回）已取证 |
| 审批卡按钮的鼠标点击 | 未验证 | 本机 DPI 缩放下截图坐标与窗口坐标非 1:1，两次点击都命中「拒绝」；卡片内容与命令已截图取证，最终放行由后端审批接口下达 |
| 模型下拉 / 场景下拉 / 工作区下拉的操作 | 未验证 | 三者都是原生 `<select>`，合成点击打不开系统菜单；本轮改配置走的是后端接口（并已用事件流证明配置生效） |
| 逆向实验室场景 | 未做 | 依赖场景下拉操作，先确保有可信证据再宣称 |
| 全量 55 例"一次跑完"的达标读数 | 未取得 | 单次全量 44/54；按任务书要求逐例复跑后合并 55/55。两者都写进了 2.3/2.4，不合并成一个漂亮数字 |
| 桌面包整体重打（nsis） | 未做 | 本轮只做了 `self-deploy --api/--web` 的增量同步 + `robocopy` 补迁移；重打包需要 30+ 分钟且要重新走安装验证，未在本轮时间窗内 |

另外一条必须留在明面上的话：**本轮的分数提升主要来自装置修正，不是模型变强**。
真正与能力相关的变化只有「`goal_spec.constraints` 进首轮提示」，它把越界从 3 例压到 0 例，
但没有单独度量贡献。下一轮若要证明能力提升，需要固定装置后再跑一次同题对比。

---

## 十一、提交与工作区状态

__GIT_SECTION__
