# uibench：单页 UI 任务的 Anthropic 式评测

把「复杂 UI 任务」当成 SWE-bench 类评测来做：**模型看不到判据，交付物由隐藏验收自动评分，
多次独立采样后报告 pass@k**。这里不评模型自述、不看截图主观分，只看页面能不能用。

## 任务

`task/incident-console.prompt.txt`：事故指挥台（Incident Console）——离线单页三文件、
5000 条确定性数据、17 组验收（粘顶、虚拟滚动、roving tabindex、抽屉焦点陷阱、批量三态、
URL/localStorage、390px、reduced-motion、print、XSS、自测）。题面本身不给判据细节。

## 判据（grader）

`grader/tortureprobe.mjs`：静态服务器 + headless Chrome CDP，逐条执行 38 条功能判据，
每条给出 detail；任一条失败退出码为 1，JSON 始终完整落盘。

判据的可信度由两层保证：

1. `grader/ref/` 参考实现必须 **38/38**（每次评测开跑前自动跑一遍；
   不达标直接以 rc=2 中止，后面的模型读数全部作废）；
2. 判据的区分力由变异电池证明（20 个单点变异逐条实测红在指定判据上，
   装置与读数见仓库 `#400` 记录与 `E:/tmp/productbench/torture-mutants-result.json`）。

## 协议

`run_eval.mjs` 对同一提示词做 N 次**独立采样**：

1. 每次一个全新空工作区（模型不知道这是第几次、也不知道判据）；
2. 通过产品 API 建项目 / 绑定工作区 / 建任务 / 运行，等待审批自动放行；
3. 到终态或墙钟上限（默认 70 分钟）后取交付目录，跑 grader；
4. 报告每次得分、**pass@1 均值**（各次得分/38 的平均）、
   **pass@N**（N 次里至少一次 38/38）、逐条判据的跨次通过率与红集。

分数以「38 条判据通过数」计，不设主观加权；超时但已写出页面的，按实际文件评分并如实标注状态。
墙钟上限要覆盖一次「整页三个文件」的生成：实测单次 2.6 万 token 的流要约 10 分钟，
25 分钟会在第二个文件上被截断，读出来的是「装置超时」而不是「产品做不到」。

## 运行

```bash
# 评测实例（隔离 DB，勿用 :8000/:3000）
cd services/api
BLANK_DATABASE_URL="sqlite+aiosqlite:///E:/tmp/productbench/ours-spark.db" \
BLANK_RATE_LIMIT_RPM=0 BLANK_MODEL_TIMEOUT_SECONDS=1800 \
  ./.venv/Scripts/python.exe -m uvicorn blank_api.app:app \
  --app-dir src --host 127.0.0.1 --port 8066

node evals/uibench/run_eval.mjs --trials 2 --timeout-min 70 --out E:/tmp/uibench-eval-<日期>
```

`BLANK_MODEL_TIMEOUT_SECONDS` 必须显著大于单文件生成耗时（600 秒会把流切断，
事件里表现为 `model.stream_fallback / model_stream_empty`），否则测的是超时配置。

可选参数：`--api`、`--provider`（默认 Spark 供应商）、`--timeout-min`、`--max-steps`、
`--port-base`（grader 的静态/调试端口）。

依赖：本机 Chrome（`C:/Program Files/Google/Chrome/Application/chrome.exe`）与 Node 18+。
产物：`<out>/eval.json`（汇总）、`<out>/trial-<i>.probe.json`（逐条 detail）、
`<out>/trial-<i>.events.json`（产品事件流，用于复查工具轨迹）、`<out>/shots/`（截图）。

## 最近一次读数（2026-11-16，Spark x2.6）

| 样本 | 得分 | 状态 | 墙钟 | 说明 |
| --- | --- | --- | --- | --- |
| A | 21/38 | failed | 3271s | 三文件齐；交付自述「未复跑验证」，校验器判 failed |
| B | 6/38 | runner_timeout | 4201s | 只写出 index.html / styles.css；网关空流后非流式吃满超时 |
| C | — | 无交付物 | 4201s | 供应商卡死，按口径不计入 pass@1 |

参考对照三次均为 38/38。pass@1 = 27/76 = 35.5%，pass@2 = 否；逐条：6 条两格都过、
15 条只 A 过、17 条两格都红。

无交付物的采样不是「0 分」，而是装置/供应商层面的失效：没有 index.html 就没有可评的页面，
混进均值会把供应商卡死记成产品做不到。判别与计数见 `run_eval.mjs` 的 `effectiveTrials`。
供应商空流→非流式超时→重试的叠加会把单步耗时推到小时级，70 分钟墙钟会在重试途中到期；
这是评测配置（1800s 超时）与网关状态的乘积，不是页面本身的行为。

原始工件在 `E:/tmp/uibench-eval-*`。
