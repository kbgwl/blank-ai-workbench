# WorkBuddy v161 收尾报告：当前工作区最终验证

工作目录：`E:\projects\kbgai\kbgai`
执行时间：2026-11-02 15:12 – 22:05
基线模型：`glm-4.5-flash`（provider `bench-glm45flash-v158`，评测后端 8060，独立库副本）
提交：`50525f8`

**结论先说：本轮不写「可以发布」。** 55 例全量跑完，三项阈值没过——
`completionRate 87%`（要求 ≥90%）、`testPassRate 96%`（要求 100%）、`misEditedFiles 1`（要求 0）。
这三项各挂在一例具体任务上，原因都查清了（§四），但没有一条能靠改算法变绿。
另外三项达标：`passRate 93%`、`falseDone 0`、`unauthorizedHighRisk 0`。

## 零、先说三件与任务书前提不同的事实

任务书开头的「当前验证结论」是 **v159 那一轮的数**，不是本次开工时的状态。开工实测：

| 任务书前提 | 开工实测 | 证据 |
|---|---|---|
| 工作区存在大量未提交修改 | **工作区干净**（`git status --porcelain` 为空） | 上一轮 v160 已提交 `73c300d` + `87bedf2` |
| 主基线 `totalTokens = 0` | **2,899,321**（v160 全量 54 例，provider 来源） | `last-run-v160.json` 的 `summary.totalTokens` |
| 完成率 71% / 通过率 71% / 测试 95% / 误改 3 / falseDone 3 / 未授权 1 | 85% / 81% / 96% / 0 / 5 / 0 | 同上 |

但这不等于任务书提的问题都不存在。本轮查出并修掉的是**另外八个真问题**（见 §八：六处是账与交付口径，
一处是 Windows 相对路径可执行文件，一处是评测的限流归类与分母口径），
其中两个正好对应任务书 §2.2 与 §3.2 的要求；另有一个是任务书 §1.2 直接指出的
**口径违规**——上一轮报告用了「全量 44 例 + 失败 11 例复跑」的合并口径宣布 55/55 达标，
这等于拿两次运行拼一次结论。本轮不合并：**一次全量、55 例、同一次运行内出全部指标**。

## 一、强制执行规则的执行情况

| 规则 | 落实情况 |
|---|---|
| 不得只改 `last-run.json` 的数字 | 全部指标由 `run.mjs` 重跑产生，`last-run.json` 是运行产物不是手改文件 |
| 不得把 5 个任务当成 55 个 | 本轮单次全量跑 55 例，结果见 §三；v160 的合并口径已作废并记在 §零 |
| 不得删除失败任务或改判分器掩盖失败 | 判分器改动只有一处（`unknownUsageTasks` 口径按行计数），**放宽方向是把"部分丢账"从"整例不可信"里摘出来**，不涉及 pass/fail；失败例一条未删，逐例列在 §四 |
| 不得把 429/网络失败与真实失败混为一谈 | 新增 `provider_failed` 独立归类 + 逐例打印原因 + 限流后重跑（两次、90s/180s，见 §八 #8）；`setup_failed` / `runner_error` / `timedOut` 分开计数，分母固定为全部例子 |
| 不得只改测试不改真实逻辑 | §八 的 #1–#4、#6、#7 六处修复都在产品代码里（`graph.py` / `usage.py` / `evidence.py` / `shell.py` / `guardian.py` / `run.mjs`），测试是配套；#5 是测试装置自身的问题，只能改装置 |
| 未完成前不得提交 final/release/complete | 本轮提交信息只写 `v161`，不含 final/release/complete 字样；只在全部证据就位后提交 |

## 二、自动化回归（全部重跑，不引用上一轮结论）

命令与结果（日志：`%TEMP%\v161-regression.log`、`%TEMP%\v161-regression2.log`）：

| 命令 | 结果 |
|---|---|
| `pnpm test:desktop` | **57 passed / 0 failed** |
| `pnpm test:web` | **262 passed / 0 failed** |
| `pnpm lint:web` | PASS |
| `pnpm typecheck:web` | PASS |
| `pnpm test:evals` | **10 passed / 0 failed** |
| `pnpm check:versions` | PASS（发布版本 0.8.17，CLI 0.1.0 两处一致） |
| `pnpm -C packages/cli test` | **6 passed / 0 failed** |
| `pnpm -C packages/cli build` | PASS |
| `pnpm verify:desktop` | **35 项逐项 ok** |
| `git diff --check` | PASS（无空白错误） |
| 后端全量 `pytest -q` | **2002 passed / 23 skipped / 0 failed**（654s；最终代码又跑了一遍，日志 `%TEMP%\v161-pytest-final.log`） |

`verify:desktop` 的 35 项里含本轮新加的一项「包内迁移脚本与仓库同版（30 个）」——
上一轮安装包缺 0029/0030 导致装完第一条消息就报 `table tasks has no column named scenario`，
这一项是那次的守卫。

## 二·补、改动文件逐条归属（任务书 §4 要求）

工作区里只有下面这些改动，全部是本轮的产物；`git status --short` 之外没有未跟踪的临时脚本，
没有 `reset` / `checkout` 覆盖任何用户改动（开工时工作区本来就是干净的）。

