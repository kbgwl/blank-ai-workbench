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
| `judgments.py` | 判据本体，全是「事件流进、判定出」的纯函数，可离线喂实物自检 |
| `lib.py` | 配置、HTTP 客户端、判定表、退出码纪律、取数小工具 |
| `launch-instance.sh` | 起隔离实例（默认 :8061）；`--with-hooks` 是钩子姿态，后面再跟 `--with-submit-hook` 才挂提交钩子，不加是对照姿态。起之前必须过 `hooks_config.py --check`，不过就不起；同时把姿态写进 `instance-posture-<port>.json`（提交钩子在留痕里是**单独一个键** `submit_hook`） |
| `hooks_config.py` | `BLANK_HOOKS` 的唯一来源与自检（`--print` 生成、`--check` 校验）。上一版这里是启动器用 `printf` 手拼的，引号没转义——见「姿态」一节 |
| `hooks/*.py` | 四种钩子夹具：`post_tool_use`（异步、退出码 2、故意写 GBK 字节）、`worktree_create`、`worktree_remove`、`prompt_submit`（#123：同一枚夹具按 goal 里的锚给四种决定——拦下 / 抑制 / 补上下文 / 什么都不做） |
| `fixtures/*.json` | 从实例原样取回的实物：修复前的负基线、编码格绿基线、**姿态修好后那轮的主格绿基线**、对照格、红集合登记表、变异电池登记；`*-v121` 是双实例同刻并跑那一轮（两台各有各的 base）；`prompt-submit-v123.json` 是**夹具与判据共读的那一份规格**（锚、正文、原因只写一处），`prompt-submit-v123-green.json` 是 #123 那轮五格实物 |
| `fixtures/wake-text.txt` | 唤醒正文的唯一来源（45 字），钩子和判据都读它，不许两处手抄 |
| `mutation_battery.py` | 把装置本身改坏 25 种，看离线自检是否每条都红；期望按实测登记 |
| `../services/api/tests/test_realmodel_device_v120.py` | 装置离线自检（70 条，不花钱） |
| `../services/api/tests/test_realmodel_instances_v121.py` | 一格一实例那一层的自检（26 条，含双实例并跑实物的离线重放） |
| `../services/api/tests/test_prompt_submit_device_v123.py` | #123 那一层的自检（38 条）：五格实物重放、对照格三条该红的逐字红、25 条变异电池、夹具过产品解析器、姿态闸门 |

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
| 抑制格 | `紫椒八十一号` | `suppressOriginalPrompt: true` + 替代正文 | 事件带 `suppressed=true`、原文只留长度+12 位指纹、替代正文逐字等长、**账面 goal 仍是用户写的那份**、模型读的是替代正文、管家候选没被原文关键词带出来（8） |
| 拦下格 | `绿锚不许进模型` | stderr 写原因 + 退出码 2 | 事件带原因逐字、只登记长度与指纹、**一次模型调用都没发起**（无 `model.usage`、无 `task.started`、`steps_used=0`）、任务以失败收场且原因指向钩子、不改账（5） |
| 补上下文格 | `绿杨二十三号` | 只给 `additionalContext` | `original_kept=true`、补充正文逐字等长、**不许冒出抑制事件**、正文不进事件、模型同时读到原文口令与补充里的编号（5） |
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

# #123 那一层：装置自检 38 条（含 25 条变异电池）+ 产品侧 13 条
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
  改不改道从来不在装置判定范围内（见文末）。
- 无关格：只有 `hook.completed`，无任何改写事件，模型照读 `白塔九号`；
- 对照格：`curated.files=["紫椒八十一号-清单.txt"]`、`summary=紫椒八十一号` ——
  侧门与原文泄漏在「没有钩子」时确实发生，主格那三条判据测的就是钩子路径。

前两轮 rc=1 都是**装置**的问题不是产品的，两轮的 stdout 与工件都在（`E:/tmp/v123/realmodel_run.txt`
→ `prompt-submit-v123-20261027-202209.json`、`E:/tmp/v123/realmodel_run2.txt` →
`prompt-submit-v123-20261027-202349.json`）：① 替代正文那句夹具文案被模型读成「去写文件」，
改成明示「在你的回答里原样输出…不要写任何文件」后该格转绿；② 驱动把对照格的红并进主判定表，
把三条之外的红也算成产品缺陷——改成对照格用自己那张表之后才是 rc=0。

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
- **`decision: "ask"` 那一档的提交钩子**。#111 实现了工具档的 ask，提交档那一路
  （`runtime.py:_submit_prompt`）只看 `denied` / `suppress_original` / `additional_context`，
  `asks` 没有分支——它只随 `hook.completed` 的 `results` 记账，不会弹问用户；装置因此也不判它。
- **`_resume` / 续跑那一路不发提交钩子**。`_submit_prompt` 在运行时里只有一个调用点
  （`runtime.py:668`，首轮），钩子按「一次提交」建模，恢复执行没有新的提交，
  所以恢复后的正文是否仍受抑制影响，这套装置测不到（#123 判据只覆盖首轮提交）。
- **真模型上的团队入口**。成员任务的 goal 也过提交钩子，但这一条只有产品侧 mock 用例
  （`test_prompt_submit_v123.py::test_团队入口的成员任务也过提交钩子`）钉着；装置四格走的是
  `/threads/<id>/turns` 那条普通任务入口，:8061 上的团队链没在这批点验。
- **付费**：三格真跑一轮是三次模型调用起（主格两轮）；#123 那五格是五次调用，
  其中拦下格一次都不调。判据改动先过离线自检和电池，别拿真模型试错。
