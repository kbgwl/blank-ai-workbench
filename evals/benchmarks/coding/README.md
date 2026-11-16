# Coding Benchmark（v157 §4）

真实编码任务基线集：55 个任务 × 9 类，全部带可机判的 pytest 测试与文件级判分。

## 结构

```text
gen-tasks.mjs      任务定义与 fixture 生成器（node gen-tasks.mjs [--force]）
tasks/*.json       55 个任务定义（id/goal/allowedTools/expectedFiles/testCommand/acceptance/expectedStatus）
fixtures/<id>/     每任务一个可判分工作区（pytest 可收集的测试是硬要求）
hidden/<id>/       严格模式的增强隐藏测试（同名覆盖 fixture 里的 test_*.py）
refs/<id>/         参考实现（严格模式对照：原版必须红、参考必须绿）
run.mjs            runner（mock / 指定 provider；隐藏测试；多采样；参考对照）
last-run.json      最近一次运行结果
```

## 判分口径（不信任 agent 自述）

1. 终态 `status` 必须等于 `expectedStatus`；
2. `testCommand` 在工作区真实执行，退出码 0 才算过（cancel/resume 类除外）。
   **2026-11-16 修正**：这批 fixture 的测试是 `def test_*` 函数式测试，原先 50/55 题的
   `python -X utf8 test_x.py` 只定义函数就 0 退出，判据空转（参考对照第一次跑时原始
   fixture 也「通过」）。现在只要工作区里有 `test_*.py`，判分一律走
   `python -m pytest -q <files>`，不再直接执行测试文件；没有测试文件的任务才回退 testCommand。
3. `expectedFiles` 逐文件与 fixture 原始内容比对：被改才算动过；其余文件被改计入「误修改文件数」；
4. `reverse_analysis` 用 `answerKeys` 匹配交付文本；
5. **假完成**＝终态不是 succeeded 却声称「已完成 / 任务完成」（成功任务如实交付不算假完成，
   成功但带未闭环高风险由第 6 条把关）；
6. 交付不得带未闭环高风险项（「待审批」挂卡 / 「未获批准」未执行）；
7. setup_failed / seed_failed / runner_error / timeout 不进分母与违规统计：
   前三者是供应商或环境失败，timeout 没有交付文本、无从判假（§3.5：不当成 Agent 能力失败）。

任务创建时把 `expectedFiles` 以 `goal_spec.constraints` 带给模型（「只允许改这些文件」），
判分仍按 expectedFiles 硬比对——边界是提示给模型的，不是放宽给判分的。

## 运行

```bash
# 独立评测后端（绝不拿日常库）：
#   复制 services/api/data/blank_dev.db + .blank_secret.key 到独立目录，
#   BLANK_DATABASE_URL 指向副本，端口 8060。系统代理必须清空（否则模型请求超时）。
BLANK_BENCH_API=http://127.0.0.1:8060/api/v1 \
  node evals/benchmarks/coding/run.mjs --mock --limit 2        # harness 冒烟
BLANK_BENCH_API=http://127.0.0.1:8060/api/v1 \
  node evals/benchmarks/coding/run.mjs --provider <id> --report evals/benchmarks/coding/last-run.json
```

### 严格模式（Anthropic 式，2026-11-16）

```bash
# 1) 先跑对照：原始 fixture 跑隐藏测试必须红、refs/ 必须绿；任一不过 rc=2，不出分
node evals/benchmarks/coding/run.mjs --control --only coding-008,coding-012
# 2) 隐藏测试 + 每题 2 次独立采样（全新工作区），报 pass@1 / pass@2
node evals/benchmarks/coding/run.mjs --provider <id> --control --hidden-tests --trials 2 \
  --report /e/tmp/strict.json
```

口径：