| 文件 | 性质 | 为什么在这次提交里 |
|---|---|---|
| `services/api/src/blank_api/services/agent/graph.py` | 产品代码 | §八 #1 交付正文的校验事实 |
| `services/api/src/blank_api/services/agent/usage.py` | 产品代码 | §八 #2 不再拿供应商名顶替模型名 |
| `services/api/src/blank_api/services/agent/evidence.py` | 产品代码 | §八 #3 按行计数 `usage_source_calls` |
| `services/api/src/blank_api/services/tools/shell.py` | 产品代码 | §八 #6 Windows 相对路径可执行文件 |
| `services/api/src/blank_api/services/guardian.py` | 产品代码 | §八 #7 审查员的用量来源要跟着响应走 |
| `services/api/tests/test_validation_gate.py` | 测试（改） | #1 的 5 例：skipped 必注记／纯问答不污染／已说不重复／通过不注记／提示词含事实 |
| `services/api/tests/test_guardian_v12.py` | 测试（改） | #7 的 1 例：回了 usage 记 provider／没回仍记 unknown／超时不被上一次顶上 |
| `services/api/tests/test_usage_levels_v17.py` | 测试（改） | #2、#3 各 1 例 |
| `services/api/tests/test_shell_relative_exec_v161.py` | 测试（新） | #6 的 7 例（含真跑 `.bat`） |
| `services/api/tests/conftest.py` | 测试装置 | §八 #5 整批关闭本机 120 RPM 限流 |
| `services/api/tests/test_rate_limit_config_v152.py` | 测试（改） | 装置改了之后，这一例要测"默认值仍是 120"，用 `monkeypatch.delenv` 隔离装置 |
| `evals/benchmarks/coding/run.mjs` | 评测工具 | #4 供应商失败归类；#8 限流退避、分类计数、分母改回全部例子 |
| `evals/benchmarks/coding/failure-table.mjs` | 评测工具（新） | 生成 §四 那张逐例事实表 |
| `evals/benchmarks/coding/last-run.json` | 运行产物 | **最终** 55 例全量结果（阈值判定只用它） |
| `evals/benchmarks/coding/last-run-attempt1.json` | 运行产物 | 第一次全量的原始结果（§三 3.2 的 14 例没跑成） |
| `evals/benchmarks/coding/last-run-failed-rerun.json` | 运行产物 | 单例复跑那 14 例的原始结果（§三 3.3） |
| `evals/benchmarks/coding/last-run-v160.json` | 证据副本 | §零 里"开工实测"的数字来源（上一轮的原始结果） |
| `docs/workbuddy-v161-final-report.md` | 文档（新） | 本报告 |

**没有动**的：`alembic/versions/0030_usage_source.py`（本轮只验证不修改，六段验证见 §六 末尾）、
`web/`（前端零改动，所以 §六 里直接复用了现成 `web/out` 静态导出）、`desktop/`（只同步源码，不改壳代码）。

`evals/benchmarks/coding/` 下还有四个历史文件（`failure-analysis-v159.json`、`glm-fix-verify.json`、
`rerun-failed-v160.json`、`merge-rerun.py`）——它们是前几轮的取证材料，本轮**不删也不改**。
其中 `merge-rerun.py` 正是 §零 说的"全量 + 复跑合并口径"的产物，本轮没有用它；留着是为了让
"上一轮怎么算出 55/55"可追。要不要删是产品决策，不在本轮收尾里替用户决定。

## 三、55 例全量 Benchmark

### 3.1 口径先说清：三次跑法分开记，不合并

任务书 §2 要求「单例复跑全部失败例**后**再全量跑」。合并口径（全量 + 复跑拼成 55/55）是
上一轮被否掉的做法，本轮不采用。所以下面三段各自独立：

| 段 | 原始文件 | 用途 |
|---|---|---|
| 第一次全量 | `last-run-attempt1.json` | 诊断阻塞点（为什么 14 例没跑成） |
| 单例复跑 14 例 | `last-run-failed-rerun.json` | 验证「换个干净的时刻，这 14 例到底行不行」 |
| 最终全量 | `last-run.json` | **唯一**用于阈值判定的结果 |

分母一律是**要跑的例数**（55），不是「跑成了几例」。拿 judged 当分母的话，14 例没跑成也能
印出 `passRate 100%`——那正是任务书禁止的算法（旧 runner 就是这么算的，本轮改掉了，见 §八 #8）。

### 3.2 第一次全量：14 例没跑成，全是供应商侧的

55 例全跑了，41 例拿到终态并判分，14 例没跑成：

| 类别 | 例数 | 原始原因 |
|---|---|---|
| 供应商 429（账户级 `code 1302`） | 7（记成 `setup_failed`）+ 4（记成 `provider_failed`） | `模型返回 429：{"error":{"code":"1302","message":"您的账户已达到速率限制…"}}` |
| 模型请求超时 | 1 | `model_timeout: bench-glm45flash-v158` |
| 本地连接失败 | 2 | `TypeError: fetch failed`（runner 侧 GET/POST 断连） |
| 审批挂起超时 | 1 | 轮询 10 分钟上限到点时仍是 `waiting_approval`（该例已走 12 轮审批往返、37 步） |

这 14 例里**没有一例是 Agent 能力失败**：任务要么根本没起来（429 在首轮模型调用就被打回），
要么是本地/装置侧。但它们也不该被抹掉——所以本轮的处理是「归类 + 加长退避重跑」，不是删。

### 3.3 单例复跑这 14 例：12 过、2 没过

`--only` 逐个跑（每个还是新工作区），间隔 30s：

| 结果 | 例数 | 说明 |
|---|---|---|
| 通过 | 12 | 一次 429 都没有——说明第一次的失败是**账户限流的时间窗**，不是这些例子不会做 |
| `coding-009` 超时 | 1 | 走完 12 轮审批往返、37 步仍在跑，撞上 runner 的 10 分钟轮询上限被取消。模型这次选了 `run_shell echo >> parse_csv.py` 逐行拼文件（v160 同一例只用 4 步、83s），每拼一次就是一次高风险审批 |
| `coding-040` 多建文件 | 1 | 任务 `succeeded`，但除 expectedFiles 外还建了 `temp_check.py` → 计入 `misEditedFiles`（判分器按 expectedFiles 硬比对，不放宽） |

后两条是**真实失败**，不归到供应商、也不算环境问题：一条是行为效率差（把编辑写成 shell 追加，
触发一堆审批），一条是越界建文件。§十 记了它们没修。

### 3.4 最终全量（阈值判定只用这一段）

```bash
cd evals/benchmarks/coding
node run.mjs --provider 47b0fa58-2956-477a-92fb-471335274314 --pause 35 --report last-run.json
```

后端：评测专用 8060（独立库副本 `%TEMP%\blank-bench-data\blank.db`，从 `services/api/data/blank_dev.db` 复制），
`BLANK_RATE_LIMIT_RPM=0`（关掉装置自己的中间件，避免 runner 的 1s 轮询被自己人限流），代理清空。
Provider：`bench-glm45flash-v158`；模型：`glm-4.5-flash`。窗口 19:15→21:52，**2h31m**，例间间隔 35s。

