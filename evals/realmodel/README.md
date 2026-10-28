# 真模型点验装置（#116–#119，装置本身 #120 进仓库、#121 改成一格一实例）

把上一批登记的那条欠账闭掉：真模型点验原来活在 `E:/tmp/realmodel/v119_*.py` 里，
换台机器、换个人就复现不了。现在判据、夹具、钩子、启动器都在仓库里，
**凭据仍然不在**——实例的数据目录、settings.json、secret.key 由环境变量指过去，
仓库一个字节都不收（`test_装置目录里不许有凭据或密钥文件` 扫整个目录）。

## 文件

| 文件 | 职责 |
| --- | --- |
| `async_hooks_v119.py` | 驱动：铺工作区、起任务、取事件、汇总三格读数、定退出码 |
| `prompt_submit_v123.py` | #123 那一层的驱动：提交钩子的三道决定各占一格，四格 + 对照格，同刻并跑 |
| `team_submit_v123.py` | #123 团队入口：#131 补的第二条入口。四格 `--mode suppress|ask|multi|budget`：抑制 / ask 拒绝 / 多成员并收尾 / 预算截断，成员任务的 goal 也过提交钩子；`--control` 走无钩子那台，红集合按模式分派且必须精确相等 |
| `judgments.py` | 判据本体，全是「事件流进、判定出」的纯函数，可离线喂实物自检 |
| `lib.py` | 配置、HTTP 客户端、判定表、退出码纪律、取数小工具 |
| `launch-instance.sh` | 起隔离实例（默认 :8061）；`--with-hooks` 是钩子姿态，后面再跟 `--with-submit-hook` 才挂提交钩子，不加是对照姿态。起之前必须过 `hooks_config.py --check`，不过就不起；同时把姿态写进 `instance-posture-<port>.json`（提交钩子在留痕里是**单独一个键** `submit_hook`） |
| `hooks_config.py` | `BLANK_HOOKS` 的唯一来源与自检（`--print` 生成、`--check` 校验）。上一版这里是启动器用 `printf` 手拼的，引号没转义——见「姿态」一节 |
| `hooks/*.py` | 四种钩子夹具：`post_tool_use`（异步、退出码 2、故意写 GBK 字节）、`worktree_create`、`worktree_remove`、`prompt_submit`（#123：同一枚夹具按 goal 里的锚给四种决定——拦下 / 抑制 / 补上下文 / 什么都不做） |
| `fixtures/*.json` | 从实例原样取回的实物：修复前的负基线、编码格绿基线、**姿态修好后那轮的主格绿基线**、对照格、红集合登记表、变异电池登记；`*-v121` 是双实例同刻并跑那一轮（两台各有各的 base）；`prompt-submit-v123.json` 是**夹具与判据共读的那一份规格**（锚、正文、原因只写一处），`prompt-submit-v123-green.json` 是 #123 那轮五格实物，`team-submit-v123-green.json` 是团队入口**四格 × 主/对照**的实物（每格的任务行、群消息与轮，`provenance` 记 rc / 实测红集合 / 工件路径） |
| `fixtures/wake-text.txt` | 唤醒正文的唯一来源（45 字），钩子和判据都读它，不许两处手抄 |
| `mutation_battery.py` | 把装置本身改坏 25 种，看离线自检是否每条都红；期望按实测登记 |
| `../services/api/tests/test_realmodel_device_v120.py` | 装置离线自检（70 条，不花钱） |
| `../services/api/tests/test_realmodel_instances_v121.py` | 一格一实例那一层的自检（26 条，含双实例并跑实物的离线重放） |
| `../services/api/tests/test_prompt_submit_device_v123.py` | #123 那一层的自检（80 条）：五格实物重放、对照格三条该红的逐字红、25 条变异电池、团队入口四格 × 主/对照实物、23 条团队损伤变异、夹具过产品解析器、姿态闸门 |

## 三格

- **主格**（带钩子，两轮 + 一次回退）：#116 后台句柄链、#117 每轮回退点、
  #118 钩子副本与清理、#119 唤醒投递。16 条判据。
- **编码格**（带钩子，一轮）：唤醒正文逐字不许变形。#119 那个 GBK 缺陷就红在这里，
  修复前实测读成 39（45 字被静默吃掉 6 个），这条判据现在钉的是「等于原文长度」。
- **对照格**（**不配钩子**）：同一套判据打在没有 `worktree_create` 钩子的实例上，
  期望 `mode=hook` 那条**必须红**。它不红 = 主判据在任何一次运行里都成立 = 什么都没测到，
  整次点验作废（rc 2，不是 rc 0）。

