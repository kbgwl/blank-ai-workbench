# E2 开局环境预检 开发交接单

## 任务与范围
- 已实现：
  - 任务开局探测一次本机能力：python / `py -3`（含 pip 版本）/ 工作区 `.venv`
    （含 pip）/ node / git / uv；同一探针顺带读 python-docx / openpyxl / python-pptx
    是否可用。结果缓存 5 分钟（按工作区 + PATH）。
  - 结果压成一小段「本机环境（开局实测）」注入系统提示，只报版本与存在性，
    不报主机路径；明确写「不要再用 where / --version 逐条探测」。
  - 预检失败只记 warning，任务照常开跑；纯聊天 / 无工具任务不探测。
  - 预检结果写一条 `environment.profiled` 事件（版本信息，可审计）。
- 明确未实现：
  - 不自动装包；没有 pip 时只给「别装、用标准库」的结论。
  - 没探测 npm/pnpm 版本（只影响前端脚手架类任务，后续再补）。

## 修改内容
- 新增 `services/api/src/blank_api/services/agent/env_profile.py`：
  `profile()` 探测 + 缓存、`profile_async()` 线程执行、`render_block()` 渲染。
- `services/api/src/blank_api/services/agent/graph.py`：
  `initial_messages(..., environment_block="")`，追加到 system 的场景段之后。
- `services/api/src/blank_api/services/agent/runtime.py`：
  开局按 `session.shell.env_for()` 探测并注入；写 `environment.profiled` 事件。
- 新增 `tests/test_env_profile_v412.py`（5 条）。

## 验证结果
| 命令 | 结果 | 摘要 |
|---|---|---|
| `pytest tests/test_env_profile_v412.py -q` | 5 passed | 探测/渲染/缓存/注入 |
| `pytest tests/test_env_profile_v412.py tests/test_goal_revive_v411.py tests/test_doing_tasks_prompt_v167.py tests/test_prompt_parts_v427.py tests/test_output_styles_v47.py -q` | 53 passed | 含真实 runtime 路径 |
| 本机实测 `profile()` | 833ms | py -3: 3.12.10 + pip 25.0.1；node 24.20.0；git 2.56.1；.venv 无 pip |

本机输出（节选）：

```
- 系统: Windows 11；shell: cmd.exe
- py -3: 3.12.10（pip 25.0.1 可用）
- 工作区 .venv: 3.12.14（没有 pip，不能用它装包）
- node: 24.20.0
- git: 2.56.1.windows.1
- 需要装包时，用上面标了「pip 可用」的解释器；装包失败两次就停…
- 以上是开局实测：不要再用 where / --version 逐条探测解释器。
```

后端已重启，`/api/v1/health` 200；首页浏览器探针 console 0。

## 补记：文档库一起探测

- 同一次探测里多读三个库：`python-docx` / `openpyxl` / `python-pptx`（每个解释器一次）。
- 提示里直接写「文档库已装: py -3: python-docx」或「文档库未安装；要写
  Word/Excel/PPT 时用标了 pip 可用的解释器装一次（首次需审批），否则用 Markdown/CSV」。
- 本机实测：py -3 有 pip 25.0.1、三个文档库都未安装；预检 702ms。
- 目的：把「要写 docx 到底要不要装包」从试错变成开局事实。

## 风险与需要审查的点
- 预检每个任务多约 0.8–1 秒（缓存命中后为 0）；探测进程 3 秒封顶。
- 版本信息会进模型上下文（不含路径）；若供应商审计敏感可关闭（后续可加设置开关）。
- 预检结果 5 分钟内可能过期（用户中途装/卸工具）；提示里限定「开局实测」。
- `python` 不可用而 `py -3` 可用时，提示会同时出现「python 未找到」与「py -3 可用」，
  以实测为准，不再让模型去找。

## 建议人工验收
1. 新开一个有工作区的任务，问「这台机器有哪些解释器」，看它直接引用预检结论。
2. 观察时间线：不应再出现连串 `where python` / `python -m ensurepip` 探测。