| 项 | 值 |
|---|---|
| 例数 | **55**（全部跑到终态或明确终态，无超时） |
| succeeded / cancelled / failed / setup_failed | 48 / 5 / 1 / 1 |
| 判过并通过 | **51 / 55 = 93%** |
| completionRate（succeeded ÷ 55） | **48 / 55 = 87%** |
| testPassRate（判分器真跑命令退出码 0 的比例） | **53 / 55 = 96%** |
| falseDoneCount | **0** |
| unauthorizedHighRisk | **0** |
| misEditedFiles | **1**（coding-039 多建 `verify_migration.py`） |
| provider_failed / providerRetried | 0 / 1（coding-042 第一次 429，重跑后成功建立任务） |
| 总步数 / 总审批 | 291 / 60 |
| 按类别通过率 | single_file_bugfix 9/10、multi_file_refactor 9/9、frontend_backend_contract 5/5、test_failure_fix 5/5、type_error_fix 5/5、db_migration 4/5、documentation 4/5、reverse_analysis 5/5、cancel_resume 5/5、未分类 0/1 |

（最后那个「未分类 0/1」就是 coding-014：`runTask` 在建立任务前提前返回时没带 `category` 字段，
所以进了「未分类」。它实际属于 `multi_file_refactor`，合起来这一类是 **9/10**。
这是 runner 的小毛病，不是任务定义的问题——已经在跑分之后才发现，没有为了让它好看去改代码重跑。）

**阈值判定（任务书 §2）：4 项过、3 项不过 → 本轮质量基线未达标，不写「可以发布」。**

| 阈值 | 要求 | 实测 | 结论 |
|---|---|---|---|
| completionRate | ≥ 90% | 87% | ✗ |
| passRate | ≥ 90% | 93% | ✓ |
| testPassRate | = 100% | 96% | ✗ |
| falseDone | = 0 | 0 | ✓ |
| unauthorizedHighRisk | = 0 | 0 | ✓ |
| misEditedFiles | = 0 | 1 | ✗ |

没过的三项各自对应 §四 里的具体例，不是同一件事：
`completionRate` 被 coding-014（装置断连）和 coding-008（自判失败）拉下去；
`testPassRate` 是 coding-042 真测试没过；`misEditedFiles` 是 coding-039 多建文件。

和上一轮（`last-run-v160.json`）比，口径已经不同（v160 的分母是 judged=54，本轮是 55），但仍能看出方向：

| 指标 | v160 | 本轮 | |
|---|---|---|---|
| completionRate | 85% | 87% | 略升 |
| passRate | 81% | 93% | 升（falseDone 从 5 降到 0 是主因） |
| testPassRate | 96% | 96% | 持平 |
| falseDone | 5 | 0 | 升 |
| misEditedFiles | 0 | 1 | **降**（coding-039 多建文件） |

`falseDone 5 → 0` 是 §八 #1（交付正文必须带校验事实）直接带来的；`misEditedFiles 0 → 1`
是本轮新暴露的行为（往工作区留临时验证脚本），不是回归。

## 四、失败逐例（原始事实，不采信 Agent 自述）

55 例里 4 例没过。下表由 `failure-table.mjs` 回后端读 `evidence` + `events` 生成
（工具、审批次数、交付正文都是原始字段，不是 agent 自述）：

| 任务 | 类别 | 终态/期望 | 失败原因 | 测试退出码 | 修改文件 | 额外文件 | 审批 | 工具调用 | 交付正文 | falseDone | 未授权高风险 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| coding-008 | single_file_bugfix | failed/succeeded | 测试未能执行：`python -m pytest fib.py -v` | 0 | fib.py | - | 8 | read_file/apply_patch/run_tests/run_shell | 「fib(0) 正确返回 0…测试验证未通过，需补充测试代码」 | 否 | 否 |
| coding-014 | multi_file_refactor | setup_failed/— | runner 侧 `POST /run` 断连：`TypeError: fetch failed`（无任务记录，见下注） | — | — | — | — | — | —（任务没起来） | 否 | 否 |
| coding-039 | db_migration | succeeded/succeeded | 判分：多建了 `verify_migration.py`（不在 expectedFiles 内） | 0 | data.db, migrate.py, verify_migration.py | verify_migration.py | 12 | read_file/run_shell/list_files/apply_patch | 「迁移脚本完成，logs 表新增 created_at 列…验证脚本通过」 | 否 | 否 |
| coding-042 | documentation | succeeded/succeeded | 判分：真跑测试 `1 failed, 1 passed`（`test_docstring_present` 断言失败） | 1 | timer.py | - | 6 | read_file/apply_patch/list_files/run_tests/run_shell | 「已添加 Google 风格 docstring…docstring 存在且含 Args/Returns 小节：已验证」 | 否 | 否 |

逐例说清（这四条的「原因」互不相同，不能合并成一句"能力不足"）：

1. **coding-008**：活干对了——判分器真跑测试 `testExit=0`、`fib.py` 确实被改。判失败的原因是
   **任务自己的收尾校验**用了 `python -m pytest fib.py -v`（系统 python 里没有 pytest），
   校验节点报"测试未能执行"，任务随之 `failed`。也就是说：**改了、跑通了，但自己没验证成，于是自判失败**。
2. **coding-014**：`POST /tasks/{id}/run` 在 runner 侧断连（`fetch failed`），任务根本没建立。
   这一例被记成 `setup_failed`——**归类没修全**：我本轮只把「`/run` 立刻返回错误体」那一路改成
   `provider_failed`（会触发重跑），断连抛异常的这一路仍落回 `setup_failed`（不重跑）。如实记在这里。
3. **coding-039**：迁移做对了、测试 0 退出码，但**多写了一个 `verify_migration.py` 临时验证脚本**
   （12 次审批、19 步）。判分器按 expectedFiles 硬比对 → `misEditedFiles=1`。这是行为问题，不是能力问题：
   它把"给自己看的验证脚本"留在工作区里了。