## 一格一实例（#121）

上一批装置只认识一台实例：`BLANK_REALMODEL_BASE` 一个值管三格。后果写在 #120 文档里——
对照格只能**复用**早前无钩子那一次的读数，「两种姿态各起一台、同刻并跑」这句话没法执行，
因为闸门只会把三格同跑一律拦死。

现在「格子」与「实例」解绑，名字只登记一处（`lib.CELLS`）：

| 格 | 任务 id 变量 | 实例 base | 数据目录 home |
| --- | --- | --- | --- |
| 主格 | `RM_TASK_MAIN` | `RM_BASE_MAIN` | `RM_HOME_MAIN` |
| 编码格 | `RM_TASK_ENC` | `RM_BASE_ENC` | `RM_HOME_ENC` |
| 对照格 | `RM_TASK_CTL` | `RM_BASE_CTL` | `RM_HOME_CTL` |

不给就沿用 `BLANK_REALMODEL_BASE` / `BLANK_REALMODEL_HOME` 那一台。姿态闸门
（`lib.premise_error`）改为按**每格自己那台**的留痕核对，并把要真跑的格按 base 分组：

- 同一台既要领钩子格（主格/编码格）又要领对照格 → rc 2，且文案点名
  「用 `RM_BASE_CTL` 起第二台」，不许退化成「今天没法并跑」；
- 一台实例的留痕读不到 `hooks` 却要跑主格/编码格 → rc 2（钩子判据必红，白花钱）；
- 对照格那台留痕不在、或读到的不是 `no-hooks`（**包括坏 JSON**）→ rc 2。留痕坏了不能被
  当成「那大概没钩子」，这一条是 #121 写用例时才发现的真缺陷，电池 M23 钉住。

## 提交钩子那三道决定（#123，四格 + 对照格）

对标 `UserPromptSubmit` / `UserPromptExpansion`：用户在提交那一刻可能遇到三种事——被补了一段背景、
正文被换成别的、或者整条提交被拦下不让进模型。这一层用**同一枚夹具**（`hooks/prompt_submit.py`）
按 goal 里的锚给四种决定，一格一样，全在一台带 `--with-submit-hook` 的实例上跑：

| 格 | goal 里的锚 | 夹具的决定 | 判据钉住什么（条数） |
| --- | --- | --- | --- |
| 抑制格 | `紫椒八十一号` | `suppressOriginalPrompt: true` + 替代正文 | 事件带 `suppressed=true`、原文只留长度+12 位指纹、替代正文逐字等长、**账面 goal 仍是用户写的那份**、模型读的是替代正文、管家候选没被原文关键词带出来、**没有引发任何工具动作**（9） |
| 拦下格 | `绿锚不许进模型` | stderr 写原因 + 退出码 2 | 事件带原因逐字、只登记长度与指纹、**一次模型调用都没发起**（无 `model.usage`、无 `task.started`、`steps_used=0`）、任务以失败收场且原因指向钩子、不改账（5） |
| 补上下文格 | `绿杨二十三号` | 只给 `additionalContext` | `original_kept=true`、补充正文逐字等长、**不许冒出抑制事件**、正文不进事件、模型同时读到原文口令与补充里的编号、**没有引发任何工具动作**（6） |
| 无关格 | `白塔九号` | 什么都不做 | 钩子仍然跑过并留审计、不产生任何改写事件、原文照送（3） |
| 对照格 | 同抑制格那份 goal | **那台没挂提交钩子** | 三条必须红（下表） |

「抑制的是上下文，不是账」是这批最容易写歪的一条：`tasks.goal` 必须逐字等于用户写的那份，
被换掉的只是送进模型的那一节；所以抑制格里既断言事件里的 `original_chars/sha` 对得上账，
也断言账面 goal 没被改写。另外抑制时 `curate(goal="")`——上下文管家按关键词挑候选文件，
留着原文就等于给抑制留了个侧门，对照格里那条侧门是真的会漏的。

对照格那三条（`judgments.CONTROL_123_RED`，逐字）：

- `123 抑制格留下 prompt_substituted 且 suppressed=true` —— 没钩子就没这条事件；
- `123 模型读数里不含原文口令` —— 没抑制时模型照读原文，实测答 `紫椒八十一号`；
- `123 上下文管家的候选没被原文关键词带出来` —— 实测带出 `紫椒八十一号-清单.txt`。

