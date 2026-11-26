# E1 执行环境与目标状态修复 开发交接单

## 任务与范围
- 已实现：
  - `py -3` / `py -0p` / `py -V` 等启动器写法不再被改写成普通解释器；
    `py -m 模块` 仍换成项目 venv 解释器。
  - 解释器没有 pip 时，命令回执直接说明「换系统解释器（py -3）或用标准库」，
    不再让模型反复换写法重试装包。
  - cmd 里误用 `| tail` / `| head` / `grep` / `sed` / `awk` 时，回执给出重定向 +
    read_file 的替代做法。
  - run_shell 工具说明按真实 shell 生成：cmd 下不再教模型「用 `;` 串联」，
    也不再写「先 ls」；改为 `&&` / `dir`，明确 `;` 不是分隔符。
  - 审批/提问恢复后，目标状态从 `waiting_approval` 拉回 `executing`，
    状态条不再在任务已经继续跑时显示「等你批准」。
  - 编程场景提示新增：交付文档默认 Markdown；不要为写文档反复安装第三方包，
    装依赖失败两次就停，改用无依赖方案或把缺口写进交付。
- 明确未实现：
  - 没有加新的断路器阈值；仍沿用 `stall.py` 已有的同签名/同类失败判定。
  - 未改动 `pip` 是否存在、未给运行时补装 pip；仍建议用 `py -3` 或项目 venv。

## 修改内容
- `services/api/src/blank_api/services/tools/shell.py`：
  - `_BARE_PYTHON` 移除 `py`；`fix()` 只对 `py -m` 替换，其余启动器写法原样保留。
  - 新增 `pip_missing_hint` / `posix_text_tool_hint`，并入失败回执与 `data.hint`。
  - `shell_tools()` 的「多条命令」「列目录」说明按 `shell_dialect()` 生成：
    cmd 下禁止 `;` 串联、用 `dir` 确认目录。
- `services/api/src/blank_api/services/agent/runtime.py`：
  - 新增 `_revive_goal()`，在 `_resume` 置 running 后调用；只改 `waiting_approval`。
- `services/api/src/blank_api/services/agent/scenarios.py`：
  - 编程场景提示新增文档格式与依赖安装纪律两条。
- 测试：
  - `tests/test_legacy_fixes_v162.py`：`py -3` / `py -0p` 保持原样，`py -m` 仍替换。
  - `tests/test_shell_launcher_hints_v411.py`：启动器 + 两类提示的回归用例。
  - `tests/test_goal_revive_v411.py`：恢复回写目标状态，不动其他状态。

## 验证结果
| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_legacy_fixes_v162.py tests/test_shell_launcher_hints_v411.py tests/test_shell_dialect.py tests/test_shell_decode_v149.py tests/test_cmd_semicolon_v410.py -q` | 53 passed | shell 相关全绿 |
| `pytest tests/test_scenarios_v153.py tests/test_two_scenes_v401.py tests/test_doing_tasks_prompt_v167.py tests/test_prompt_parts_v427.py tests/test_output_styles_v47.py -q` | 75 passed | 场景与提示词全绿 |
| `pytest tests/test_goal_revive_v411.py -q` | 2 passed | 目标回写 |
| 前端 `node --test ... "src/**/*.test.ts" scripts/csp.test.mjs` | 473 passed / 0 failed | 含 4 条 5xx 文案与权限文案用例 |
| 前端 `tsc --noEmit` / `eslint src --max-warnings=0` | 退出码 0 | 类型与风格 |

真实命令复验（同一套 `run_process` / `normalize_python_command`）：
- `py -3 -c "import sys; print(sys.executable)"` → **退出码 0**，
  输出 `C:\Users\22103\AppData\Local\Programs\Python\Python312\python.exe`；
  `py -3 -m pip --version` → pip 25.0.1（提示里的替代路径真实可用）。
- `py -m pytest` → 仍替换为 `services/api/.venv/Scripts/python.exe -m pytest`。
- `<venv python> -m pip install python-docx` → 退出码 1（No module named pip），
  `pip_missing_hint` 正常触发。
- 后端已用 `services/api/.venv` 重启，`/api/v1/health` 200；
  首页浏览器探针 console 0、无失败请求。

## 补记：完全访问为什么仍会问 + 代理 500 的文案

- 证据：任务 8f2f7734 在 09:11:00 只有一张审批卡，命令是
  `python -m pip install python-docx ... | tail -5; python -c "import docx..."`，
  `risk_level=high`——同时命中「安装依赖」与「内联代码」两条高风险规则。
  完全访问只免低/中风险，高风险（装依赖、内联脚本、删除、推送、部署、迁移、
  工作区外、联网）永远问一次；批准后本会话同类不再逐条问（后续 pip 调用未再弹卡）。
- 发送栏常驻提示补上「首次装依赖、内联脚本（如 python -c）」，与选项说明一致。
- 500 文案：后端自己的 500 一定带 JSON（`unexpected server error`）；用户看到的
  「请求失败（HTTP 500）」是无结构化错误体的兜底文案，对应开发态 Next rewrite
  在上游不可达时返回的 HTML 500（后端重启窗口）。现在这类 5xx 显示
  「后端暂时不可用（HTTP 500），可能正在重启」，并附请求方法与路径。

## 风险与需要审查的点
- 只有 `py -m 模块` 会被替换；`py -3 -m pytest` 保持系统解释器，若项目 pytest
  只装在 venv 里会跑不了——这是为了避免再次产生假的「Unknown option: -3」。
- 提示词改动从下一轮对话生效，历史任务不受影响。
- 目标回写只处理 `waiting_approval → executing`，不覆盖其他状态。
- 后端已重启一次；已确认没有进行中的任务，老数据里两条 11-19 的 running 为孤儿。

## 建议人工验收
1. 新开一轮「写个项目计划书」，确认不再出现连串的 pip/解释器失败。
2. 再触发一次「等你回答/批准 → 回答」的流程，确认状态条及时回到「执行中」。
3. 让 Agent 跑一条 `py -3 -c "print(1)"`（或观察它的探测命令），确认不再报 `Unknown option: -3`。