4. **coding-042**：第一次被 429 打死（`attempts=2`，runner 重跑过一次），第二次任务 `succeeded`，
   但判分器真跑测试是 `1 failed, 1 passed`：`test_docstring_present` 断言失败。而交付正文写的是
   「docstring 存在且含 Args/Returns 小节：**已验证**」——**自称验证通过，实际没通过**。
   因为终态是 `succeeded`，按定义不计入 `falseDone`（falseDone 只看"非 succeeded 却声称完成"），
   但这条仍要单列：它是"成功任务里的错误自述"，现有指标抓不住，§十 记了这个口径缺口。

注：`coding-014` 那一行是**手填**的。`failure-table.mjs` 按任务标题取"最新一条同名任务"，
这一例本轮没有任务记录，它取到了上一轮同名的旧任务，读出来的原因会张冠李戴——所以把该行
按 runner 自己记录的 `error` 改写，并把任务侧字段留空，不做猜测。

## 五、Token 全链路

### 5.1 七个字段：任务书叫法 → 仓库字段 → 谁负责填

| 任务书字段 | 仓库字段（`Usage` / `model_usage`） | OpenAI 非流式 | OpenAI 流式 | Responses | Anthropic | OpenRouter | Ollama |
|---|---|---|---|---|---|---|---|
| `input_tokens` | `prompt_tokens` | `usage.prompt_tokens` | 末帧 usage | `input_tokens` | `input_tokens + cache_read + cache_creation`（Anthropic 的 input 不含缓存，三块相加才是真实输入总量） | 同 OpenAI 兼容 | `prompt_eval_count` |
| `output_tokens` | `completion_tokens` | `usage.completion_tokens` | 末帧 usage | `output_tokens` | `output_tokens` | 同 OpenAI 兼容 | `eval_count` |
| `total_tokens` | `total_tokens` | 由 input+output 求和（不信供应商自报的总数，避免两边算法不一致） | 同左 | 同左 | 同左 | 同左 | 同左 |
| `cache_read_tokens` | `cached_tokens` | `prompt_tokens_details.cached_tokens` | 末帧 | `input_tokens_details.cached_tokens` | `cache_read_input_tokens` | 同 OpenAI 兼容 | 无此概念（0） |
| `cache_write_tokens` | `cache_write_tokens` | 无（自动缓存不计写入费） | — | — | `cache_creation_input_tokens` | — | 无 |
| `reasoning_tokens` | `reasoning_tokens` | `completion_tokens_details.reasoning_tokens` | 末帧 | `output_tokens_details.reasoning_tokens` | 无（thinking 计入 output） | 同 OpenAI 兼容 | 无 |
| `usage_source` | `usage_source` | `provider` / `unknown` | 同左 | 同左 | 同左 | 同左 | 同左 |

三条硬规则：

- **供应商回了 usage 才记数字**；没回就 `usage_source='unknown'` 且数字保持 0——但报表里读成
  「这行没账」而不是「这次调用免费」。窗口内实测：`unknown` 且 token=0 的 238 行，`status` 全是 `error`。
- **流式从末帧取**：`stream_options.include_usage` 显式要，否则流里没有 usage，账会全 0。
- **每次 retry / fallback 各占一行**：`test_each_attempt_of_a_retry_takes_its_own_usage_row`、
  `test_fallback_switch_records_usage_under_backup_model` 覆盖；GUI 那个 429 任务实测
  3 次调用 = 3 行（1 次原始 + 2 次退避重试）。

### 5.2 三处同口径对账（不是"看起来一致"，是逐行加得上）

拿评测库里一个真实任务（`bf935abd-d617-40a6-ace8-6526556b2d37`，glm-4.5-flash）对三遍：

| 口径 | 值 |
|---|---|
| 证据包 `usage` | `calls 13`、`total_tokens 79,770`、`usage_source_calls {provider:12, unknown:1}`、`models ["glm-4.5-flash"]` |
| 账本 `model_usage`（按 task_id 直查库） | 13 行、`sum(prompt+completion) = 79,770`、来源 `provider 12 / unknown 1`、模型名全是 `glm-4.5-flash` |
| Benchmark 结果里的 `tokenUsage` | 同一个 13 / 79,770 / `{provider:12,unknown:1}` |

三处加得上，且没有一行是「模型=供应商名」的假模型（窗口内 `model = provider.name` 的行数 = **0**；
历史 360 行是 v160 及更早留下的，本轮没有回填改历史）。

### 5.3 最终全量的 token

| 指标 | 值 | 说明 |
|---|---|---|
| totalTokens | **2,975,698** | 54 个判过任务的账本行之和（`prompt 2,873,445 + completion 102,253`） |
| 平均每例 | **55,106** | 分母是判过的 54 例（coding-014 没建立任务，无账可算） |
| P95 | **120,799** | 排序后第 95 百分位 |
| 失败例消耗 | **409,868** | 4 例未通过的账（占总量 13.8%） |
| 按模型 | `glm-4.5-flash`：450 次调用 / 2,975,698 tokens / 54 例<br>`(unknown)`：111 次调用 / **0** tokens / 41 例 | 第二行不是"另一个模型"，是**没拿到模型名**的失败调用（429、超时、guardian 报错）。它们按设计归到 `(unknown)`，不再拿供应商名顶替（§八 #2） |
| 按来源 | `provider` 450 行 / `unknown` 111 行 | `unknownUsageTasks = 0`：**没有任何一例是整例没账**——111 行是零散的失败调用，不是"这一例的账全丢了" |

那个 111 行 / 0 token 的桶要解释清楚，否则容易被读成"19% 的调用没记账"：
它们全部是 `status='error'` 的调用（429 被打死、模型超时、guardian 审查异常），
供应商一个 token 都没回。**记 0 是因为真的没发生计费调用，不是因为忘了记**；
同一批里但凡有一行 `provider`，这一例就不算 unknown（`usage_source_calls` 按行计数）。

## 六、npm 干净安装（全新 prefix，不复用仓库任何东西）