三条不齐 = 主判据在「没有钩子」时也成立 = 它测的不是钩子路径，整次读数作废（`rc` 非 0）。
反过来，主格的红只从主格那台取——第一版驱动把对照格的红并进同一张判定表，
把「装置自己没跑提交钩子」误报成「产品缺陷」，这是 #123 真跑第一轮 rc=1 的根因。

## 姿态：花钱之前先钉死，两道闸

`HookRunner.from_env` 是 fail-closed 的——配置解析失败一律降级成「没有钩子」，**哪里都不报错**。
所以姿态错了不会有人告诉你，只会在三格跑完之后把 11 条钩子判据全判红。#120 第一次真跑就栽在这：
启动器用 `printf` 手拼 `BLANK_HOOKS`，引号没转义 → 产品读成没有钩子 → 钱花了，红的是姿态。

- **起实例时**：`launch-instance.sh --with-hooks` 先 `hooks_config.py --print` 生成、再 `--check` 自检
  （合法 JSON、三个事件齐、命令恰好两段、夹具文件真存在、async 只在 `post_tool_use`），
  自检不过直接 rc 2 不起服务。要挂提交钩子就再跟一个 `--with-submit-hook`：它只往配置里多
  `user_prompt_submit` 这一格，`--check` 跟着改口径——**没开这个开关却挂着 `user_prompt_submit`
  会被拒**（那台实例的姿态不是装置以为的那一份），开了开关却缺事件同样被拒。
  不认识的启动参数一律 `[ABORT]` + rc 2，不静默忽略。
- **花钱之前**：装置读**每格自己那台**的 `instance-posture-<port>.json`（`lib.premise_error`，
  分组与三条拦法见上一节）。要真跑主格/编码格而那一台姿态不是 `hooks` → rc 2 拒跑；
  对照格那台留痕不在或读不到 `no-hooks` → 同样 rc 2。**手起的实例不假定它带了钩子。**
  复用早前那几轮（`RM_TASK_MAIN` / `RM_TASK_ENC` / `RM_TASK_CTL`）不花钱，闸门不拦。
- #123 那四格只认留痕里的**那一个布尔**：`prompt_submit_v123.submit_posture_abort` 要求
  `submit_hook is True`。缺键、`None`、字符串 `"true"`、`False` 全部当「姿态没钉住」拒跑（rc 2）——
  是 `is True` 而不是「truthy 就行」，跟产品侧 `parse_hook_output` 只认真布尔
  `suppressOriginalPrompt` 是同一条纪律。
- 坏 JSON 那串（`BROKEN_RAW`）留在自检里当永久负基线：产品必须读成 `enabled is False`。

## 跑法

```bash
# 1a. 起带钩子的隔离实例（凭据在 BLANK_REALMODEL_HOME 里，仓库看不到）
BLANK_REALMODEL_HOME=E:/tmp/realmodel bash evals/realmodel/launch-instance.sh --with-hooks &

# 1b. 起第二台——无钩子姿态，给对照格用。第二台必须有自己的一份 run.db
#     （共用一份会互相写），但 secret.key 得是同一把（供应商行是它加密封存的）：
#     用 BLANK_REALMODEL_SEED_HOME 让启动器缺库时从主实例铺一份过来，只拷文件、不读内容。
BLANK_REALMODEL_HOME=E:/tmp/realmodel-ctl BLANK_REALMODEL_PORT=8065 \
  BLANK_REALMODEL_SEED_HOME=E:/tmp/realmodel bash evals/realmodel/launch-instance.sh &

# 2. 三格真跑（花真模型的钱）：主格+编码格在 :8061，对照格在 :8065
export BLANK_REALMODEL_PROVIDER=<provider id>
export BLANK_REALMODEL_HOME=E:/tmp/realmodel
export BLANK_REALMODEL_BASE=http://127.0.0.1:8061
export RM_BASE_CTL=http://127.0.0.1:8065 RM_HOME_CTL=E:/tmp/realmodel-ctl
services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/async_hooks_v119.py

# 已经跑过的那几轮按任务 id 复用事件重算，不再花钱：
RM_TASK_MAIN=<id> RM_TASK_ENC=<id> RM_TASK_CTL=<id> 同上
# 只跑某几格：RM_CELL=encoding,control 同上
```

这台机器上 :8063 被一个无关的 node 进程占着（`stub2.mjs`），所以上面的第二台用 :8065；
换端口只是 `BLANK_REALMODEL_PORT` 与 `RM_BASE_CTL` 两个值的事，姿态留痕文件名跟着端口走
（`instance-posture-<port>.json`）。