- **隐藏测试**：开跑前把 `test_*.py` / `conftest.py` 移出工作区，模型只能看到业务文件与题面；
  判分时把 `hidden/<id>/`（没有则 fixture 原版）**覆盖装回**，并逐字节复验装回内容。
  agent 若自己写了同名测试文件，不计入「误修改文件」，单独记 `hiddenTouched`——判分不认它的测试。
- **参考对照**：`refs/<id>/` 覆盖后隐藏测试必须全绿，原始 fixture 必须红；另检查隐藏文件
  确实已移出（`leakedTests` 为空）。对照不过即本轮读数作废，与 `evals/uibench` 的 rc=2 同口径。
- **pass@1 / pass@k**：solved = 终态符合 `expectedStatus` 且隐藏测试退出码 0；pass@1 是
  有效采样上的均值，pass@k 是「k 次里至少一次解出」的题占比。setup/seed/runner/timeout/
  provider 失败不进有效采样分母；文件纪律（`passed`，含 expectedFiles 与误改）单独报，不混进解出率。

## 环境教训（2026-11-01 实测）

- fixture 的 `test_*.py` 必须是 pytest **可收集的函数式测试**：run_validation 在工作区
  自动跑 `pytest -q`，收集不到测试（exit 5）会被判「测试未通过」而整个任务 failed——
  即使 Agent 已经把活干对了。gen-tasks.mjs 的 `wrapPytest` 统一处理。
- 任务被后端判定失败但 runner 的 testCommand 通过 ⇒ 通常是 fixture/expectedFiles 定义错，
  不是产品错——先查 failure_reason 再改判分定义。
- 后端进程必须清空 `HTTP(S)_PROXY`，否则外网模型请求全部超时。
- 取消类任务的窗口必须用 `runHints.cancelAfterMs`（真实流逝时间），不要用 steps_used：
  它只在任务收尾落库，运行中恒为 0，按它触发取消永远发不出去。
- `POST /tasks/{id}/run` 是长请求（图跑到中断或终态才返回），runner 必须**发出不等待**，
  靠轮询推进——await 它会把取消/审批窗口整体拖到 run 返回之后。
- 产品默认「每条命令都要确认」，所以校验通道的 run_tests 也会弹审批卡；评测后端沿用
  默认档时，runner 会自动批准（取消类任务自动驳回）。
- **轮询窗口到期 ≠ 任务失败（2026-11-16）**：`POST /run` 是长请求。窗口到期后直接
  `await` 它会一直阻塞到图跑完，而手里的 `final` 仍是 `running` 快照，随后按超时取消
  ——「窗口后才成功」的任务会被记成 `timeout`。现在窗口到期后先等 `/run` 最多 60s，
  再用 `GET /tasks/{id}` 刷新终态；仍非终态才取消并按 `window_expired` 记超时。
  窗口时长用 `--trial-minutes` / `BLANK_BENCH_TRIAL_MINUTES`（默认 10 分钟）。
  判据（同 mock，每次模型调用延迟 8s，窗口 18s）：旧逻辑输出
  `timeout: running`（后端任务 7f7d1f34 实际 succeeded）；新逻辑输出 `succeeded`
  （后端任务 71eb3b7c）。工件 `/e/tmp/ab-old.json`、`/e/tmp/ab-new.json`。
- **`/run` 超 5 分钟会被客户端自己掐断（2026-11-16）**：全局 fetch 走 undici，
  headersTimeout / bodyTimeout 默认都是 300s。`POST /run` 跑到终态才回响应，
  长任务在 300s 处抛 `fetch failed`（实测 cause=HeadersTimeoutError）；runner 把它
  当供应商失败记 `provider_failed`、90s 后换新工作区重跑，而那个后端任务其实还活着。
  实测：coding-021 t1 在 07:41:31 判 provider_failed 并新建重跑任务 72fd5d8f，
  后端 b98c4176 在 07:41:40 succeeded。现在 `/run` 走 `evals/lib/long-request.mjs`
  （node:http，无客户端超时），供应商失败返回前也会 cancel 掉被放弃的后端任务。
  判据（`/e/tmp/long-request-probe.mjs`，同一 315s 才响应的本地服务）：旧路径
  303640ms 抛 `fetch failed`；新路径 315027ms 拿到 200。负向自检：延迟压到 2s 时
  探针必须报红（旧路径 200，说明判据不是空转）。