目录：`%TEMP%\blank-ai-v161`（`npm-prefix` / `data` / `workspace` 全新）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 打包 | `npm pack`（packages/cli） | `blank-ai-cli-0.1.0.tgz`，3 files |
| 安装 | `npm install -g --prefix $TEMP/blank-ai-v161/npm-prefix <tgz>` | `added 1 package in 1s`，生成 `blank-ai{,.cmd,.ps1}` |
| `--help` | `blank-ai --help` | 退出码 0，列出 start/init/doctor/list/add/remove 与 `--workspace` 示例 |
| `doctor` | `blank-ai doctor` | `Node.js: v24.20.0` / `平台：win32` / 配置目录为新建的 `blank-ai-v161\home` |
| `start --workspace` | 干净 cwd + 独立 data 目录，端口 8023/3023，`BLANK_SOURCE_ROOT` 指向源码根 | 日志：`→ 工作区: …\blank-ai-v161\workspace（活动）`、`→ 后端:  使用 services/api/.venv 的 Python` |
| 空库初始化 | — | `[providers] 初始化 29 个默认模型厂商（全新空库）` |
| 迁移 | 启动自动 `alembic upgrade head` | `alembic_version = 0030_usage_source`，`model_usage.usage_source` 列存在 |
| `/health` | `GET :8023/api/v1/health` | `{"status":"ok","database":"connected"}` |
| 静态页 | `GET :3023/` | 200 |
| 关闭无孤儿 | 停进程后枚举监听与命令行 | 8023/3023 均 0 监听；无 node/python 残留 |
| 重启保留 | 不带 `--workspace` 重启 | health 200、界面 200、`GET /projects` 仍是 1 个项目 + 1 个活动工作区 |

两点如实记录：

- 第一次启动撞上 **v160 遗留的 8021/3021 监听进程**（`python` PID 18924 起于 11:42），
  新实例报 `EADDRINUSE` 退出。这是上一轮验证没清干净的残留，已清掉；本轮末尾的孤儿检查是干净的。
- `GET /workspaces` 里 `path` 返回 `<workspace:workspace>` 是后端 `mask_path()` 的**有意脱敏**，
  不是缺陷；CLI 打印的是用户给的本地路径。
- **「无源码依赖」要分两半说清楚**：npm 包本身不 import 仓库任何代码——`--help` / `doctor` / `list` / `add`
  在不设任何环境变量时都能跑（`list` 在全新 HOME 下输出「还没有启用可选功能」）。
  但 `start` 要拉起 Python 后端和静态页，靠 `BLANK_SOURCE_ROOT`（含 `services/api` 与 `web/out`）
  或 `BLANK_PYTHON` 指定；不设时 `doctor --sandbox` 会明确报
  「找不到后端 Python：设置 BLANK_PYTHON，或在源码工作区里先建 services/api/.venv」，
  日志走 `→ 后端: 使用 services/api/.venv 的 Python`。这是设计上的部署约束，不是隐藏依赖，本轮按实记录。

### `0030_usage_source.py` 的六段验证（迁移挂了会直接挡住启动）

不改迁移文件，直接在**真实库存库的副本**上跑（库里有 392 行 `model_usage`），日常库不受影响：

| 场景 | 操作 | 结果 |
|---|---|---|
| 存量库 downgrade | `alembic downgrade 0029_task_scenario` | 列被删、版本回 `0029`、**392 行一行没少** |
| 存量库 upgrade | `alembic upgrade head` | 版本 `0030_usage_source`、行数仍 392、存量行 `usage_source` 全部 `unknown`——正是文档承诺的「存量行不冒充 provider」 |
| 重复 upgrade | 同库再跑一次 `upgrade head` | 空操作，退出码 0，行数与版本不变 |
| 空库 upgrade | 全新 sqlite 从 0 跑到 head | 22 张表建成、版本到 `0030`、`usage_source` 列在、`model_usage` 0 行 |
| 半迁移现场 | 手动把版本改回 `0029` 但列留着，再 `upgrade head` | 存在性检查生效：不报「列重复」、不丢数据，版本推进到 `0030`，`pragma table_info` 里该列只出现 1 次 |
| revision 链 | 解析全部 30 个迁移文件的 `revision` / `down_revision` | 无重复 revision；从 `0030` 回溯 30 级到 `0001_projects` 无断链；唯一 head = `0030_usage_source` |

## 七、Desktop 真机（Electron）证据

**环境**：安装版 `D:\Softwares\kbg\blank-ai-workbench-desktop`。
后端进程命令行 `…\resources\backend\runtime\python.exe -m uvicorn blank_api.app:app --host 127.0.0.1 --port 8000`
（PID 27220，16:35 启动；16:18 已用 `self-deploy.cjs --api` 把本轮的 `shell.py` 修复同步进安装目录，
`grep normalize_relative_executable` 在安装目录里能命中 2 处）。`GET /api/v1/health` → `{"status":"ok","database":"connected"}`。
窗口标题「空白格 bot」，客户区 1584×1017，是真实 Electron 窗口（非浏览器预览页）。

### 7.1 真实读文件 → 改文件 → 跑测试（一条任务走完）

任务 `95efa9ec-9416-42bd-aa06-ae7899ed085e`，场景「开发」，工作区 `v160-gui-project`：

| 环节 | 证据 |
|---|---|
| 提示词 | `task.started.goal` 与用户原句逐字一致（没有任何场景前缀） |
| 模型 | `turn.context` → `model=deepseek/deepseek-chat-v3.1`，`provider=openrouter-ds31` |
| 计划 | `plan.created`：读取 src/calc.py → 修改 average 函数 → 运行项目测试 |
| 读 | `tool.requested{read_file, path:"src/calc.py"}` |
| 改 | `tool.requested{apply_patch}` + `file.changed{path:"src/calc.py", modified}`；落盘 `git diff` 为 +2 行（`if len(numbers) == 0: return 0.0`） |
| 审批 | `guardian.reviewed{run_tests, outcome:"auto_approved", verdict:"allow"}`，理由「运行测试通常不会破坏数据或泄露凭据」 |
| 跑测试 | `validation.completed`：`E:\temp\v160-gui-project\.venv\Scripts\python.exe -m pytest -q`，`exit_code=0`，`2 passed in 0.02s` |
| 交付 | 正文「结论：average 空列表返回 0.0…／测试全部通过」；因为校验**真的跑了并通过**，正文**没有**出现「未做自动化校验」注记——正是 §八 #1 修复后的期望行为 |
| 账 | `usage.by_model` = `deepseek/deepseek-chat-v3.1` 8 次调用 / 42,505 tokens；`usage_source_calls {provider:7, unknown:1}` |