对照格要的是**没有钩子**的那台实例：要么这样同刻并跑，要么复用早前在无钩子姿态下跑过的
那一次（`RM_TASK_CTL`）。复用模式下回退格不重判——那一轮已经退过，重算会把「已回退」读成
缺陷，读数里会明写哪几条没判，还会逐格念 `[来源] … 复用 …（旧读数，不是此刻的实物）`。

### #123 那四格

主格那台要**额外**挂提交钩子（`--with-submit-hook`），对照格那台仍然什么钩子都不挂：

```bash
BLANK_REALMODEL_HOME=E:/tmp/realmodel bash evals/realmodel/launch-instance.sh --with-hooks --with-submit-hook &
BLANK_REALMODEL_HOME=E:/tmp/realmodel-ctl BLANK_REALMODEL_PORT=8065 \
  BLANK_REALMODEL_SEED_HOME=E:/tmp/realmodel bash evals/realmodel/launch-instance.sh &

export BLANK_REALMODEL_PROVIDER=<provider id>
export BLANK_REALMODEL_HOME=E:/tmp/realmodel
export BLANK_REALMODEL_BASE=http://127.0.0.1:8061
export RM_BASE_CTL=http://127.0.0.1:8065 RM_HOME_CTL=E:/tmp/realmodel-ctl
services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/prompt_submit_v123.py

# 跑过的那几轮按任务 id 复用，不再花钱：
RM123_TASK_SUPPRESS=<id> RM123_TASK_BLOCKED=<id> RM123_TASK_INJECTED=<id> \
RM123_TASK_PLAIN=<id> RM123_TASK_CTL=<id> 同上
```

四格共用一台（它们只差 goal 里的锚，不差实例配置），对照格必须是**另一台**——
提交钩子挂在谁身上，谁的留痕就得写 `submit_hook: true`，闸门读不到就 rc 2，不会替猜。
拦下格预算给 90 秒（它一次模型都不调，跑得久说明产品没拦住），其余四格 240 秒。

### #123 团队入口（#131、四格 #134–#137）

群聊的成员任务是**第二条入口**：`services/team.py` 把用户发言直接当成员任务的 goal，
所以提交钩子对它是真起作用的。`--mode` 选一格，主格与对照格各自跑同名的模式：

| 格 | 点名人数 / 发言上限 / 预算 | 判据钉住什么 |
| --- | --- | --- |
| `suppress` | 1 / 3 / 60k | 成员任务被改写、账面 goal 逐字不动、机器人答替代正文口令且不含原文（5 条） |
| `ask` | 1 / 3 / 60k | 每条成员任务 `decision=ask` 阻断、零模型调用、失败原因写明按拒绝处理、群里念得出拒绝原因（6 条） |
| `multi` | 2 / 5 / 60k | 上述 5 条 + 两位不同成员各一条任务、收尾轮恰一条、群轮 completed（3 条结构判据） |
| `budget` | 2 / 4 / 5k | 上述 5 条 + 真在预算线停下、不再给后续成员派任务、轮 completed、轮合计等于各条发言用量之和（4 条） |

```bash
export BLANK_REALMODEL_PROVIDER=<provider id>
RM_BASE_MAIN=http://127.0.0.1:8061 RM_HOME_MAIN=E:/tmp/realmodel \
  services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/team_submit_v123.py --mode ask
RM_BASE_MAIN=http://127.0.0.1:8065 RM_HOME_MAIN=E:/tmp/realmodel-ctl \
  services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/team_submit_v123.py --mode ask --control

# 跑过的这一轮按 id 复用，不再花钱（主格与对照各一份，变量名带模式）：
RM123_TEAM_ASK_PROJECT=<project id> RM123_TEAM_ASK_GROUP=<group id> ... 同上
RM123_TEAM_ASK_CTL_PROJECT=<project id> RM123_TEAM_ASK_CTL_GROUP=<group id> ... 同上
```

点名写进用户发言（`compose_goal`）：不点名时队列里只有主理人，多成员与收尾根本不会发生，
判据也就没有多份样本。收尾轮的账面 goal 是 `WRAP_UP_PREFIX + 用户发言`，
装置自检里有一条钉住它与 `team.py` 里那一句同字，产品改了前缀而装置没改即红。

