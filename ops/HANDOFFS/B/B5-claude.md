# B5 开发交接单

## 任务与范围

- 已实现：
  - 用同一组 4 个任务（编程新增功能、编程缺陷修复、数据 CSV、文档 README）
    对空白格 / Claude Code / Codex 做机器判分对标；
  - 每个任务加入 agent 看不到的隐藏验收，改掉“既有测试本来就是绿的”也能拿
    正确性分的假信号；
  - 评测工作区移出仓库、Claude 关闭用户级定制、Codex 显式可写沙箱，
    避免把项目合同/用户配置/只读沙箱混进能力结论；
  - 修正评测自身的三处假分数：T03 fixture 原来并不坏、`.blank/` 被算成
    多余改动、Codex 不可用被算成 15 分；
  - 修掉对标中暴露的两个产品缺陷：
    1. `不要改变对外 API 名称` 被裸“改”正则误判成只读，整轮不许写文件；
    2. 无目录时“做一个官网”被 `chat_only` 放行自动执行，而不是 `wait_user`。
- 明确未实现：
  - 没有跑大型多文件项目、浏览器/桌面任务；
  - Codex 因模型/额度不可用，没有有效成绩；
  - 没有做独立盲评；隐藏验收仍由本仓库编写；
  - 没有按场景分别跑完整评测集。

## 修改内容

- `evals/compare.mjs`：
  - 支持样例 `hidden_files` + `checks`，隐藏验收在改动快照之后注入；
  - 目录型 `expect_files` 按前缀命中；
  - 忽略 `.blank/` 产品自身记账目录；
  - 工作区默认放到仓库外的 `_bench_workspaces`；
  - Claude 加 `--safe-mode`；Codex 加 `-s workspace-write`；
  - Codex 从 `item.completed` 的 `command_execution` 记自证命令；
  - Codex 不可用同时检查 stdout 与 stderr，不再漏掉 JSON 事件里的 404/402。
- `evals/task-samples.json`：
  - T01 明确 truncate 边界，加隐藏测试；
  - T03 fixture 改成真正缺 `.js` 扩展名，加隐藏导入测试；
  - T07 明确 `summarize(path)` 返回结构，加隐藏日报测试；
  - T25 明确三个示例的输入/输出，加隐藏 README 检查。
- `services/api/src/blank_api/services/agent/directives.py`：
  `_NO_WRITE` 去掉裸“改”，避免“改变/改名”被当成“改代码”。
- `services/api/tests/test_directives_v496.py`：补“不要改变对外 API 名称”
  必须是 `NONE` 的回归测试。
- `services/api/tests/test_output_styles_v47.py`：同步 B4 清单规则后的
  `SYSTEM_PROMPT` 哈希；全量回归发现的唯一失败，已单跑 31 passed。
- `services/api/src/blank_api/services/agent/dispatcher.py`：
  `workspace_bound` 只在“纯聊天且不执行”时豁免目录；动作任务无目录必须
  `wait_user`，不能借 `chat_only` 自动开跑。
- `docs/QUALITY.md`（新增）：判分口径、隐藏验收、环境隔离与局限。
- `docs/capability-benchmark-2026-11-22.md`（新增）：本次完整结果与结论。

## 验证结果

| 命令 / 动作 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_v165_supervisor.py tests/test_directives_v496.py tests/test_intent_and_reply.py tests/test_scenarios_v153.py tests/test_chat_only_v495.py tests/test_transition_exhaustive_v485.py tests/test_transition_explain_v432.py tests/test_dispatch_v162.py tests/test_dispatch_runtime_v162.py` | **148 passed** | 指令、意图、场景、纯聊天、下发、跳转全绿 |
| 后端全量 `pytest -q` | **5024 passed / 1 failed / 28 skipped** | 唯一失败是 B4 改系统提示后未同步的哈希；已更新并单跑该文件 **31 passed**；全量未在改哈希后重跑 |
| T03 修复后单跑（ours，deepseek） | **100 分** | 1 个文件改动，156K token，8 轮，13 秒 |
| 最终四任务对标 | **ours 96.5 / Claude 97.5** | 双方 4/4 通过；ours 225K vs Claude 128K token/任务 |
| Codex 探测 | **不可用** | 默认免费模型 404；`gpt-5.6-luna` 402 额度不足 |

最终结果：`evals/results/compare-2026-11-22T10-37-35-830Z.json`

## 风险与需要审查的点

- Claude Code 的 JSON 输出不含工具明细，T25 自证只能从文本判断；它的 90 分
  是下限，不是“没跑测试”的定论。下一轮改用 `stream-json` 才能公平。
- 我们与 Claude 是同一模型家族但不是严格同模型：`deepseek-flash` vs
  `deepseek-flash[1m]`；不能把差异全部归给 harness。
- 任务只有 4 个，且隐藏验收由我们编写，存在实现偏好；不能据此宣称“整体
  超过 Claude”。
- T25 我们 399K token 是明显浪费，下一步应先查文档任务为什么 16 轮、
  每轮带多少历史，而不是继续加功能。
- Codex 未对比；对外汇报必须写“不可用”，不能写“0 分”。
- `_bench_workspaces` 在仓库外，清理由评测脚本逐个删除；本轮结束后只剩空目录。

## 建议人工验收

1. 打开 `docs/capability-benchmark-2026-11-22.md`，核对四任务表与 token 表。
2. 打开 `evals/results/compare-2026-11-22T10-37-35-830Z.json`，确认
   Codex 记录是 `unavailable`，没有被算进平均分。
3. 在界面发「修复导出错误，不要改变对外 API 名称」类任务，确认不再被锁成
   只读；无目录发「做一个官网」应提示先选目录，而不是直接开跑。
4. 若要复现，先启动后端，再运行报告第七节的 `compare.mjs` 命令。