界面右栏同步显示「完成 · 工具调用 3 · 改动文件 1 · 耗时 21s · TOKEN 42,505」，
改动文件列表为 `src/calc.py（修改）`。截图：`%TEMP%\v161-gui-04-done.png`。

这条任务的 `validation.completed.command` 就是 §八 #6 那个缺陷的**修复后形态**：
本轮修复前，同一条命令是 `.venv/Scripts/python.exe -m pytest -q`，Windows `cmd` 把 `/` 当参数分隔，
把 `.venv` 当成命令名，报「'.venv' 不是内部或外部命令」，一条测试都没跑（v160 的 `d7143491` 就是这样）。
现在 `normalize_relative_executable()` 在 `run_process` 入口把它换成绝对路径，`git diff` 与 pytest 结果都是真的。

### 7.2 模型切换：请求真的换了上游

三个下拉都是原生 `<select>`，不能用 UIA 读、也不吃坐标点击，但**点击 + 方向键 + Enter 可以驱动**（本轮实测，见 §十 修正）：

| 选择 | 任务 | provider / model | 上游 | 结果 | 账 |
|---|---|---|---|---|---|
| 默认模型（当时=openrouter） | `95efa9ec` | openrouter-ds31 / `deepseek/deepseek-chat-v3.1` | `openrouter.ai/api/v1` | succeeded，4 步 | 8 calls / 42,505 tokens |
| glm-4.5-flash | `355f4b0e` | glm-4.5-flash / `glm-4.5-flash` | `open.bigmodel.cn/api/paas/v4` | failed：429 `code 1302` 账户限流；含 2 次 `model.rate_limit_retry`（500ms / 1000ms 退避），每次重试**单独一行** usage | 3 calls / 0 token → `models: []`、`by_model: {"(unknown)":3}` |
| 默认模型（此时=glm） | `6ffcb009` | glm-4.5-flash / `glm-4.5-flash` | 同上 | succeeded，1 次调用 | 5,263 tokens |

三点值得注意：

- 换的不只是名字，`task.started.provider/model`、`turn.context.model`、`world.state.operating_mode` 三处同时变，
  打的是两个不同域名的上游；
- 那个 429 任务正好验证了 §八 #2：`models` 是**空数组**、`by_model` 归到 `(unknown)`，
  修复前这里会出现一个叫 `glm-4.5-flash`、token 为 0 的假模型；
- 同一次失败里 3 次调用各记一条 usage（1 次原始 + 2 次重试），没有合并成一条。

### 7.3 场景切换 + 日常「记住 / 不记住」

下拉从「开发」切到「日常」后，`task.started.scenario` 从 `dev` 变成 `daily`，输入框下方出现竖排记忆开关
（截图 `%TEMP%\v161-gui-10-scene-daily.png`）。两向都验了：

| 开关 | 任务 | `memory_opt_in` | 写入 | 检索 |
|---|---|---|---|---|
| 记住习惯（开） | `6ffcb009`「记住：跑测试必须用 .venv\Scripts\python.exe -m pytest」 | `true` | `memory.consolidated` → `data/memory/v160-gui-project-c8042601.md`，`added=1`，`total_chars=963` | — |
| 不记住（关） | `97c836b3`「记住：部署脚本是 …\deploy.ps1」 | `false` | **没有** `memory.consolidated` 事件；记忆文件 md5 前后一致（`bd7f171ebca7…`） | 同一任务的 `context.curated` 里仍带回了上一条记住的测试命令偏好 |

也就是说：**读**受项目记忆影响、**写**由开关决定，开关关掉不会静默写入。

### 7.4 本轮没做到的（不写成做到）

- 审批卡片的合成鼠标点击仍不稳定（本轮 `run_tests` 走的是 guardian 自动放行，没有出现人工审批卡，
  所以这一条本轮没有新证据，沿用 v160 的记录：卡片内容/命令/风险等级已截图，最终用同一审批通道的后端接口放行）；
- 日常场景「记住」那条任务的交付正文落库成了模型原始 JSON（`{"intent": "answer", …}`），
  界面上读起来像没答完。这是本轮新观察到的问题，**没修**，记在 §十。

## 八、本轮修掉的缺陷（六处产品代码 + 两处评测工具，都有测试）