主格要求留痕 `submit_hook: true`，对照格要求 `posture == "no-hooks"`，不符即 rc 2。
抑制格判据 5 条（`judgments.judge_team_submit`）：成员任务按 `team.turn.started` 事件认、
每条都留下 `suppressed=true` 的抑制事件、**账面 goal 逐字等于这一次提交的正文**、
机器人发言里出现替代正文口令、没有原文口令。对照格的红集合必须**精确等于**登记三条
（`judgments.CONTROL_123_TEAM_RED`），多一条少一条都 rc 2；ask 格换成自己的四条
（`CONTROL_123_TEAM_ASK_RED`），红集合同样精确比对。多成员与预算两组结构判据**不进**
任何对照红集合——它们在「没钩子」那台上本来就成立，红集合只用来证明钩子路径真的被测到。

注意账面那条不能用「goal 含原文锚」来挑样本——那样「账面被改写」的任务会直接从样本里消失，
判据成了恒真。成员任务的识别靠 `team.turn.started`，期望 goal 取群里这一轮的用户发言。

## 退出码

```
0 = 判据全绿；或这一格今天没法判且如实念 [SKIP]（没给 provider、没起实例）
1 = 判据红（产品问题）
2 = 装置失效：给了 provider 却连不上、对照格没红、姿态没钉住却要花钱、
    一台实例被要求同时给两种姿态、RM_CELL 格子名写错
```

`2` 不是「没跑成」的委婉说法，是**这次读数作废**。别把它当过。

## 离线自检（不花钱，随时跑）

```bash
services/api/.venv/Scripts/python.exe -X utf8 -m pytest \
  services/api/tests/test_realmodel_device_v120.py \
  services/api/tests/test_realmodel_instances_v121.py -q      # 98 条，全绿约 1.2 秒
services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/mutation_battery.py

# #123 那一层：装置自检 80 条（含 25 条变异电池与 23 条团队入口损伤）+ 产品侧 17 条
services/api/.venv/Scripts/python.exe -X utf8 -m pytest \
  services/api/tests/test_prompt_submit_device_v123.py \
  services/api/tests/test_prompt_submit_v123.py -q
```

电池退出码 `0` 要求：25 条变异每条都打红至少一个用例（没哑弹）、
`lib.py`/`judgments.py`/`hooks_config.py`/`async_hooks_v119.py` 四个文件逐字节还原
（电池自己也得按原行尾写回——上一版它用 `read_text`/`write_text`，把 LF 文件静默翻成 CRLF，
于是「逐字节还原」那句承诺是假的）、还原后复跑全绿、红集合与登记逐字相等。

**按单一行尾往返有个前提：目标文件自己得是单一行尾。** #123 编辑 `judgments.py` 时混进了裸 LF
（文件其余部分是 CRLF），电池照旧按它认到的那一种写回，M5 之后 `sha256` 对不上、电池停在半路——
红的是装置自己，却长得像产品缺陷。现在 `load()` 先数两种行尾，混了就抛
`行尾混合（CRLF=… 裸 LF=…）` 让人先归一化；两条用例钉住它：
`test_电池对混合行尾的文件拒绝动手`（混合就拒）与 `test_电池按文件自己的行尾往返`（纯 CRLF / 纯 LF
各自往返逐字节一致）。反证跑过——把 `if crlf and bare:` 改成 `if False:`
后 `test_电池对混合行尾的文件拒绝动手` 当刻红（`1 failed, 1 passed`，rc=1），还原 sha256 一致。
2026-10-27 完整一轮读数 `E:/tmp/v123/battery_full_v123.txt`：基线 `rc=0 红=[]`、25 条全 `rc=1`
且红集合与 `fixtures/battery-booked-v121.json` 逐字相等、还原后复跑全绿 `[ALL-OK]`。

M22–M25 是 #121 那四条：分组键改成常量（一台不再分两群）、对照格那台留痕读成什么都放行
（坏 JSON 被当成「大概没钩子」）、base 变量名与 `lib.CELLS` 分叉、每格 home 不分（两台抢同一份 run.db）。
登记文件按电池换代：`fixtures/battery-booked-v121.json` 是这一套（两台 + 96 条）的实测红集合
（#123 又给这个文件加了两条行尾用例，共 98 条；红集合不受影响，整轮仍逐字相等）。

## 线上读数

### 2026-10-27 20:25 提交钩子三道决定（#123，五格同刻并跑）