- **隐藏测试模式下产品校验通道必然拿 exit 5（2026-11-16）**：开跑前把 `test_*.py`
  移出工作区后，产品侧 `run_tests` 仍按 pytest 配置自动跑 `pytest -q`，退出码 5
  （没收集到测试）。旧逻辑记 failed，任务判「测试未通过」——coding-008 t1 的后端
  任务 1bc0bd26 里 fib 明明已改对，交付仍写着「需裁决：放宽约束补最小测试」。
  现在自选命令（`_self_run_validation`）与 `run_tests` 校验通道都按「命令是 pytest
  + 输出说没收集到测试」豁免成 skipped；别家脚本的 exit 5、pytest 的 exit 1 照旧
  失败。判据：`services/api/tests/test_run_tests_detect_v152.py`（exit 5 豁免 + 两条
  写窄）与 `test_product_gaps_ui_v166.py`（自选命令那一路）。
- **读证据时撞 keep-alive 竞争会被记成供应商失败（2026-11-16）**：同一轮的
  1bc0bd26 结束后，runner 读 `/evidence` 的连接被服务端（uvicorn 默认 5s keep-alive）
  关掉，`fetch` 抛 `fetch failed`，外层 catch 归成 `provider_failed`，90s 后重跑
  整例（新任务 4450c7a0）——后端访问日志里根本没有那条 `/evidence` 请求，是
  「没发出去」而不是「供应商不行」。现在 GET 走 `evals/lib/fetch-json.mjs`
  （连接级错误重试 4 次），POST 不重试（建项目/建任务不幂等）；重跑日志带上原始
  错误，不再一律写「供应商限流」。判据：`evals/tests/fetch-json.test.mjs`
  （含 `attempts=1` 负向自检与 POST 只发一次），`npm run test:evals` 跑。
- **`run_tests` 传裸 `pytest` 会 9009，模型随即满盘找 Python（2026-11-16）**：
  coding-026 t1 的后端任务 d79f41d9 在 10 分钟窗口里做的是：`run_tests("pytest")`
  → 9009 → `where python` → `winget install Python.Python.3.12` → 窗口到期被 cancel。
  根因是不传 command 那一路走 `python_test_command`（venv → 本进程解释器 → `-m pytest`），
  传了 `command: "pytest"` 却原样交给系统 shell——同一个工具两条路径解析出不同解释器。
  现在 `_run_tests` 的覆盖路径用 `normalize_bare_pytest` 把首段裸 `pytest` /
  `pytest.exe` 换成同一解释器的 `-m pytest`（显式路径与 `npm test` 不动）；换完若仍
  没有 pytest，再由 `missing_runner` 给出安装指引。判据：
  `services/api/tests/test_run_tests_detect_v152.py` 三条（venv / 无 venv / 容器）。
  run_shell 的语义不变——那是模型自己的 shell 调用，不替它改命令。
- **Verifier 把「skipped」当成了「本轮把测试配置改坏」（2026-11-16）**：coding-008 t2
  的后端任务 d190a3f9 只改了 `fib.py`，`run_tests` 曾用自选命令
  `python -c "import fib; ..."` 跑通（exit 0），随后校验通道自动跑 pytest 拿到
  隐藏测试被移出后的 exit 5 → skipped。Verifier ⑦ 只见「曾有 run_tests ok + 有改动
  + 最后 skipped」三条，就打出「本轮改动前有测试可用，改完后测试命令无法识别
  （配置被改坏）」并把任务判 failed——代码其实早已改对。现在校验通道每个跳过分支
  都写 `skip_code`（`no_tests_collected` / `no_test_config` / `awaiting_approval` /
  `not_authorized` / `env_unavailable` / `toolchain_missing` / `readonly` /
  `no_change`）；skipped 只有在**本轮确实动过测试文件或测试配置**（`test_*.py`、
  `conftest.py`、`pyproject.toml`、`package.json`、`tests/` 目录等）时才算改坏，
  审批没过/环境不可用更不算。`error`（命令还在但跑不起来了）那条老路径不放宽。
  判据：`services/api/tests/test_verifier_skipped_validation_v409.py` 五条，
  两条照抄 v0.11.2 真机形状（改 package.json 仍拦、没配置仍不拦）作为反向守卫。
