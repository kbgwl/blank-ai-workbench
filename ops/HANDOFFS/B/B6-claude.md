# B6 开发交接单

## 任务与范围

- 已实现：
  - **T25 成本收敛**：同一 README 任务从 B5 的 **399K token / 16 轮 / 43s**
    降到 **136–156K / 9–10 轮 / 16–23s**，隐藏验收仍 100 分；
  - 链式命令里的裸 `python` 不再漏替换：`set "PYTHONPATH=src" && python -c ...`
    会按段换成项目 venv 或应用解释器，Windows 上不再先报 9009 再 where/重试；
  - 工作区没有 venv 时 `run_shell` 回退到 `sys.executable`；显式 `set PATH=...`
    不替换；Docker 档不回退宿主路径；
  - 文档-only 任务收窄工具表：任务台账、子代理、技能发现、浏览器、
    notebook/REPL、计划模式、report_findings 等 18 个入口不装配；
    工具说明 22,467→11,192 字符，工具数 34→16；
  - 文档-only 任务追加执行纪律：只读目标文档与直接引用源码，不扫全仓、
    不写临时校验脚本、除非明确要求不查 git/浏览器/任务台账，最后跑一次 run_tests。
- 明确未实现：
  - 未做人工界面验收；
  - 文档-only 依赖显式边界，未覆盖“只写文档但没写不改源码”的任务；
  - 未改 UI、未改 B5 报告原始数据；
  - 未跑完整后端 5000+，本轮跑了 161 条相关回归。

## 修改内容

- `services/api/src/blank_api/services/tools/shell.py`：
  - `_command_segments` 按 `&&`/`||`/`|`/`;` 分段、引号内不切；
  - `normalize_python_command` 按段替换；显式 `PATH=` 尊重用户选择；
  - `_run_shell` 在无 venv、非 Docker 时用 `sys.executable` 兜底。
- `services/api/src/blank_api/services/agent/directives.py`：
  - `documentation_only(goal, acceptance)`；
  - `DOC_ONLY_INSTRUCTION`。
- `services/api/src/blank_api/services/tools/registry.py`：
  - `DOCS_ONLY_EXCLUDED`（18 个低频入口）。
- `services/api/src/blank_api/services/agent/runtime.py`：
  - 文档-only 时排除工具并落 `tools.excluded`；
  - 把 `DOC_ONLY_INSTRUCTION` 追加到本任务协作规则。
- 测试：
  - `tests/test_legacy_fixes_v162.py`：链式命令、引号分隔符、管道、`2>&1`、
    显式 PATH、无 venv 回退，共 6 条新用例；
  - `tests/test_directives_v496.py`：文档-only 正反例；
  - `tests/test_tool_budget_v427.py`：文档-only 收窄后核心工具仍在、meta 不在。
- `docs/t25-cost-2026-11-22.md`（新增）：前后数据、evidence、边界。
- `docs/capability-benchmark-2026-11-22.md`：补一条 B6 后续说明，不回填旧数据。

## 验证结果

| 检查 | 结果 | 摘要 |
|---|---|---|
| 相关后端回归（指令/工具预算/解释器/run_tests/工具面/任务工具/监督者/纯聊天） | **161 passed** | 含新加的 6 条解释器与 1 条文档-only 用例 |
| 后端全量 `pytest -q` | **5033 passed / 28 skipped** | 18:44，退出码 0；B6 改动后全绿 |
| T25 第一次（文档纪律版） | **156K / 10 轮 / 23s / 100 分** | 隐藏验收通过，只改 README |
| T25 第二次（稳定性） | **136K / 9 轮 / 16s / 100 分** | 与 Claude 138K 基本持平，轮次更少 |
| T01 普通编程回归 | **155K / 8 轮 / 13s / 100 分** | 与 B5 的 154K/8 轮一致，文档收窄未误伤 |
| 四任务最终复跑 | **ours 98.7 / Claude 94.4；双方 4/4** | Claude 自证下限；补回约 99.4；ours 166K vs 154K，T25 135K vs 178K；当时旧版 Codex 不可用 |
| 首次复现 evidence | 17 次调用、prompt 425K、工具说明 22,467 字符 | 9009 + where 重试是主要浪费之一 |
| 最终 evidence | 9 次调用、prompt 134.8K、工具说明 11,192 字符 | 8 个工具调用 |

原始结果：
- `evals/results/compare-2026-11-22T11-28-36-284Z.json`（156K）
- `evals/results/compare-2026-11-22T11-31-01-050Z.json`（136K）
- `data/t25-evidence.json`、`data/t25-evidence-6.json`

## 风险与需要审查的点

- `documentation_only` 是规则识别：只在“README/文档 + 不改源码/只改文档/
  验收未修改 src”时生效；没写边界就仍走全量工具表（不省，但不误伤）。
- 文档任务若实际需要浏览器/git，这些工具会被排除；这类任务不应被标成
  “只改文档、不改源码”。排除是任务级、`tool_search` 也拉不回。
- 系统提示新增一句“不查 git/浏览器/任务台账”，若用户在同一任务里明确要求
  查，仍以用户当前指令为准；但模型可能先遵守纪律再被纠正，需真机复核。
- `sys.executable` 兜底只用于宿主/本地沙箱；Docker 档已显式跳过。若未来
  增加远程执行后端，需要重新检查。
- T25 两次 156K/136K 有模型波动；成本改善是确定的，但不能把 136K 当成
  所有文档任务的固定值。
- 四任务最终复跑已通过（ours 98.7 / Claude 94.4），后端全量
  **5033 passed / 28 skipped** 也已通过。
- B6 四任务运行时 Codex 仍是旧 0.154 + nex 配置（404/402）；之后 20:12 已更新到
  v0.171.0 + spark-x2.6。**交互式 TUI 正常**（node_repl 读文件/校验），但
  `codex exec` 自动化 T01 在 workspace-write / danger-full-access 下都 300s
  超时，apply_patch 写入 `…` 未加引号导致 SyntaxError；这是自动化入口/终端
  链路问题，不是 Codex 整体不可用。编码机器对标暂缺，TUI 可用于人工审查。

## 建议人工验收

1. 读 `docs/t25-cost-2026-11-22.md`，核对 399K→136–156K 与两次工具序列。
2. 在界面选一个目录，发“在 README 增加三个函数的中文用法示例，不改源码”：
   确认只读目标文档/源码、不写临时脚本、最后跑一次测试。
3. 发一个**普通编程任务**（如 T01），确认全量工具表没有被误收窄。
4. 发 `set "PATH=..." && python ...` 或 `.venv/Scripts/python.exe ...`，
   确认用户显式解释器不被改写；`set "PYTHONPATH=..." && python ...` 会被换成
   可用解释器。
5. Codex v0.171 + spark 目前只建议做只读审查；编码任务先解决 Windows
   apply_patch 链路，再重跑 T01 对照。