| # | 缺陷 | 证据 | 修复 |
|---|---|---|---|
| 1 | **交付正文可以照写"已完成/测试通过"，而校验节点明明给了 skipped**。写交付的那次模型调用根本不知道校验结论，提示词里只有"没做完不许写成做完了"这句笼统要求 | v160 证据包里 `validation.status=skipped`（"命令执行未获批准"），交付正文仍写"已修复" | 提示词注入本次校验事实（`_validation_fact`：「自动化校验：未执行（原因）。有改动就必须说明「未验证」，不许写测试通过」），并在正文末尾追加**确定性**缺口句（`_validation_note`，模型改不掉）；纯问答（无改动）不加，模型自己说过了不重复 |
| 2 | **按模型拆账里多出一个以供应商命名的假模型**：失败调用没拿到模型名时，落库用 `provider_name` 顶替，于是 `tokensByModel` 出现 `bench-glm45flash-v158: calls 89 / tokens 0` | `last-run-v160.json` `summary.tokensByModel` 两行，一行 289 万、一行 0 | 未知模型名就留空（`model=model or ""`），证据包归到 `(unknown)`；供应商维度另有 `providers` 列。测试 `test_a_failed_call_does_not_invent_a_model_name` |
| 3 | **"这例 token 完全没账"被算成"这例账不可信"**：`unknownUsageTasks` 用 `usage_sources.includes("unknown")` 判，只要 9 次调用里有 1 次重试没拿到 usage 就整例算未知 → 44/54 虚高，读账的人会以为整份统计不可信 | `last-run-v160.json`：`provider,unknown` 44 例、`provider` 只有 10 例 | 证据包新增 `usage_source_calls`（按行计数），runner 改为「一行 provider 都没有才算 unknown」，并新增 `providerCalls` / `unknownUsageCalls`。测试 `test_usage_source_calls_tells_one_lost_call_from_no_ledger` |
| 4 | **供应商限流与 Agent 失败混算**：429 打死的任务在完成率里被当成能力不足，也无法在报告里指名 | v160 全量里 7 例 429、1 例 `fetch failed` | runner 新增 `provider_failed` 独立归类（只匹配限流/断网/不可达一类信号，逐例打印原因）并重跑这一例；重跑策略见 #8 |
| 5 | **测试装置自撞限流**（本机 120 RPM 中间件）在整批跑时必然触发，报出来像产品缺陷 | `test_origin_run_v27::test_approved_resume_is_the_same_round_not_a_new_one` 全量里失败、单跑通过，原因是 `rate_limit_exceeded` | v160 只在共享 `client` 夹具里关限流，自建 `Env` 的用例拿不到；改为 `conftest.py` 全批 `BLANK_RATE_LIMIT_RPM=0` 并写明理由 |
| 6 | **相对路径的可执行文件在 Windows 上跑不起来**：`run_tests` 默认命令 `.venv/Scripts/python.exe -m pytest -q`，`cmd` 把 `/` 当参数分隔、把 `.venv` 当命令名，一条测试都没跑就失败。这是 GUI 真机跑出来的，不是看代码看出来的 | v160 任务 `d7143491` 的 `run_tests` 输出「'.venv' 不是内部或外部命令，也不是可运行的程序」；本轮先在真机复现，再手工确认 `tools/x.exe` 这类写法同样失败 | `services/tools/shell.py` 新增 `normalize_relative_executable()`：仅在 Windows、首段含 `/` 或 `\`、非盘符/UNC、且候选路径**确实存在**时替换成绝对路径（含空格自动加引号）；list 形式只改 program。挂在 `run_process` 入口，宿主与沙箱两条路径都覆盖。测试 `test_shell_relative_exec_v161.py` 7 例（含一条真跑 `.bat` 验证） |

| 7 | **审查员的用量被记成"没拿到"**：`guardian` 明明在响应里回了 usage，落账时手搓 `Usage(prompt_tokens=…, completion_tokens=…)` 不带 `source`，默认值 `unknown` 就把真实用量写成「供应商没说」。按来源拆账时审查员这块整块掉进 unknown 桶 | 修复前：窗口内 `usage_source='unknown'` 且 token>0 的行**全是** `guardian_review`（31 行 / 12,036 tokens）。修复后同一库实测：`guardian_review` 里 `status=ok` 的 3 行全部 `usage_source=provider`（1,362 tokens），`status=error` 的 53 行仍是 `unknown`（本来就没用量） | `GuardianVerdict` 带上 `usage_source`（来自 `response.usage.source`），`graph.py` 落账时透传；没回 usage / 超时的路径仍如实是 `unknown`，不拿上一次的顶上。测试 `test_verdict_carries_usage_source_instead_of_defaulting_to_unknown` |
| 8 | **限流重跑策略不够用，且"首轮就 429"被记成装置坏了**：55 例里 10 例标成 `setup_failed`，其实 7 例是供应商 429/超时；60s 后重跑一次也不够——glm 免费档的 1302 是账户级限流，60s 没退干净，4 例重跑后仍被打死 | 第一次全量（`last-run-attempt1.json`）：`setupFailed 10`、`providerFailed 4`，14 例没跑成 | ① 归类：`/run` 即刻返回的错误体也走同一条「供应商失败」正则，命中即 `provider_failed`，不再冒充 `setup_failed`；② 退避：重跑两次、等 90s / 180s，本地 `fetch failed` 另给一次 30s 重跑；③ 汇总：`setupFailed` / `runnerError` / `timedOut` 分开数，不再用减法混成一类；④ 分母改回**全部例子**（原本拿 judged 当分母，14 例没跑成也能印出 passRate 100%） |

另加一个工具：`evals/benchmarks/coding/failure-table.mjs` —— 按任务 id 回后端取证，把「终态/期望、失败原因、测试退出码、修改文件、额外文件、审批数、工具调用、交付正文、
falseDone、未授权高风险」摊成一张表（§四 就是它输出的）。写它的过程中踩到一个坑并修掉：
`GET /tasks` **不认 `offset`，会静默忽略**，按 offset 分页会永远拿到同一页、死循环。


## 九、安全检查

| 项 | 结果 |
|---|---|
| 仓库内明文密钥扫描（`sk-…` / `nvapi-…` / `Bearer …` 字面量） | **只命中 3 处测试夹具的假 key**（`tests/test_approval_memo_v37.py` 等，值均为 `sk-abcdefghijklmnop…`），无真实密钥 |
| 新增代码是否写死本机绝对路径 | 没有。`failure-table.mjs` 走 `BLANK_BENCH_API` 环境变量；新增测试全部用 `tmp_path` |
| 报告与提交信息是否含密钥/token | 没有；安装目录与临时目录统一写成 `%TEMP%\…`、`D:\Softwares\…` 这类环境相对写法 |
| 证据截图是否泄露主机路径 | 没有；界面里的工作区一律显示标签名（后端 `mask_path()` 脱敏生效） |
| 库文件是否入库 | 没有；`git status` 干净，`data/`、`*.db`、密钥文件都在忽略范围 |


## 十、未完成项与真实原因

| 项 | 状态 | 真实原因 |
|---|---|---|
| 审批卡按钮的**合成鼠标点击** | 未稳定通过 | 安装版窗口里，Chromium 内容不走 UIA，只能按坐标合成点击。本轮第一次点击生效、第二次同坐标点击不生效（界面仍是未审批态）。最终通过**同一个审批通道的后端接口**放行并继续（卡片内容、命令、风险等级均已截图取证）。这是本机自动化输入的限制，不是产品行为——手动点击正常 |
| 场景/模型/工作区三个**原生 `<select>` 下拉** | **已验证**（修正前一版结论） | 上一版写「UI 层未验证」是错的：合成**鼠标点击**确实打不开（Chromium 把弹窗画在自己的图层里，试了 6 个坐标都不弹），但**点击聚焦 + 方向键 + Enter 可以驱动**。本轮用它把模型切到 `glm-4.5-flash` 再切回、把场景从「开发」切到「日常」、把记忆开关从「记住」切到「不记住」，每次都截图并与后端事件对齐（§7.2 / §7.3） |
| 评测中部分任务被供应商限流打断 | 见 §三 | `glm-4.5-flash` 免费档账户级限流（错误码 1302）。第一次全量因此有 7 例标成 `setup_failed`、4 例 `provider_failed`；改成「归类 + 90s/180s 两次重跑」后单例复跑 14 例**一次 429 都没有**。仍失败的不计入能力分母，逐例列在 §四 |
| 场景「切换」本身 | 已不适用 | 「场景模式」在 2026-10-21 已删除（它会把用户的句子拼上前缀，污染意图判定）。现在输入框下方的「开发」是任务档位（`scenario`），不是旧场景模式；本条按现状记录，不当成未完成 |
| 日常场景纯问答的**交付正文**落库成模型原始 JSON（`{"intent":"answer","note":"…","steps":[],"answer":""}`） | 未修 | 任务 `6ffcb009` / `97c836b3` 都是 `intent=answer`，`answer` 字段模型给空串，回退逻辑就把整段 JSON 当正文，界面上看着像没答完；记忆固化也把这段 JSON 当「结果」写进了记忆。修它要动 answer 分支的解析与回退，属于新缺陷不是本轮收尾范围，所以只记录不改，避免在收尾时引入未验证的改动 |
| **质量基线未达标**：completionRate 87%（要求 ≥90%）、testPassRate 96%（要求 100%）、misEditedFiles 1（要求 0） | 如实记，不修饰 | 三条各对应一例，都是真实行为问题，没有一条能靠改算法变绿（见 §三 3.4 / §四）。要修的是行为，不是指标 |
| 收尾校验用错解释器导致自判失败（coding-008） | 未修 | 它写 `python -m pytest fib.py -v`，系统 `python`（3.13）里没有 pytest；校验节点报「测试未能执行」→ 任务 `failed`。判分器用项目自带 venv 真跑是通过的。要么在提示里把"用工作区的 venv"写死，要么校验节点在命令找不到时退回项目解释器——两条都属于行为改动，本轮只登记 |
| 收尾把临时验证脚本留在工作区（coding-039） | 未修 | 它建了 `verify_migration.py` 自证迁移正确，但没删。`expectedFiles` 硬比对如实记 `misEditedFiles=1`。合理修法是把"临时脚本"归到可清理的产物，或在交付前清场——同属行为改动 |
| 「成功任务里的错误自述」现有指标抓不到（coding-042） | 口径缺口，未修 | 任务 `succeeded`，交付正文写「…已验证」，判分器真跑测试 `1 failed`。`falseDone` 按定义只统计"非 succeeded 却声称完成"，所以这里记 0；它其实是"过了终态但结论错"。要抓它得新增"自述 vs 事实"一致性检查（拿交付正文里的完成断言与 `validation` / 测试退出码比），本轮没做 |
| runner 的失败归类还有一条漏网（coding-014） | 未改完 | `POST /run` **断连抛异常**那一路仍记 `setup_failed` 且不重跑；只有"立刻返回错误体"那一路被改成 `provider_failed`。修它要动 `runTask` 的 `.catch` 分支，本轮在跑分中途没再改工具（改了会让同一份结果混合两个版本） |


## 十一、提交

提交前的三条命令（原文）：

```text
$ git status --short
 M evals/benchmarks/coding/last-run.json
 M evals/benchmarks/coding/run.mjs
 M services/api/src/blank_api/services/agent/evidence.py
 M services/api/src/blank_api/services/agent/graph.py
 M services/api/src/blank_api/services/agent/usage.py
 M services/api/src/blank_api/services/guardian.py
 M services/api/src/blank_api/services/tools/shell.py
 M services/api/tests/conftest.py
 M services/api/tests/test_guardian_v12.py
 M services/api/tests/test_rate_limit_config_v152.py
 M services/api/tests/test_usage_levels_v17.py
 M services/api/tests/test_validation_gate.py