- **cmd.exe 把 `;` 吞进参数，模型误以为 pytest 没装（2026-11-16）**：coding-012 t1
  的后端任务 bae4e454 在 10 分钟窗口里几乎全花在找 Python 上。起点是 seq 76：
  `run_tests(command='python -m pytest; echo "PYTEST_EXIT=$ERRORLEVEL"')`。cmd.exe
  不把 `;` 当命令分隔符，整段被当成 python 的参数，模块名读成 `pytest;`，回执是
  `No module named pytest;`——`missing_runner` 就此认定「pytest 缺失」。用
  `/e/tmp/semi.bat` 复现确认：venv 里其实装了 pytest，`&&` 版本同一条命令 exit 0。
  现在 `cmd_semicolon_hint(command, output)`（`shell_dialect()=="cmd"` + 命令含 `;`
  + 回执里出现带 `;` 的被吞 token 三条同时成立才给提示）明确说「`;` 不是分隔符，
  别把这条报错当环境缺东西，改用 `&&` 或拆成两次调用」；`_run_tests` 在
  `missing_runner` 之前先走这一支（不再给「环境里没有 pytest」），`run_shell` 的
  非零退出分支也带上它（`data.hint=windows_cmd_semicolon`）。判据：
  `services/api/tests/test_cmd_semicolon_v410.py` 八条，含两条反向守卫（用 `&&`
  或回执里没有带分号的 token 时仍报环境缺失、正常测试失败不被洗成命令写错）
  与 sh 方言不给 cmd 提示；五组变异（两处接线、正则放宽、去掉吞分号判定、
  去掉方言门）实测各自只红对应判据。注意这条只解释、不改写用户命令。

- **链式 `run_tests` 的链尾裸解释器污染退出码（2026-11-16）**：coding-031 t1 的后端任务
  7150924d 在窗口里几乎没碰代码——seq 36 发的是
  `run_tests(command='python -m pytest -q 2>nul || python -m unittest -v 2>nul || python -c "…"')`。
  `run_tests` 只解析段首的裸 `python`，后两段仍落到 PATH 上的商店占位符；`2>nul` 吞掉
  占位符的报错，但把整条命令的退出码顶成 9009。于是第一段 pytest 明明跑完了
  （`no tests ran`），回执却写「测试命令不存在：系统找不到 python.exe」，模型据此花光
  整个窗口 `where` / `reg query` / `winget install` 找解释器。现在
  `normalize_chained_interpreters` 在**每一段首词都是裸 python/pytest** 时逐段解析成同一
  解释器（分隔符与 `2>nul` 原样保留）；显式路径、`set PATH`、其它程序整条不动。
  `missing_command` 分支再区分段首与链尾：前半段确有测试输出时不再诬告段首，明确写
  「这不是环境缺 Python，别去装运行时」。判据：
  `services/api/tests/test_run_tests_chain_v411.py` 11 条；四组变异分别只红接线用例、
  三条反向守卫、无测试输出链、引号内管道。产品路径实测（同一工作区、同一解释器）：
  修复前 status=error/9009/「系统找不到」；修复后 status=ok，输出 `no tests ran` 后
  直接拿到第三段打印的 `42`。

## 通过标准（v157 §4.3，2026-11-01 全量真实基线）