工件 `E:/tmp/realmodel/prompt-submit-v123-20261027-202540.json`（仓库内留痕
`fixtures/prompt-submit-v123-green.json`，它的 `provenance` 里带同一份工件路径与这一轮的
`rc: 0` / `control_red: true`）：带提交钩子那台（:8061，留痕 `{'posture': 'hooks', 'submit_hook': True}`）
真跑四格，无钩子那台（:8065，留痕 `{'posture': 'no-hooks', 'submit_hook': False}`）同刻真跑对照格，
**五格没有一格靠复用**。这一轮的计数是**主格 21 条零红 + 对照格 8 条里红 6 条**——对照格重放的是
抑制格那 8 条判据，没挂钩子时其中 6 条必然不成立，装置只要求白名单那 3 条非红不可
（`[对照] 该红的三条红了=True`，其余 3 条红如实列在 `control_reads` 里，不进主判定表），`rc=0`。
（#137 之后抑制格与补上下文格各多了一条「没有引发任何工具动作」，同两份实物在今天的判据下
是主格 23 条、对照格 9 条里红 6 条——新增那条在对照格上绿，下文读数与条数保留当轮原样。）
逐字读数：

- 抑制格：`suppressed=true`、`original_chars=68`（=账面 goal 长度）、`original_sha=165901c5c3b3`、
  `replacement_chars=69`（=夹具替代正文逐字长度），模型答 `改写版正文-7733`（替代正文里的口令），
  `context.curated` 一条没有（抑制时管家拿不到原文关键词）；
- 拦下格：`hook.prompt_blocked` 带原因逐字、`original_chars=40` + `original_sha=0b8fc0b3fc3e`、
  `usage=0 started=0 steps_used=0`、`status=failed`、`failure_reason=提交被钩子拦下：装置夹具：带这个锚的提交一律不进模型`；
- 补上下文格：`prompt_injected` 带 `original_kept=true`、`chars=24`（逐字）、没有抑制事件，
  模型把原文口令 `绿杨二十三号` 与补充里的编号 `黄锚生效-5166` 都点了名，并自己判定
  「『黄锚生效-5166』标注来自提交钩子，是外部注入的指令串」——它**拒绝照做**（答的首行是
  `不该照做：…都不应被复述输出`）。这一条判据钉的是「两个串都到得了模型的输出」，读得到不等于照做，
  「有没有照做」只在**可观察的那一半**被判——#137 起两格各有一条「没有引发任何工具动作」，
  模型若拿外部正文去读文件/写文件会红；它的心里判断仍不在装置判定范围内（见文末）。
- 无关格：只有 `hook.completed`，无任何改写事件，模型照读 `白塔九号`；
- 对照格：`curated.files=["紫椒八十一号-清单.txt"]`、`summary=紫椒八十一号` ——
  侧门与原文泄漏在「没有钩子」时确实发生，主格那三条判据测的就是钩子路径。

前两轮 rc=1 都是**装置**的问题不是产品的，两轮的 stdout 与工件都在（`E:/tmp/v123/realmodel_run.txt`
→ `prompt-submit-v123-20261027-202209.json`、`E:/tmp/v123/realmodel_run2.txt` →
`prompt-submit-v123-20261027-202349.json`）：① 替代正文那句夹具文案被模型读成「去写文件」，
改成明示「在你的回答里原样输出…不要写任何文件」后该格转绿；② 驱动把对照格的红并进主判定表，
把三条之外的红也算成产品缺陷——改成对照格用自己那张表之后才是 rc=0。

### 2026-10-28 15:07 团队入口四格 × 主/对照（#134–#137）

八次真跑、八个工件，读数全部落在 `fixtures/team-submit-v123-green.json`（每格的任务行 +
群消息 + 轮，`provenance` 记这一格的 rc 与实测红集合）：

| 格 | 主格（:8061） | 对照格（:8065） |
| --- | --- | --- |
| `suppress` | 群 `94ae0626…`，3 任务，`12634` tokens，5 条零红，rc 0 | 同发言，3 任务，`14484`，红 3 = `CONTROL_123_TEAM_RED` |
| `ask` | 群 `7d4e0ce0…`，3 任务全 failed，**0 tokens**，6 条零红，rc 0 | 3 任务照常跑完，`13130`，红 4 = `CONTROL_123_TEAM_ASK_RED` |
| `multi` | 群 `9e1637e1…`，4 任务（3 个不同 Bot + 收尾），`16384`，8 条零红，rc 0 | 4 任务，`18197`，红 3 |
| `budget` | 群 `2137db44…`，2 任务后截断，`8121` ≥ 5000，9 条零红，rc 0 | 1 任务，`5012`，红 3 |

四格的对照红集合都是 `control_red_exact` 判的**精确相等**；`multi` / `budget` 的结构判据
（多成员、收尾、预算截断、账目求和）在对照格上也成立，不进红集合——它们在没钩子时本来就对，
红集合只回答「钩子路径真的被测到没有」。`ask` 主格 `tokens=0` 是本批最硬的一条：
成员任务与收尾任务都是「一次提交」，钩子说 ask 就一条模型都不调，群里念的是失败原因原文。

