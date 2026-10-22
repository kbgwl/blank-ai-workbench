# 项目 harness：可版本化的 Agent 工作约定与离线优化

这份文档对应 [google-research/harness2](https://github.com/google-research/harness2) 里
可以落进本项目的部分。结论先说：**没有把它的代码搬进来，也没有加依赖**——那套实现绑在
POSIX 文件锁和它的评测脚本上，直接嵌进来只会多一层不能用的壳。这里按同样的思路重写成了
本项目的原生模块，并接在真实的指令层、评测适配器和 CLI 上。

代码位置：`services/api/src/blank_api/services/harness/`。

在这套骨架上又补了自改进需要的四件事：**从真实失败轨迹反思**（不是让模型凭空再猜一版）、
**回归与成本门禁**（不能为了主任务涨分而悄悄弄坏旧任务或把 token 翻倍）、**ACE 式经验反馈
计数**（被验证有用的条目优先进入记忆）、**失败挖掘**（未通过的实测记录只作反例，不冒充已采纳
harness）。四者都只作用于 harness 文本和经验库，不碰安全策略。

## 它解决什么问题

系统提示、项目约定、经验、技能这些「模型每次都要读的东西」通常散落在代码里，改一句要发一次版，
也说不清改动到底有没有让任务变好。harness2 的做法是把这部分抽成一个**项目内可版本化的目录**
（c1–c7），再配一条「提出候选 → 实测 → 择优采纳」的离线回路。

我们保留了这个核心，并加了两条本项目必须有的边界：

1. 运行时的 harness 只从 `<workspace>/.blank/harness/` 读取；采纳结果只写评测用的经验库目录。
   这条链路上没有代码去改 `safety.py` / `permissions.py` / `sandbox/policy.py`。
2. 采纳必须基于实测：基线永远一起跑，候选在工作区里改自己的 harness 直接判无效，
   最终写进经验库的树必须与探测时逐字节一致。

## 七个组件面（c1–c7）

全部位于 `<workspace>/.blank/harness/`：

| 组件 | 路径 | 上限 | 进入提示的方式 |
|---|---|---|---|
| c1 工作方式 | `systemprompt.md` | 4,000 字符 | 全文 |
| c2 硬性约束 | `guardrails.md` | 2,000 字符 | 全文 |
| c3 长期记忆 | `LongTermMEMORY.md` | 2,000 字符 | 全文 |
| c4 工具使用 | `tool_descriptions/tool_guidance.md` | 4,000 字符 | 全文 |
| c5 子代理 | `sub_agents/<name>/AGENT.md` | 3,000 字符 | 只列名字、description 与路径 |
| c6 技能 | `skills/<name>/SKILL.md` | 6,000 字符 | 只列名字、description 与路径 |
| c6 技能脚本 | `skills/<name>/scripts/*.py` | 20,000 字符 | 只列路径 |
| c7 脚本 | `scripts/<name>.py` | 40,000 字符 | 只列路径 |

always-on（c1–c4）总计不超过 **6,000 字符**。原因很直接：这段文本模型每一步都要付一次 token，
长文应该留在磁盘上，等 agent 真需要时用 `read_file` 读。c5–c7 因此只进索引，不内联正文。

harness2 的 c8（插件挂载点）**没有实现**：我们的运行时没有可执行插件的挂载点，收一个
`.ts` 文件只会让人误以为它会被执行。

### 校验与泄题防护

`spec.validate()` 在 CLI 与优化器采纳前执行，一次返回所有问题：

- 路径必须是 harness 内的相对路径；绝对路径、`..`、符号链接都拒绝。
- 每个组件的字符上限、always-on 总预算、至少一个 always-on 组件。
- `.py` 文件必须能被 `ast.parse` 解析。
- 禁止出现评测材料标记：`task-samples.json`、`evals/results`、`evals/workspaces`、
  `expect_files`、`n_passed`、`n_criteria`、`pass_rate`、`all_pass`、`verify_ok`、
  `deterministic_eval`，以及 `T01` 这种任务编号。把答案写进 harness 等于开卷考试，
  宁可拒绝一次合法编辑。

运行时读取是**宽松**的：非 UTF-8、读取失败或树整体不可解析，只让这一层缺席，
不会让任务起不来（尺寸与泄题约束由采纳入口负责，运行时不再逐条校验）。

## 运行时的层序

指令按「越靠近当前任务优先级越高」叠加：

```
system  内置 SYSTEM_PROMPT（文件永远改不了）
user    ~/.blank/AGENTS.md
project <workspace>/AGENTS.md、<workspace>/.blank/AGENTS.md
harness <workspace>/.blank/harness/（c1–c7）
task    本次任务附带的技能与指令
```

冲突时按 **任务 > harness > 项目 > 用户** 取舍。任何一层都不能放宽安全规则、审批规则或验收条件，
也不能要求隐藏过程或伪造结果。真正的强制力仍在工具网关、权限策略与沙箱里；harness 只是提示词，
模型即使被诱导，越界调用照样会被拒绝或要求审批。

`render()` 会把 c1–c4 全文、c5–c7 索引和一段安全前言压成一个有界文本，再由
`instructions.compose()` 统一做总预算裁剪（弱层先掉）。相关测试在
`tests/test_instructions_layers.py`、`tests/test_harness_spec.py`。

## 经验库

采纳结果写到 `--library` 指定的目录（默认 `_verify/harness_library`），按领域隔离：

```text
<root>/<domain>/INDEX.md
<root>/<domain>/harnesses/<task>/EXPERIENCE.md      已采纳的胜出树
<root>/<domain>/harnesses/<task>/harness/<c1-c7 文件>
<root>/<domain>/failures/<task>/EXPERIENCE.md       实测失败，只作反例
<root>/<domain>/harnesses|failures/<task>/FEEDBACK.json   ACE 计数 sidecar
```

一条经验必须同时有 `cue`、`lesson`、`evidence` 才允许写入；只有分数没有原因的记录会被拒绝。
保存完整树而不是摘要，是因为「教训」离开它对应的文件就只是一句观点。领域名与任务 id 会做
Windows 文件名净化（`/` → `__`，非法字符 → `_`），不会因为标签里有冒号就写不进去。

条目还带一组 ACE 式元数据：`scope`（适用范围）、`helpful` / `harmful`（后续实测的计数）、
`last_verified`。`record_feedback()` 把计数写进 `FEEDBACK.json` 而不是改写人类可读的
`EXPERIENCE.md`；`lessons()` 读回后，`RuleProposer` 按 `helpful - harmful`、再按 confidence
排序，被验证有用的条目先进入 always-on 记忆。计数不会隐藏负面记录。

**失败挖掘**与采纳分开存储：`optimize()` 只把「基线仍未解决」的主任务写进 `failures/`，
带真实的验证输出/轨迹摘录。它们没有 `harness/` 树，永远不会被当成可复制的已采纳工作方式；
在提案里只以「失败教训（仅作警示）」出现，不混进已验证规则。`failure_lessons()` 也把
`outcome` 标成 `failed`，避免调用方混淆。

## 五项判分

与 `evals/compare.mjs` 同一套口径（Python 版在 `harness/score.py`，已用 160 组随机输入
与 JS 实现逐组对过）：

| 分项 | 权重 | 判定 |
|---|---|---|
| 正确性 | 45% | 验证命令通过 **且** 至少改动一个文件 |
| 命中率 | 20% | `expect_files` 中实际被改动的比例 |
| 最小改动 | 15% | 期望外改动 0 个 → 1；1 个 → 0.6；2 个 → 0.3；≥3 个 → 0 |
| 自证 | 10% | agent 自己跑过验证（`run_tests` 成功或命令行匹配） |
| 成本 | 10% | 同一题内 `最省 token / 本次 token`，上限 1 |

正确性里的「且至少改动一个文件」不能去掉：很多 fixture 的基线本来就是绿的，
只验证通过会让「什么都不做」拿高分。

## Propose–Probe–Reflect–Compose

入口是 `optimize()`，CLI 里的 `run` 子命令就是它的外壳。

1. **Propose**：`RuleProposer` 从起始树 + 领域经验产出若干棵**完整**候选树
   （domain / guided / workflow / guarded 变体）；可选 `ChatProposer` 让模型出 JSON，
   解析或校验失败时回退到规则。候选不是补丁片段，避免「合并没有测过的片段」。
2. **Probe**：同一批样例、同一个模型，对基线（当前起始树）和每个候选各跑一遍，
   每次都从 fixture 重建工作区；有回归样例时每条候选也跑回归集。
3. **Reflect**（可选，`--reflect`）：首轮探测后，只把**仍然没通过的主任务**交给
   `ReflectionProposer`。它拿到的是真实 `verify` 输出和工具轨迹（已脱敏），要求每条改动
   引用具体证据；返回的仍是完整树（只给改动文件时与起始树合并），来源标为 `reflection`，
   并重新走一遍探测。没有未通过的主任务就跳过，不为了“多问一次模型”而多花 token。
4. **Compose**：候选必须先过正确性守门（每道主任务 `verify` 通过且至少改一个文件），
   再过三道采纳门禁，最后选主任务均分最高、token 更少的那棵：

| 门禁 | 默认 | 作用 |
|---|---|---|
| 最小增益 `--min-gain` | 0.5 分 | 候选主任务均分必须比基线高出这个值 |
| 回归掉分 `--max-regression-drop` | 0.0 | 旧任务均分允许的最大下降；默认不允许任何下降 |
| token 增长 `--max-token-growth` | 1.0 | 候选 token 不得超过基线的 2 倍 |

此外，报告会维护一条 **帕累托前沿**：按每道主任务的分数向量比较，保留互不支配的候选
（包括最终没被采纳的）。它用于回答「这棵是不是用 A 题换 B 题」；当前只作报告证据，
不改变“先过门禁再选最高均分”的采纳规则。

三个防止自欺的细节：

- **基线永远跑**。没有实测基线，就没有「更好」；增益与回归掉分都以它为参照。
- **运行中改 `.blank/harness` 的候选直接出局**。agent 若能在测量途中改写自己的指令，
  分数就不属于安装的那棵树。适配器把 `.blank/harness` 从改动快照里排除，并单独比对内容。
- **预算**：`--max-tokens`、`--max-seconds` 到顶就停，报告中标记 `budget_exhausted`。

反思证据在进入提示前会脱敏：评测材料标记替换为「评测材料」，`T01` 这类任务编号替换为
「同类任务」；模型返回的候选再走一遍 `spec.validate()`，含泄题标记或越权内容的整棵丢弃。
反思失败、返回重复候选或预算耗尽都不会推翻已经测过的候选。

## 评测适配器

`BlankEvalAdapter` 走真实 HTTP API：建项目 → 绑工作区 → 建任务 → `POST /tasks/{id}/start`
（非阻塞）→ 轮询 evidence → 自动批准 `waiting_approval` → 任务终态后**自己**跑一遍
`verify` 命令。

要点：

- 验证命令由适配器在 agent 退出后执行，模型自报的「测试通过」不算证据。
- 超时先 `cancel`，再给 10 秒收尾，确认任务不再写文件后才验证，避免测到半成品。
- 工作区逐字节快照，忽略 `.git`、`node_modules`、`__pycache__` 等目录和大于 2 MB 的文件。
- 有 `--cleanup` 时才删除服务端项目记录；`--keep-workspace` 保留现场用于排查。
- 自动批准只适用于一次性评测工作区。**不要**拿它跑用户真实项目。

## CLI

命令从**仓库根目录**运行，这样 `evals/task-samples.json` 和 `_verify/` 默认路径才有意义：

```bash
# 校验当前工作区的 harness（uv 管依赖）
uv run --project services/api python -m blank_api.services.harness.cli validate --workspace E:/path/to/repo

# 看域经验库（默认 _verify/harness_library）
uv run --project services/api python -m blank_api.services.harness.cli library --domain 解析器

# 跑样例：先起后端，并准备一个真实供应商
uv run --project services/api python -m blank_api.services.harness.cli run \
  --api http://127.0.0.1:8000/api/v1 --provider <provider-id> \
  --only T01,T02 --k 3 --report _verify/harness_report.json

# 主任务 + 回归集 + 按真实失败轨迹反思一次
uv run --project services/api python -m blank_api.services.harness.cli run \
  --api http://127.0.0.1:8000/api/v1 --provider <provider-id> \
  --only T01 --regression-category regression --reflect \
  --max-regression-drop 0 --max-token-growth 0.5 \
  --report _verify/harness_report.json
```

本机 Git Bash 里 `uv` 不在 PATH 时，用项目 venv，但仍从仓库根目录执行：

```bash
PYTHONPATH=services/api/src services/api/.venv/Scripts/python.exe \
  -m blank_api.services.harness.cli validate --workspace E:/path/to/repo
```

`run` 默认读 `evals/task-samples.json`，将其中的 `fixture` 名解析成真实文件表。
可选 `--base <已用 harness 目录>` 从现有树出发；`--no-adopt` 只测不写库；
`--model-proposer` 需要同时给 `--provider`，模型不可用时回退到规则候选。
`--python` 默认当前解释器，用来替换验证命令开头的 `python`；`--node` 同理。

回归集与反思：

- `--regression-samples <文件>` 默认与 `--samples` 相同；`--regression-only T03,T04` 或
  `--regression-category <类>` 从中挑旧任务。与主样例重复的 id 会被剔除，避免同一题
  既算增益又算回归、把门禁稀释掉。
- 不给回归参数就不跑回归门禁；一旦给了，回归掉分默认 0 容忍。
- `--max-token-growth 0.5` 表示候选 token 不得超过基线的 1.5 倍；默认 1.0（即 2 倍）。
- `--reflect` 需要同时给 `--provider`（用同一个供应商端点做反思，不另配模型）；
  `--reflect-rounds` 默认 1，每轮只针对仍未通过的主任务。反思失败不会中止整轮优化。

## 边界与已知限制

- **没有搬 harness2 的代码**：它的 POSIX 文件锁、外部评测脚本和目录假设与本项目不兼容；
  原生实现保留了组件面、校验、判分和优化回路。
- **没有 c8**：没有插件执行挂载点，不想制造「文件存在但不会执行」的假象。
- **harness 不是安全边界**。它只影响提示词；真正的拦截在工具网关、权限与沙箱。
- **反思需要真实供应商**。`--reflect` 走 `--provider` 的 `/chat` 端点；本机的自动化验证
  用的是脚本化 chat 函数，证明的是“证据进入提示、候选被重新测量、门禁按预期判定”，
  不是模型反思质量。没有真实模型时，规则提案与实测门禁仍然完整可用。
- **门禁默认保守**：回归掉分默认 0 容忍、token 默认最多 2 倍。任务本身波动大时，这两个
  默认值会把一些“总体更好但有噪声”的候选挡在门外；确认可以接受某种权衡后，再用
  `--max-regression-drop` / `--max-token-growth` 显式放宽。
- **失败记录不是规则**。`failures/` 只作反例注入，不参与正确性判定，也不会被复制成
  harness。它回答的是“上次为什么没成”，不是“这次照做就能成”。
- **帕累托前沿只报告**。它保留互不支配的候选，方便人工看权衡；自动采纳仍以“过门禁后
  主任务均分最高”为准，不会因为进了前沿就绕过回归/成本门禁。
- **效果尚未在真实模型上大规模验证**。本机已经接上真实供应商（DeepSeek）并跑通了评测样例集与工作台对话，
  但 harness 的 propose/run 回路至今只在脚本化模型上端到端验证过，因此这里能证明链路、判分与 `verify`
  是真的，不能证明真实模型上的收益。
  要得到产品级结论，用 `run` 配真实 `--provider` 跑满样例，并按 `docs/QUALITY.md`
  的口径读分。
- **Windows**：验证命令由适配器做 `python`/`node` 路径替换；工作区清理有重试，
  杀不干净会进 `warnings` 而不是让整轮评测崩掉。

## 验证记录

- 后端全量测试通过：659 passed / 14 skipped；其中 harness 相关 83 个用例，覆盖反思提案、
  回归/成本门禁、帕累托、失败库、反馈计数与 CLI 参数。
- 五项判分与 `evals/compare.mjs` 在 160 组输入上结果一致。
- 进程内端到端探针 `scripts/harness_e2e_probe.py`（脚本化模型，真实 app / LangGraph /
  工具网关 / evidence）：任务成功，`done.txt` 写入，`run_validation` 节点跑通 pytest，
  适配器独立 `verify` 通过，harness 未被改动，c1 标记出现在真实系统提示中。
- 优化回路探针 `scripts/harness_optimize_probe.py`（真实工作区、真实 `verify` 子进程、
  真实经验库落盘）：破坏回归集的候选被门禁拒绝，安全候选被采纳；基线失败写进
  `failures/`；反思提案收到真实的 `CHECK-FAILED` 输出、重新测量后被采纳；
  `record_feedback()` 的计数能被读回并进入 always-on 记忆。
- 两个反证同时跑过：不安装 harness 时标记进不了系统提示；只声称完成、不写文件时
  适配器 `verify` 判失败、改动为空。说明上述断言不是恒真。

```bash
# 仓库根目录
uv run --project services/api python scripts/harness_e2e_probe.py
PROBE_MODE=no-harness uv run --project services/api python scripts/harness_e2e_probe.py
PROBE_MODE=no-write   uv run --project services/api python scripts/harness_e2e_probe.py
uv run --project services/api python scripts/harness_optimize_probe.py

# 本机 Git Bash 里 uv 不在 PATH 时，用项目 venv（在 services/api 下）
PYTHONPATH=src .venv/Scripts/python.exe ../../scripts/harness_e2e_probe.py
PYTHONPATH=src .venv/Scripts/python.exe ../../scripts/harness_optimize_probe.py
```