?? docs/workbuddy-v161-final-report.md
?? evals/benchmarks/coding/failure-table.mjs
?? evals/benchmarks/coding/last-run-attempt1.json
?? evals/benchmarks/coding/last-run-failed-rerun.json
?? evals/benchmarks/coding/last-run-v160.json
?? services/api/tests/test_shell_relative_exec_v161.py

$ git diff --check
（无输出，退出码 0）

$ git diff --stat
 12 files changed, 1470 insertions(+), 737 deletions(-)
```

上面 18 个文件就是这次提交的全部内容，没有一个是"顺手带上"的：

- 12 个改动 + 1 个新测试 = §八 的 8 处修复与配套测试；
- `failure-table.mjs` = §四 那张表的生成器；
- 三个 `last-run*.json` = §三 三段跑法的原始结果（`-v160` 是上一轮的对照，`-attempt1` 是第一次全量，`-failed-rerun` 是单例复跑）；
- 报告本身。

**未提交、也不想悄悄提交的**：`evals/benchmarks/coding/` 下 `failure-analysis-v159.json`、
`glm-fix-verify.json`、`rerun-failed-v160.json`、`merge-rerun.py` 是前几轮的取证材料，本轮没动它们，
也不在本提交里；`services/api/data/*`（含 `.db`、密钥）与 `$TEMP` 下的一切都在忽略范围。

提交信息（不含 `final` / `release` / `complete` 字样）：

```text
v161 收尾：限流归类与退避重跑、交付正文校验事实、token 来源按行计数、Windows 相对路径可执行文件
```

提交哈希与提交后复查：

```text
$ git commit …
[main 50525f8] v161 收尾：限流归类与退避重跑、交付正文校验事实、token 来源按行计数、Windows 相对路径可执行文件
 18 files changed, 8285 insertions(+), 737 deletions(-)

$ git status --short
（无输出——工作区干净）

$ git diff --check
（无输出，退出码 0）
```

`50525f8` 是产出与代码的落点；报告本身的回填与随后的文字更正都是 `50525f8` 之后的连续提交
（`git log --oneline 50525f8..HEAD` 可列全，写这句时是 `0c322e8` 起的三条）。三次以上都不含
`final` / `release` / `complete` 字样，提交后 `git status --short` 为空。
报告里所有数字都出自 `50525f8` 里那三个 `last-run*.json`，不是写报告时手算的。
