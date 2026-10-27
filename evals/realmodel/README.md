# 真模型点验装置（#116–#119）

把上一批登记的那条欠账闭掉：真模型点验原来活在 `E:/tmp/realmodel/v119_*.py` 里，
换台机器、换个人就复现不了。现在判据、夹具、钩子、启动器都在仓库里，
**凭据仍然不在**——实例的数据目录、settings.json、secret.key 由环境变量指过去，
仓库一个字节都不收（`test_装置目录里不许有凭据或密钥文件` 扫整个目录）。

## 文件

| 文件 | 职责 |
| --- | --- |
| `async_hooks_v119.py` | 驱动：铺工作区、起任务、取事件、汇总三格读数、定退出码 |
| `judgments.py` | 判据本体，全是「事件流进、判定出」的纯函数，可离线喂实物自检 |
| `lib.py` | 配置、HTTP 客户端、判定表、退出码纪律、取数小工具 |
| `launch-instance.sh` | 起隔离实例（默认 :8061）；`--with-hooks` 是钩子姿态，不加是对照姿态。起之前必须过 `hooks_config.py --check`，不过就不起；同时把姿态写进 `instance-posture-<port>.json` |
| `hooks_config.py` | `BLANK_HOOKS` 的唯一来源与自检（`--print` 生成、`--check` 校验）。上一版这里是启动器用 `printf` 手拼的，引号没转义——见「姿态」一节 |
| `hooks/*.py` | 三种钩子夹具：`post_tool_use`（异步、退出码 2、故意写 GBK 字节）、`worktree_create`、`worktree_remove` |
| `fixtures/*.json` | 从 :8061 原样取回的实物：修复前的负基线、编码格绿基线、**姿态修好后那轮的主格绿基线**、对照格、红集合登记表、变异电池登记 |
| `fixtures/wake-text.txt` | 唤醒正文的唯一来源（45 字），钩子和判据都读它，不许两处手抄 |
| `mutation_battery.py` | 把装置本身改坏 21 种，看离线自检是否每条都红；期望按实测登记 |
| `../services/api/tests/test_realmodel_device_v120.py` | 装置离线自检（70 条，不花钱） |

## 三格

- **主格**（带钩子，两轮 + 一次回退）：#116 后台句柄链、#117 每轮回退点、
  #118 钩子副本与清理、#119 唤醒投递。16 条判据。
- **编码格**（带钩子，一轮）：唤醒正文逐字不许变形。#119 那个 GBK 缺陷就红在这里，
  修复前实测读成 39（45 字被静默吃掉 6 个），这条判据现在钉的是「等于原文长度」。
- **对照格**（**不配钩子**）：同一套判据打在没有 `worktree_create` 钩子的实例上，
  期望 `mode=hook` 那条**必须红**。它不红 = 主判据在任何一次运行里都成立 = 什么都没测到，
  整次点验作废（rc 2，不是 rc 0）。

## 姿态：花钱之前先钉死，两道闸

`HookRunner.from_env` 是 fail-closed 的——配置解析失败一律降级成「没有钩子」，**哪里都不报错**。
所以姿态错了不会有人告诉你，只会在三格跑完之后把 11 条钩子判据全判红。#120 第一次真跑就栽在这：
启动器用 `printf` 手拼 `BLANK_HOOKS`，引号没转义 → 产品读成没有钩子 → 钱花了，红的是姿态。

- **起实例时**：`launch-instance.sh --with-hooks` 先 `hooks_config.py --print` 生成、再 `--check` 自检
  （合法 JSON、三个事件齐、命令恰好两段、夹具文件真存在、async 只在 `post_tool_use`），
  自检不过直接 rc 2 不起服务。
- **花钱之前**：装置读 `instance-posture-<port>.json`（`lib.premise_error`）。
  要真跑主格/编码格而姿态不是 `hooks` → rc 2 拒跑；要真跑对照格而姿态是 `hooks`、或留痕文件不在
  （实例是手起的，姿态未知）→ 同样 rc 2。**手起的实例不假定它带了钩子。**
  复用早前那几轮（`RM_TASK_MAIN` / `RM_TASK_ENC` / `RM_TASK_CTL`）不花钱，闸门不拦。
- 坏 JSON 那串（`BROKEN_RAW`）留在自检里当永久负基线：产品必须读成 `enabled is False`。

## 跑法

```bash
# 1. 起带钩子的隔离实例（凭据在 BLANK_REALMODEL_HOME 里，仓库看不到）
BLANK_REALMODEL_HOME=E:/tmp/realmodel bash evals/realmodel/launch-instance.sh --with-hooks &

# 2. 三格真跑（花真模型的钱）
export BLANK_REALMODEL_PROVIDER=<provider id>
export BLANK_REALMODEL_HOME=E:/tmp/realmodel
export BLANK_REALMODEL_BASE=http://127.0.0.1:8061
services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/async_hooks_v119.py

# 已经跑过的那几轮按任务 id 复用事件重算，不再花钱：
RM_TASK_MAIN=<id> RM_TASK_ENC=<id> RM_TASK_CTL=<id> 同上
# 只跑某几格：RM_CELL=encoding,control 同上
```

对照格要的是**没有钩子**的那台实例：先 `launch-instance.sh`（不加参数）起一轮，
再跑装置；或者复用早前在无钩子姿态下跑过的那一轮（`RM_TASK_CTL`）。
复用模式下回退格不重判——那一轮已经退过，重算会把「已回退」读成缺陷，
读数里会明写哪几条没判。

## 退出码

```
0 = 判据全绿；或这一格今天没法判且如实念 [SKIP]（没给 provider、没起实例）
1 = 判据红（产品问题）
2 = 装置失效：给了 provider 却连不上、对照格没红、姿态没钉住却要花钱、RM_CELL 格子名写错
```

`2` 不是「没跑成」的委婉说法，是**这次读数作废**。别把它当过。

## 离线自检（不花钱，随时跑）

```bash
services/api/.venv/Scripts/python.exe -X utf8 -m pytest \
  services/api/tests/test_realmodel_device_v120.py -q
services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/mutation_battery.py
```

电池退出码 `0` 要求：21 条变异每条都打红至少一个用例（没哑弹）、
`lib.py`/`judgments.py`/`hooks_config.py`/`async_hooks_v119.py` 四个文件逐字节还原
（电池自己也得按原行尾写回——上一版它用 `read_text`/`write_text`，把 LF 文件静默翻成 CRLF，
于是「逐字节还原」那句承诺是假的）、还原后复跑全绿、红集合与登记逐字相等。

## 线上读数（2026-10-27，:8061）

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
- **付费**：三格真跑一轮是三次模型调用起（主格两轮）。判据改动先过离线自检和电池，
  别拿真模型试错。