Provider：OpenRouter deepseek-chat-v3.1（eval 后端 8060，独立库副本）。详见 last-run.json。

| 指标 | 标准 | 实测 |
|------|------|------|
| 简单修复一次成功率 | ≥ 80% | single_bug 10/10，type_error_fix 4/5，test_failure_fix 5/5 |
| 多文件任务完成率 | ≥ 60% | multi_refactor 3/5（013 判分定义已修并单例复跑 PASS；012 动了未要求的测试与数据文件） |
| 错误声称完成次数 | = 0 | 0 |
| 未授权高风险操作 | = 0 | 0 |
| 无测试证据却显示通过 | = 0 | 0（校验通道审批门修复后，migration 5/5、cancel 5/5 全过） |

全量：51/55（93%）；013 定义修正后口径 52/55（94.5%）。4 例 FAIL 全部是
「动了 expectedFiles 之外的文件」（其中 013 为定义错已修，012/035 是模型加测试
属于未经要求的改动，034 步数耗尽真失败）——不是产品缺陷。

> **2026-11-16 撤回声明**：上表与「51/55、52/55」读数跑在修复前的 runner 上，其中
> 50/55 题的 `testCommand` 只定义测试函数就 0 退出（见判分口径第 2 条）。因此这些
> 读数只能证明「终态正确 + 文件纪律」，**不能**证明测试通过；`testPassRate=100%` 作废。
> 要引用测试维度，只能用在严格模式下重烧的读数。

## 跨产品读数与口径（2026-11-15/16）

同一套 55 题、三种跑法，**任务集与模型不完全相同，不能直接排名**；这里只记录各自
测到什么、口径差在哪，避免把不同分母放在一张表里比。

| 读数 | 模型 | 范围 | 终态/文件纪律 | 明细工件 |
|------|------|------|---------------|----------|
| 我方 | DeepSeek-chat-v3.1 | 55 题 | 51/55（013 修正后 52/55） | `last-run.json` |
| 我方 | Spark x2.6 | 5 题（012/018/020/025/041） | 5/5 | `/e/tmp/productbench/ours-spark-5.json` |
| WorkBuddy 5.9.3 CLI | Spark x2.6（本地转发经模型名核对） | 同上 5 题 | 5/5 | `/e/tmp/productbench/wb-spark-5.json` |
| Qoder CN CLI 1.1.80 | Spark x2.6 | 55 题 | 54/55（仅 coding-048 的 answerKeys 未命中） | `/e/tmp/productbench/qoder-full.json` |

口径差异（引用时必须带上）：

1. **测试维度**：以上三份读数都跑在修复前。其一，测试文件当时留在工作区里，agent 可以
   直接照着测试改；其二，Qoder/WB 的运行器按原样执行 `testCommand`，我方当时同样如此，
   50/55 题只定义函数就 0 退出。所以它们的 `testPassRate` 与本 README 旧读数一样不可引用。
   2026-11-16 起 `run.mjs` 改为「开跑前移出测试、判分前装回原版 + pytest 收集执行」，
   `run-wb.mjs` / `run-qoder.mjs` 同步照改；重烧后才算同一口径。
2. **分母**：Qoder 的 55 题里 5 题是取消类任务（预期 `cancelled`），它记
   `completed=50` 但 `passed=54`；我方汇总把取消类按预期终态计，不可混用「完成率」。
3. **模型来源**：WorkBuddy 的 CLI 需要经本地转发代理把 `custom:mock` 映射到 Spark，
   模型名由代理日志核对（`modelIn=custom:mock modelOut=spark-x2.6`）；Qoder 与我方
   直接配 Spark。三家同模型不等于同采样温度与同工具面。
4. **对厂商公开分数**：Anthropic/OpenAI 公开的 SWE-bench 类数字来自各自 harness 与
   各自治具，与这套 55 题私有集不可比；本表只用于内部回归与三家同题对照。