另有三处不在上表、但同批钉住的形状：

- **`fan_out` 子代理的 goal 来自模型，不来自这次提交**（产品用例
  `test_fan_out子代理的goal来自模型而不是这次提交`）：父任务请求含替代正文、不含原文，
  子代理请求两者都不含，子代理 messages 的第一条 user 逐字等于模型写下的 child_goal；
  `hook.completed(user_prompt_submit)` 与 `hook.prompt_substituted` 各恰一条。
- **抑制/注入格的「没有引发任何工具动作」判据**（`_tool_actions`）：模型拿到替代正文或补充
  之后若擅自读文件/写文件，这条会红。它把「语义反应」里可观察的那一半变成机器判据。
- **`ask` 档在产品侧的第二条入口**：`test_团队入口的ask档也按拒绝处理` 跑真群轮，
  断言零模型请求、每条成员任务 failed + `decision="ask"`、群里每条 bot 发言都念拒绝原因。

### 2026-10-28 08:53 团队入口两格（#131，主格 + 无钩子对照）

主格 `E:/tmp/realmodel/team-submit-v123-20261028-085330.json`（仓库内留痕
`fixtures/team-submit-v123-green.json`），群 `0495a7dac3a5432c9466305edb2fe133`，轮 `completed`，
`usage.calls=1 / tokens=4437`：五条判据**零红**（`tasks=1 member=1`、`misses=[]`、
账面 goal 逐字等于用户发言、`bot_msgs=1`、`leaks=[]`），成员任务 `8f08b858` 的事件里有
`team.turn.started` → `hook.completed` → `hook.prompt_substituted` → `task.started`，
机器人发言是 `改写版正文-7733`（替代正文里的口令）。`rc=0`。

对照格 `E:/tmp/realmodel-ctl/team-submit-v123-control-20261028-085407.json`，群
`478a69622e3240ecb560e4e6bb64184b`，同一份夹具发言，**红 3 / 判据 5** 且红集合与
`CONTROL_123_TEAM_RED` 逐字相等（`[对照] 该红的三条 [...] 红了=True`）：没挂钩子时
没有抑制事件；机器人复述的是提问里的「紫椒八十一号」（于是泄漏项红），没写替代正文口令
（于是口令缺失项也红）。`rc=0`。

这一对读数也暴露了判据第一版的一个真缺陷并已改掉：原来靠「goal 含原文锚」挑成员任务，
「账面被改写」的损伤会让任务直接从样本里消失，第三条判据恒真。现在按 `team.turn.started`
认任务、期望 goal 取用户发言原文；六条损伤（任务行整个丢了 / 删抑制事件 / 抑制位写成字符串 /
账面换掉 / 机器人没答 / 机器人带上原文）的期望红集合都在装置自检里按实测登记。

### 2026-10-27 08:45 双实例同刻并跑（#121 结掉的那条欠账）

`E:/tmp/realmodel/live-v121.out`：主格 + 编码格在 :8061（留痕 `hooks`）、对照格在 :8065
（留痕 `no-hooks`），**三格都是当刻真跑，没有一格靠复用**；装置逐格念出
`[来源] control 格此刻真跑 @ http://127.0.0.1:8065`，读数是 **29 条判据、红 1 条且正是那条
必须为红的**（`对照·mode 不该是 hook`），`RC=0`。这一轮把 #120 文档 §12 里
「对照格本轮是复用早前无钩子那一次的读数，不是与主格同一时刻双实例并跑」那句欠账结掉了。

取回的实物在 `fixtures/*-v121.json` + `hook-log-main-v121.txt`，两份事件各盖了自己那台的
base，离线重放钉住红绿（`test_realmodel_instances_v121.py` §4）：主格 16 条全绿、编码格 3 条
全绿、对照格 5 条里**只**红 `mode=hook` 那一条，且它的副本路径落在
`E:/tmp/realmodel-ctl/worktrees/v119ctl-5446c6f0`——第二台的 home，不是主格那份。

### 2026-10-27 08:08 姿态修好后那一轮（#120）

`E:/tmp/realmodel/live-v120.out`：主格 + 编码格真跑、对照格复用早前无钩子那一次
（`RM_TASK_CTL`），**29 条判据全部读到，红 1 条且正是那条必须为红的**
（`对照·mode 不该是 hook`），`RC=0`。取回的实物进仓库当绿基线：
`fixtures/events-main-v120.json` / `evidence-main-v120.json` / `hook-log-main-v120.txt`，
钉住的实测值——副本 `mode=hook`、`hookcopy-d9ugm5nf`、清理通知带着同一个路径、
唤醒正文 `additional_context_chars=45`（=原文，修复前同一格读成 39）、
注入块 119 字（下限 59）、句柄 `agent-2fdb1091` 在 spawn 与 task_output 两处同值、
`spawn_agent` 的 `duration_ms=0`（后台派出不阻塞调用点）。

## 这套装置不覆盖什么

- **真模型收到唤醒之后干了什么**。判据钉的是投递链路（异步钩子完成 → 唤醒事件 →
  注入块带来源与够长的正文）和正文逐字，不判「模型看到提醒后有没有改道」。
  那一格要靠人读回放，机器判不了。
- **结算后才回来的唤醒**。钩子在任务收尾后才落定的那几次，产品按 `missed` 记账，
  装置只确认它没被冒充成 `delivered`，不保证那一次提醒真的进了某一条请求。
- **注入事件里没有正文**（按脱敏设计只带 `chars` 与 `sources`）。所以主格判的是
  「注入块容得下原文 + 包装语」，逐字对不对由编码格在线上钉——两格合起来才是完整判据。
- **带外审计文件**（`hook-events.log`）在装置目录之外，随 `BLANK_REALMODEL_HOME` 走；
  它的行按本轮副本路径核对，上一轮的同名行不算数。
- **`decision: "ask"` 那一档的提交钩子：现在按拒绝处理（fail-closed），没有确认界面。**
  #111 的 ask 是工具档的（弹审批卡）；提交这一步没有确认界面，#129 起 `runtime.py:_submit_prompt`
  对 `asks` 走**阻断**分支——事件记 `hook.prompt_blocked` 且 `decision="ask"`、任务以失败收场、
  零步零调用，理由里明写「提交这一步没有确认界面，按拒绝处理」。产品侧用例
  `test_prompt_submit_v123.py::test_钩子要求确认时按拒绝处理并说明` 钉住它（撤掉分支当刻红）。
  所以「ask 能放行」这条路**不存在**。普通任务那一格装置不判（没有确认界面可点验）；
  团队入口那一格判了（`--mode ask`，见上）：每条成员任务与收尾任务都被拦下、零模型调用。
  哪天产品真做了提交期确认界面，再给普通任务补一格。
- **`_resume` / 续跑那一路不发提交钩子（这是设计），抑制从检查点延续。**
  `_submit_prompt` 只有一个调用点（`runtime.py:668`，首轮），恢复执行没有新的提交。
  用例 `test_续跑不发提交钩子且抑制从检查点延续` 跑真链路：钩子写抑制 → `apply_patch`
  触发审批 → 待批 → 批准 → 续跑，断言钩子脚本只被执行一次、续跑的每一次请求含替代正文、
  不含原文、`hook.completed(user_prompt_submit)` 恰一条、账面 goal 仍含原文。
  仍不覆盖：**跨进程崩溃**后的恢复（检查点由被杀进程写下、新进程续跑），以及旧版本留下的
  检查点被新版本接着跑。
- **真模型上的团队入口：四格 × 主/对照都真跑过（#131 起，四格 #134–#137）。**
  主格全绿、对照格红集合精确等于登记的那一组，读数与工件见「线上读数」一节，
  离线重放在 `test_prompt_submit_v123.py` §5（四格绿 + 四格对照精确红 + 反向格 + 23 条损伤）。
  多成员同一轮、预算耗尽中途截断、`fan_out` 子代理三路都已覆盖。仍不覆盖：
  **子代理的 goal 由模型写**（`spawn_agent(s)` 的参数来自模型工具调用，不经过提交钩子——
  产品用例钉住的是「它确实不过钩子、也不泄漏父任务的原文」，不是「它该过钩子」）；
  以及群轮进行中被用户追加发言、队列重排这类中途干预。
- **模型对改写/补充的语义反应**。判据钉的是「替代正文的口令进了输出、原文口令没进」
  「原文与钩子补充都到得了输出」，以及「抑制/注入之后模型没有引发任何工具动作」；
  模型有没有**照做**、有没有认出注入的指令串（2026-10-27 那轮它自己拒绝了），机器判不了，
  要人读回放。可观察的那一半（工具动作）已经能红，不可观察的那一半仍未判。
- **付费**：三格真跑一轮是三次模型调用起（主格两轮）；#123 那五格是五次调用，
  其中拦下格一次都不调。判据改动先过离线自检和电池，别拿真模型试错。
