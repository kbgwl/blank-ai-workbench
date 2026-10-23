# 自建沙箱立项设计：没有 Docker 的 Windows 上，把边界做成真的

状态：**设计稿，未动代码**。四条路线已由用户裁决（原生 Windows 内核级隔离 / 三档 sandbox × 审批模式 /
越狱探针自证 + 证据链 / 全 Agent 进边界），本文件回答的是"具体怎么建、怎么证明它真的拦住了"。
所有"现状"结论都带 `文件:行号`，来自 2026-10-22 通读代码，不是推测。

## 0. 为什么要自建，而不是继续用 Docker

产品要交付给的是**装了 Windows 11 家庭版、不是管理员、没装 Docker** 的用户（这台开发机就是这个形状：
连"查询可选功能"都返回 `requires elevation`）。在这种机器上：

- Docker 路线直接不存在；
- Hyper-V / Windows Sandbox 是专业版以上的可选功能，也排除；
- 于是今天代码里的实际行为是：`BLANK_SANDBOX_ENABLED=0` 是桌面壳的默认
  （`services/api/src/blank_api/services/safety.py:156-158` 注释），命令**跑在宿主上，用用户的完整权限**。

Codex 在这台机器上做的事（见 `docs/REA-WAVE4-CODEX-RUNTIME.md` §5）是：两个本地专用账号
（`CodexSandboxOffline` / `CodexSandboxOnline`）+ DPAPI 存口令 + **按工作区分配自定义 SID 写 ACL** +
独立助手进程以另一用户身份 `CreateProcessAsUser`。这条路要**创建本地用户**，创建用户要管理员权限——
它成立的前提是我们没有的那半截权限。所以差异化不是"抄得像"，而是：
**只用非管理员就能拿到的内核级机制，把"能不能隔离"从权限问题变成机制问题。**

## 1. 现状基线：今天真正生效的边界，和只是展示值的边界

先把话说难听一点，否则设计文档会在一个谎上面盖楼。

| 声称 | 实际 | 依据 |
| --- | --- | --- |
| 资源限额 `cpus 1 · memory 512m · pids 128 · tmpfs 128m` | **只有 Docker 后端真实生效**；`local` 后端一个都不生效，面板照旧展示 | `sandbox/policy.py:56-90`（`docker_flags` 是唯一落点）、`agent/governance.py:45-48`（无条件回报）、`web/src/components/tasks/api.ts:750-779`（拼串） |
| 沙箱开关 | 同名 env 两个默认值：`policy.py:33` 默认 `true`，`safety.py:446` 默认 `false` → 面板说的和路由选的不是一个口径 | 同上 |
| 缺 Docker 且没开 `allow_local` | `backend_info` 仍说"本机受限执行"，实际命令被拒（`SandboxUnavailable`） | `sandbox/runner.py:210-219`、`safety.py:433-442` |
| 文件写入边界 | **纯代码检查**：`resolve_for_write` 拒绝对外逃逸；命令里的越界路径靠 `_outside_paths`，它只把**真实存在**的绝对路径算作越界（代码里写明是"误杀会让人关掉整条策略，比漏判更糟"，`safety.py:271-274`） | `tools/paths.py:29-44`、`safety.py:247-254`、`269-305` |
| `LocalSandbox` | 只有超时 + 进程树清理；它自己的 docstring 就承认不是隔离 | `sandbox/runner.py:139-192` |
| 审批节奏 | 审批卡上**无条件提供**"本会话总是允许该工具"这一档（`SCOPE_TOOL`），用户点一次之后该工具整会话不再问；叠加 `bypass` 可长驻宿主执行 | `permissions.py:272-281`（第 278-280 行那支不带任何条件） |
| 被拒调用 | 事件与审计都留痕（`approval.denied` + JSONL 照样落一条），但 `tool.completed` 没有 `sandbox_backend` 键，`ToolCall` 表也没有 backend 列 | `agent/graph.py:1250-1257`、`tools/gateway.py:87-129`、`agent/evidence.py:151-153` |
| `writable_rootfs` | 由 `SANDBOX_READ_ONLY_ROOT=true` **取反**得到，命名与语义相反 | `policy.py:40` |

一句话结论：**今天的边界只存在于"我们的代码愿意检查的那部分"，操作系统层面没有任何东西在拦。**
这不是猜的——把 `safety._candidate_paths` 单独拉出来喂四条命令（2026-10-22 实测）：

```
cat C:\Users\22103\.ssh\id_rsa              -> ['C:\\Users\\22103\\.ssh\\id_rsa']   看得见
echo hi > C:\Users\22103\out.txt            -> ['C:\\Users\\22103\\out.txt']        看得见
python -c "open(r'C:\Users\22103\.ssh\id_rsa').read()"   -> []                     看不见
python -c "import pathlib;print(pathlib.Path('C:/Users/22103/.ssh/id_rsa').read_text())" -> []  看不见
```

**路径出现在参数位就拦得住，藏在脚本字符串里就一个字都看不见**——而模型写的命令十有八九是后一种形状。
所以本立项的第一条验收判据就是这条必须被拦住，且要靠 OS 拒（错误码 5），不是靠我们的正则又变聪明一点。
> M0 补一句（见 §2.0）：这条判据只在 `os_enforced` 档成立。本机（非管理员 build 26300）进不了 AC、
> 造不出受限令牌，所以这条在 `job_only` 档**必须允许它是红的**——红着并写明"未由系统强制"，
> 比靠放宽拦截规则把它做绿更有价值。

## 2. 机制选型：只用非管理员拿得到的内核级原语

四条候选机制，按"是否需要提权"分层。**每条都带一个探针待验项**——不验就不写进实现。

### 2.0 M0 实测表（已验，不再是"待探针"）

机器形状：非管理员（TokenIsElevation=0）/ Windows 11 build 26300 / 无 Docker·Hyper-V·Windows Sandbox /
令牌内无 `SeImpersonate`·`SeCreateToken`·`SeIncreaseQuota`（P0.3）。
工件：`E:/tmp/realmodel/m0-probe-11.log`（29 条，OK=19 / 内核拦住=3 / 不可用=4 / 不支持=3 / 未定论=0）
与 `E:/tmp/acprobe/m0-report-11.json`；跨进程树对照 `E:/tmp/realmodel/detached-{in-tree,detached-task}.txt`。

| 内核原语 | 探针行 | 本机结论 | 可核对的判据 |
|---|---|---|---|
| 拿到 AppContainer SID | P1.3 | **可用**，零权限 | `DeriveAppContainerSidFromAppContainerName` 对**未注册**的名字也返回 `S-1-15-2-30416275-…` |
| 建 AC 档案 `CreateAppContainerProfile` | P1.2 / P1.2b | **不可用** | NULL SD → `0x80070057`；换成 `IsValidSecurityDescriptor=True` 的真实 SD → API 内部访问违例（在子进程里实测，ctypes 与 .NET P/Invoke 两套栈同结果）→ 拒绝与 SD 内容无关 |
| 给 AC SID 授/拒 NTFS ACE（含 `/T` 递归） | P2.1–P2.3 | **可用**，非管理员对自己拥有的目录 | `icacls` 的 SID 参数**必须带 `*` 前缀**：裸 SID → 1332，`*SID` → rc=0；`/grant (OI)(CI)(RX) /T` 与 `/deny` 全 rc=0 且子文件回读确认继承 |
| 把进程放进 AC（属性号 0x20008） | P3.0b / P3.1 / P3.1c / P3.2 | **不可用** | 属性表确实被 CreateProcess 消费（变异自检：`PARENT_PROCESS=0xDEAD` → 998）；0x20008 下**真 AC SID=575、NULL SID=575、未知属性号=50、不传属性=exit 0** 四种结果互不相同 → 号被解释、但拒绝发生在读 payload 之前；`where`/`cmd`/`notepad`/`csrss` 四个载体 × `CREATE_NO_WINDOW` 开与关 × 内置/派生/非 AC SID 全部同码 |
| 跨进程树对照（排除"是我们自己的包装层坏了"） | 工件 `detached-*.txt` | 结论不变 | 由任务计划程序起（父进程 Tasksvc，不在 Qoder/uv 的进程树与包装层里）跑同一份载荷：仍 575 / 998 |
| 自我降权令牌 `CreateRestrictedToken` | P5.1b / P5.2 | **不可用** | 998，且 `flags=0`（任何 Windows 都该成功的最小形状）与降权组合同码；同一句柄上 `DuplicateTokenEx` 成功 → 不是"我们的句柄坏了"；ctypes 与 .NET 两套栈同码 |
| 换身份起进程 `CreateProcessWithTokenW` | P5.3 | **不可用（特权缺失被证实）** | 三种参数形状 → `1314 / 1314 / 87`：`ERROR_PRIVILEGE_NOT_HELD` 拿到两次，说明缺 `SeImpersonatePrivilege` 这条被实测证实（上一版只试一种形状拿到的是 87，那是探针自己填错参数，不是机器的结论） |
| Job：建 / 设限 / 挂载 | P6.1 / P6.3 | **可用**，且子进程真的在 Job 里 | `QueryInformationJobObject(BasicProcessIdList)` 当场回读到该 PID（`子进程pid=36448 Job内PID=[36448] 在列表里=True`） |
| Job 内存上限 | P6.2 / P6.4 | **内核强制生效** | 上限 48MB 时子进程分配 256MB → `MemoryError`；同一命令在 Job 外 → `ALLOC_OK 67108864`（阳性对照） |
| Job 活跃进程数=1 | P6.5 | **生效** | 第二个进程 `AssignProcessToJobObject` 直接被拒（err=1816） |
| Job `KILL_ON_JOB_CLOSE` | P6.7 | **生效** | 关句柄后 600 秒睡眠的子进程被带走；不挂 Job 的同种对照子进程 1 秒后仍在运行 |
| `CheckNetIsolation LoopbackExempt -s` | P5.4 | 列出不需要管理员 | rc=0（只测了 `-s` 列出，没测 `-a` 新增；AC 本身进不去的机器上这条已无意义） |

**这张表对原设计改了一处根本性的东西**：主隔离层（AppContainer）和第二层（受限令牌）
在"非管理员 + 这台 build"的形状上**都拿不到**；非管理员手里唯一被内核强制的原语是 **Job Object**
（资源上限 + 进程数 + 树终止）。NTFS 那边"能配 ACE，却没有可放进去的 Principal"——
授 ACE 的路是通的（P2 全绿），但 P3 进不去，所以**授权给一个没人能变成的身份等于没授**。
下面 2.1/2.2/2.3 的每一条都按这个事实改口径，并新增 2.5「边界强度分级」。

### 2.1 AppContainer（主隔离层）

- 是什么：内核级的 capability 沙箱。进程带一个 AppContainer SID，NTFS 默认对它**拒绝一切访问**，
  除非目录 DACL 里显式给了这个 SID 的 ACE。这正是 Codex `cap_sid` 那本账想要的效果，
  但不需要创建用户。
- 非管理员可行性（探针 P1）：**一半成立**。拿 SID 不要权限（P1.3 `DeriveAppContainerSidFromAppContainerName`
  对未注册名字也返回 `S-1-15-2-…`），但**建档案不行**（P1.2/P1.2b：NULL SD → `0x80070057`，
  通过校验的真实 SD → API 内部访问违例）。给**自己拥有的目录**加 ACE 确实不要权限（P2 全绿）。
- 进边界（探针 P3，原设计没单列的一条，现在它是决定性的）：**不行**。0x20008 属性号被 CreateProcess
  解释，但对任何载荷都返回 575，且不传属性时同一载体正常退出 → 拒绝在载荷之前，与"我们结构填错"无关；
  跨进程树对照同码。
- 白送的性质（探针 P2/P4）：**AC 默认出网被拒这一条本机无法验证**（P4.0：进程根本进不去 AC，
  行为 A/B 无从跑起）。放行要显式 capability + 防火墙规则（后者要管理员）仍是文档口径，
  但对我们的产品而言它目前是**不可用的一档**，不是"自然成立的默认状态"。
- 边界落点（原设计保留，作为 `os_enforced` 档的实现）：工作区根目录 + 一个专属临时目录授予读写；
  其余一律不给 ACE；deny-read 不需要单独机制——**没有 ACE 就是不可读**（P2.3 也证明 `/deny` 可用）。
- 结论：**这一层不作为本机的默认交付**。它降级成"能力探测通过后启用"的加强档（见 2.5），
  并在 `blank doctor --sandbox` 里明确告诉用户本机为什么只有 `job_only`。

### 2.2 受限令牌（第二层，收窄身份）

- ~~`CreateRestrictedToken` 用自己的令牌即可创建（不需要提权，待探针 P3）~~
  **实测否决（P5.2）**：本机返回 998，`flags=0` 的最小形状也一样；同句柄 `DuplicateTokenEx` 成功、
  ctypes 与 .NET 两套栈同码、脱离我们进程树后同码 → 这是机器级不可用，不是调用写法问题。
- 目的不是"变成另一个用户"，而是**让进程拿不到用户自己的完整令牌**：
  即使 AppContainer 的 ACL 有配置失误，受限令牌也读不到 `HKCU` 下的大多数键、写不进 `%APPDATA%`。
- 与 `CreateProcessWithTokenW` 的兼容（原"探针 P4"，实测在 P5.3）：**被特权缺失证实**——
  三种参数形状给出 `1314 / 1314 / 87`，`ERROR_PRIVILEGE_NOT_HELD` 意味着"专用账号 + 换身份执行"
  这条 Codex 路线在本机不可用（P0.3 的 `whoami /priv` 里没有 `SeImpersonatePrivilege`，两侧口径对上）。
- 按本节原预案执行：**2.2 整层降级为"本机不提供"**，文档与证据包如实写"第二层未生效"，
  而不是拿"我们有这段代码"充数。差异化不落在抄账号，也不落在假装进 AC，而落在 2.5 的**强度分级 + 自证**。

### 2.3 Job Object（资源与生命周期）

- 非管理员可用的部分（原"待探针 P5"，实测在 P6）：**这一层是 M0 唯一全绿的机制**。
  `CreateJobObjectW` 不要权限（P6.1）；`SetInformationJobObject` + `AssignProcessToJobObject` 有效，
  且 `QueryInformationJobObject(BasicProcessIdList)` 当场能回读到子进程 PID（P6.3——这条差点是假的：
  第一版取 PID 列表的时序放在子进程退出之后，永远回 `[]`，另一版把数组起点按 `sizeof(结构体)` 算，
  永远回 `[0]`）；`PROCESS_MEMORY`=48MB 时 256MB 分配被内核拒（P6.4，Job 外同命令成功）；
  `ACTIVE_PROCESS`=1 时第二个进程 assign 被拒 err=1816（P6.5）；
  `KILL_ON_JOB_CLOSE` 真的带走 600 秒睡眠的子进程，不挂 Job 的对照还活着（P6.7）。
  **未测**：`JOB_OBJECT_UILIMIT_*`（禁剪贴板/句柄/桌面切换）与 breakaway 语义——M1 的探针补，
  没测就不写进实现、不显示在面板上。
- 明确不承诺的部分：CPU 配额（`JOB_OBJECT_LIMIT_SCHEDULING_CLASS` 语义不是限速；
  Win11 的 CPU rate control 需要管理员）→ 面板上 `cpus` 这一项在自建后端下**改叫"未强制"而不是继续显示 1**。
- 磁盘配额：NTFS quota 要管理员；替代方案是**只给一个小 tmpfs 等价物**（`RAMDISK` 不可行 →
  用工作区内的专属临时目录 + 写入字节数闸门，即今天的 `max_file_bytes` 语义，但改成**由执行层拦而不是由工具层劝**）。

### 2.4 执行体分离（照抄 Codex 的那一半）

Codex 用 `.sandbox-bin/codex-command-runner-<version>.exe` 独立助手进程执行命令，
这样"起进程的那个代码"和"主程序"不是同一个令牌上下文。我们的落点：
一个随安装包分发的 `blank-runner.exe`（极薄：收命令 → 以 AppContainer + Job 起子进程 → 回传 stdout/exit code），
主服务进程**不再自己 `subprocess`**。这条是纯工程，不需要权限，且它让"哪条命令走了边界"变成一个
可枚举的进程边界，而不是代码里的一处分支。

### 2.5 实测之后的必然结论：边界强度是一等公民，不是布尔

Codex 与主流产品把 sandbox 讲成"开/关"。M0 之后我们的口径改成**先测后承诺**：
探针在本机算出一个 `boundary_strength`，它进 `turn.context`、进证据包、进界面文案，
并且**决定 §1 那几条逃逸判据是否成立**。

| 强度 | 成立条件（都由探针实测，不看注册表猜） | 内核强制的东西 | 不强制的东西 |
|---|---|---|---|
| `os_enforced` | P3 的 AC 入口生效 **或** P5.2 的受限令牌可用 | 文件读写（没有 ACE 即拒）、身份、Job 三项 | CPU 配额、磁盘配额 |
| `job_only`（**本机就是这档**） | P6 三项生效、P3/P5 都不可用 | 内存上限、进程数上限、整棵树终止 | **文件、网络、注册表全部不强制** |
| `none` | 用户显式选 `off`，或连 Job 都建不出来 | 无 | 一切 |

`job_only` 档必须在界面上写清楚一句话：**模型把路径藏在 `python -c "open(...)"` 字符串里时，
OS 不会替我们拒**（§1 的实测形状）。这一档拦越界靠的是执行体分离 + 路径闸门 + 审批 + 事后证据链，
它的名字不能叫"沙箱"，文案要写"资源与生命周期隔离（文件边界未由系统强制）"。
这就是立项第四条"越狱探针自证 + 证据链"的落点：**我们不比"有没有沙箱"，比"敢不敢把自己的边界
强度测出来并告诉用户哪条是真的"**。

强度是**每次启动测**的，不是装机时测一次写死的：同一台机器换权限形状（管理员、UAC 设置、
安全软件注入）结论会变，M4 的"第二台机器"判据就是为这条设的。

## 3. 三档 sandbox × 四模式审批：正交矩阵

今天沙箱与审批确实正交（审批只管"问不问"，不改变"跑在哪"），但**档位只有一档半**
（container / host，`governance.py:43`）。立项改成三档，与四模式组成可核对的矩阵。

| 档位 | 边界 | 默认可写 | 网络 | 谁在用 |
| --- | --- | --- | --- | --- |
| `off` | 无（宿主，用户完整令牌） | 整个用户目录 | 有 | 只在用户显式选择时提供，且**每次调用都在事件与面板上写"无隔离"** |
| `workspace` | AppContainer + 受限令牌 + Job | 仅工作区根 + 专属临时目录 | **无** | 产品默认 |
| `workspace-net` | 同上 + 出网放行 | 同上 | 有（需探针确认可行性；不可行则该档在 Windows 上不提供，退化成 `off` + 明确告知） | 需要 `pip install` / 拉取依赖的任务 |

| 审批模式 | 与档位的关系 |
| --- | --- |
| `default` | 每个写/命令按规则问；档位由任务配置 |
| `accept_edits` | 只免"工作区内写文件"的问，**不免命令**，也不改变档位边界 |
| `plan` | 只读；`workspace` 档 + 不给写 ACE（今天的 plan 只靠代码判，改后由 ACL 保证） |
| `bypass` | 免问，但**不许升档**：`bypass` 只能选 `off`/`workspace`/`workspace-net` 中已定的那一档，不能因为免问就绕过 ACL |

要改掉的两条现状缺陷：
1. `SCOPE_TOOL` 的"批准一次整会话免问"（`permissions.py:279`）改成**按档位与目标路径前缀**免问，
   且免问范围进 `turn.context` 快照，事后能看出"当时免的是哪一片"。
2. `off` 档不再伪装成"受限执行"：`backend_info`（`safety.py:433-442`）在 `off` 下必须回
   "无隔离，宿主完整权限"，面板同步改文案。**宁可难看，不许说谎。**

## 4. 全 Agent 进边界

今天只有 `execute_command` 走沙箱路由，文件工具、子代理、团队、MCP 全在主进程里以用户权限跑。
立项要求：**凡是能产生副作用的执行体，都在同一个 AppContainer 里**，具体四件事：

- **文件工具**（`read_file` / `write_file` / `apply_patch`）：不再靠 `resolve_for_write` 判逃逸，
  而是让服务进程自己就**没有**工作区之外的写 ACE。代码检查降级为"给用户看得懂的错误信息"，
  不再是唯一防线。代价：主服务进程不能直接进 AppContainer（它要读配置、连数据库），
  所以文件工具改走 §2.4 的 `blank-runner` 或一个专职的 `fs-broker` 子进程。
- **子代理与团队**：子代理继承父任务的档位，且**不许自行降档**。
  这条要落进数据：`model.usage` 与 `turn.context` 已有 `depth` / `agent_path`，
  加一个 `sandbox_mode`，让"哪个分支在哪一档里跑"可核对（#33 已经把身份字段打通，接着用）。
- **MCP / 外部工具**：第三方进程今天完全在主进程权限下起。立项要求它必须进 `workspace` 档，
  并在证据包里单独列出"这个 MCP 的边界是什么"。
- **回滚快照**（`sandbox/snapshot.py`）与边界是两件事，不要混：快照是**可逆性**，
  ACL 是**不可达性**。两者都要，且快照失败仍要继续跑并在事件里说明原因（沿用现有原则）。

## 5. 越狱探针：每条边界都必须被主动攻击一次

这是本立项与"写了个开关就算安全"的分界线。**每条边界配一个逃逸尝试，探针必须能失败**，
且装置自检：先证明它**能红**（把闸门摘掉跑一遍，必须成功逃逸并报错），再证明它**拦得住**。

| 探针 | 攻击 | 期望 | 待验 |
| --- | --- | --- | --- |
| P-READ | 沙箱内 `python -c "open(r'C:\Users\<me>\.ssh\id_rsa')"` | 拒绝（无 ACE，非"代码检查拦下"） | 必须证明是 OS 拒的：同时断 Windows 错误码 `ERROR_ACCESS_DENIED (5)` |
| P-WRITE | 沙箱内向工作区外写文件、写 `%TEMP%\..\` 穿越、写符号链接目标 | 拒绝 | 符号链接与 junction 是重点：`resolve` 检查在 OS 层不算数 |
| P-ENV | 沙箱内读 `HKCU\Environment`、`%APPDATA%` 下的密钥文件 | 拒绝 | — |
| P-NET | `workspace` 档里 `curl https://example.com` / `pip download` | 失败 | 与 P2 同批验：确认默认拒不是"恰好没网"，阳性对照要能通（`workspace-net` 档下同一命令必须成功） |
| P-PROC | 起 200 个进程 / 申请 8GB 内存 | 被 Job 拒或杀 | 与 P5 同批 |
| P-ESCAPE | 沙箱内进程试图给自己的 AppContainer 加 ACE、试图 `RunAs` 提权 | 失败 | owner 权限下"能不能自己放宽 DACL"是关键未知数，失败即说明边界可被自我放大，需改设计 |
| P-INHERIT | 沙箱内进程再 spawn 子进程（`cmd /c start`、PowerShell `Start-Process`） | 子进程仍在边界内 | AppContainer 属性是否随 `CreateProcess` 传递要实测 |
| P-AUDIT | 上述每条执行完，检查证据包/事件/JSONL 三处都留痕且 `sandbox_mode` 正确 | 三处同源 | 沿用 `evals/boundary_probe.py` 的形状 |

探针产物落进证据包，成为**用户可自查**的东西：安装后跑一次 `blank doctor --sandbox`，
输出"八条边界，几条通过、哪几条这台机器做不到、为什么做不到"。这把"我们说安全"变成"你自己看一眼"。

## 6. 证据链要补的三处硬缺口

1. `ToolCall` 表没有 `backend` / `sandbox_mode` 列。这是**现在已有一条有意决策**：
   `evidence.py:151-153` 写着"不给 ToolCall 表加列：台账记做了什么，执行边界记在哪做的，
   证据包在读出时把边界贴回工具清单"。本立项**要推翻它**，理由不是当初判断错，而是前提变了：
   三档 + 全 Agent 进边界之后，"这次执行在哪一档"变成审计的第一问，
   而它目前只活在事件里——#33 刚抓到过事件会因 seq 竞态静默丢失，
   把审计的第一问建立在一个可丢的通道上不再可接受。迁移要连带回答"老任务该回填什么"（建议回填 `unknown`，不假装是 `off`）。
2. 被拒调用今天没有 `sandbox_backend`（`gateway.py:67-75`）→ 改成"被拒也要写它**本来会**跑在哪一档"，
   否则事后无法回答"那次免问批准如果放行了，会在什么边界里执行"。
3. `BLANK_SANDBOX_ENABLED` 两处默认值冲突（`policy.py:33` vs `safety.py:446`）必须收敛成**一个读取点**
   （建议 `governance.sandbox_boundary()`，它已经是快照口径的单点），否则面板与行为会长期漂移。

## 7. 分期与退出判据

每期的"完成"由探针定义，不由代码存在定义。

- **M0 权限现实勘测（纯探针，不改产品代码）——已完成**：`evals/win_isolation_probe.py`，
  29 条判定全部有确定结论（未定论=0，进程退出码即判据：有 UNKNOWN 就返回 1）。
  结论见 §2.0 的实测表。退出判据"一份'哪些内核原语在非管理员下可用'的实测表"已交付，
  并且**它改了 M1 的交付内容**——原设计假定的主隔离层在本机拿不到。
- **M1 执行层换骨（按 M0 结果重写）**：`blank-runner.exe` + **Job 三项限额**接管 `execute_command`；
  探针随包分发、启动时测出 `boundary_strength`；面板不再展示未强制的限额。
  退出判据分两段，**不许混**：
  - `job_only` 档（本机默认）可交付且必须红转绿的：P-MEM（内存上限）、P-PIDS（进程数）、
    P-TREE（停止任务后进程必须死）——三条在 M0 已有阳性对照，M1 要在**真实任务链路**上复跑；
  - P-READ / P-WRITE（§1 那条"路径藏在脚本字符串里也要被 OS 拒"）**在 `job_only` 档必然红**，
    因为 M0 证明了本机没有可放进去的身份。所以这两条判据绑 `os_enforced` 档：
    在支持 AC/受限令牌的环境上转绿，在本机则要求**界面与证据包如实显示"未由系统强制"**。
    把这两条在本机"做绿"的唯一办法是放宽 §1 反对的那类正则——**不允许**。
  - `off` 档文案诚实：不许出现"沙箱已启用"字样。
- **M2 三档 × 四模式矩阵落地**：档位成为任务级配置并进 `turn.context`；免问范围按档位+路径前缀。
  退出判据：矩阵 12 格里每格至少一条可失败断言；P-NET / P-PROC / P-ESCAPE / P-INHERIT 四条有结论。
  （M0 已注定：本机 P-NET 只能测"没隔离"那一侧，AC 出网默认拒无法验证。）
- **M3 全 Agent 进边界**：文件工具、子代理、团队、MCP 走同一边界；`sandbox_mode` 进用量与事件侧。
  退出判据：一条真跑的任务里，**每个** `agent_path` 都能查出它所在的档位；跨档降档必须失败。
- **M4 自证产品化**：`blank doctor --sandbox` + 证据包里的边界报告 + 安装包内的探针。
  退出判据：在**第二台**机器（不同权限形状）上跑通，输出与本机差异可解释。

### 7.1 M1 交付记录（2026-10-22，判据按 §7 两段分开算）

`job_only` 档那三条（P-MEM / P-PIDS / P-TREE）已在**真实任务链路**上复跑，不是在单元测试里：
自建隔离实例 `:8062`（自己的库/设置/密钥文件，不动用户的 `:8000/:3000/:8061`），
真 DeepSeek 模型驱动一条 `echo boundary-m1b-*` 的任务，命令真的执行、事件里真的回显了那串标记，
再断言边界事实。装置带前置——`run_shell` 没真跑就直接 FAIL，关档那次就因为这条变了红。

- **M1a**：Job Object 接进执行层，`memory_bytes/pids_limit` 由内核执行（实测 512MiB / 128）。
  判据：`tests/test_windows_job_v19.py` 6/6 + `E:/tmp/realmodel/check_m1a.py` 16/16。
- **M1b**：`boundary_strength` 成为一等公民。链路一层都不许丢：
  `boundary.measure()`（现场探测，60s TTL）→ `runner._enforcement` / `limits_not_applied`（同一套 18 键）
  → `gateway._command_enforcement`（15 键白名单，宿主路径与命令输出搭不上这趟车）
  → `tool.completed` 事件 + 证据包（按 `tool_call_id` 贴回）+ `turn.context` 治理快照
  → 前端 `timeline.ts` 读事实、`status.ts` 出角标、`api.ts` 出面板行。
  契约：**键不存在 = 这次没起进程**（只读工具、被拒调用、老任务），`sandbox_backend: null` = 进程真落在宿主上。
  缺键就渲染默认值，"未知"会长成"没强制"或"已强制"——两种都是谎。
- **M1c**：未强制的限额停止以真值展示。面板逐条标「（未强制）」（`cpus`/`tmpfs` 标，
  `memory`/`pids` 在 Job 档不标，`timeout_seconds` 由自家计时器执行所以不给它这一档）；
  角标分两半：开档「宿主执行 + 内核限额（内存/进程数/进程树）· 文件与网络未强制」，
  关档「宿主执行（未隔离，限额未生效）」，老任务只说「宿主执行（未隔离）」、不编造结论。

验收计数（全部从产物里取，不是模型自报）：
真链路 off 11/11、on 14/14；浏览器点验 22/22（Chrome headless 1440×900，前端 `:3122` 代理 `:8062`，
ON/OFF/CTRL 三桩同跑，CTRL 用改动之前跑的老任务当负对照：它必须**既拿不到**「内核限额」**也拿不到**「限额未生效」）；
web 113/113 + `tsc --noEmit` rc=0；API 全量 760 passed / 14 skipped，
`test_boundary_facts_v20.py` + `test_governance_snapshot_v16.py` 29 passed。

这套验收在交付过程中抓出并修掉了三处真缺陷（每条都有变异反证，改坏就变红）：

1. **治理指纹把探测年龄当成了策略**：`boundary_measured_seconds_ago` 进了 `digest()`，
   同一套策略每隔几步就被判成"变了"，`turn.context` 去重间歇失效。修法是 `VOLATILE_SANDBOX_KEYS` 剔除；
   把该元组置空 → 两条新回归测试确定性地红。
2. **档位冒充本次的强制**：关档页面同时写着「沙箱未启用」和「只有内存/进程数/进程树被内核强制」。
   `boundary_strength` 说的是这台机器拦得住什么，不是这次执行被拦了什么；
   现在 `enforced` 无一项为真时这一格改说「本次未强制任何限额」。
   **单元测试放过了这句谎，是浏览器点验抓出来的**——这条直接证明了点验门禁不是走过场。
   变异：`enforcedAny` 写死 `true` → 单元 1 红 + 点验 2 红。
3. **代号被当成沙箱名**：开档面板印出「local · 宿主直连…」，读者会以为有一种沙箱叫 local
   （本立项要消灭的就是这句）。改为「无容器（进程在本机）」；docker 那格仍印代号，因为它真是容器。
   变异：改回 raw backend → 单元 1 红 + 点验 1 红。

仍在 `os_enforced` 档才可能绿的：P-READ / P-WRITE（§7 已注明本机必然红，界面与证据包如实显示"未由系统强制"）、
P-NET。CPU/磁盘按 §8 不做，界面上永远标"未强制"。桌面静态导出 `out/` 里的边界行是改动前的文案，
随下一次 `BLANK_DESKTOP_BUILD=1 next build` 一起带走。

## 8. 明确不做

- **不承诺"防恶意代码"**：AppContainer + 受限令牌防的是"模型犯错与越界"，不是防一个铁了心的本地 rootkit。
  文档与界面都要这么写，不能写成"安全沙箱"。
- **不做 Windows 专用账号 + DPAPI**（Codex 那半截）：需要管理员创建用户，且引入"装个软件要输管理员口令"
  的产品体验。M0 还额外证明它在**当前形状**下连 API 层都走不通（P5.3 两次 `1314 ERROR_PRIVILEGE_NOT_HELD`，
  P5.2 造不出第二枚令牌），所以这条不是"我们选择不做"，是"这台机器不做不了"——两者都写进证据包。
- **不在本期做 CPU 硬配额**：非管理员拿不到可靠的 job CPU 限速，宁可标注"未强制"。
- **不为了"三档看起来都可用"而在 `workspace-net` 不可行时假装它可用**：做不到就不提供这一档。

## 9. 未决（需要用户裁决或外部信息）

1. `off` 档要不要保留？保留 = 承认"用户可能想要完整权限"；删掉 = 一部分任务（需要装系统级依赖）直接不可用。
2. `fs-broker` 子进程会让每次文件工具调用多一次 IPC。是否接受这个延迟换边界？
3. MCP 强制进边界可能打断现有可用配置（第三方进程未必能在 AppContainer 里起）。
   是"进不去就禁用该 MCP 并说明"，还是"允许它留在宿主 + 面板红字标注"？
4. 产品默认档：`workspace` 是默认，但今天桌面装机常态是 `off`（`safety.py:156-158`）。
   改默认会让既有用户的命令突然跑不通——需要一次显式的迁移与告知。
5. **M0 之后的新问题（需要裁决）**：本机只有 `job_only`。三选一——
   (a) 默认 `job_only` + 界面写清"文件边界未由系统强制"，想要 `os_enforced` 的用户自己以管理员运行，
   `blank doctor --sandbox` 告诉他差异在哪；
   (b) 提供一次性提权安装步骤（建一个 AC 档案 / 建专用账号），把边界做成真的，代价是安装体验变重；
   (c) 在非管理员 Windows 上**不提供**命令执行沙箱档，只跑审批模式。
   我的默认是 (a)：它不改安装体验、不撒谎，且 (b)(c) 都能作为 (a) 的后续叠加。
   **（a）的界面前提已在 M1c 交付**（§7.1）：面板逐条标「未强制」、角标说清 Job 档只强制三项、
   关档不再出现任何强制承诺。所以这一条现在只差裁决，不再差实现。

## 10. 复现命令

```bash
# 现状取证（本文件的 §1 全部结论可复跑）
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -c "
import sys; sys.path.insert(0,'src')
from blank_api.services import safety as S
for c in [r'cat C:\Users\22103\.ssh\id_rsa', r'echo hi > C:\Users\22103\out.txt',
          r'python -c \"open(r\'C:\Users\22103\.ssh\id_rsa\').read()\"']:
    print(repr(c), '->', S._candidate_paths(c))"                       # 第三条必须是 []：字符串里的路径看不见
grep -n "BLANK_SANDBOX_ENABLED" services/api/src/blank_api/services/sandbox/policy.py \
  services/api/src/blank_api/services/safety.py                        # 两处默认值冲突（true vs false）
grep -n "docker_flags" -A 30 services/api/src/blank_api/services/sandbox/policy.py   # 限额唯一落点
grep -n "class LocalSandbox" -A 55 services/api/src/blank_api/services/sandbox/runner.py  # 只有超时+杀进程树
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe evals/boundary_probe.py  # 六项 PASS：今天的边界形状

# M0 探针（已建，29 条判定；进程退出码就是判据：出现 UNKNOWN 返回 1）
PYTHONIOENCODING=utf-8 services/api/.venv/Scripts/python.exe -X utf8 evals/win_isolation_probe.py \
  --json E:/tmp/acprobe/m0-report.json           # P0 环境 / P1 AC 身份 / P2 ACL / P3 进 AC / P4 行为 / P5 令牌 / P6 Job
# 跨进程树对照（排除"是 Qoder/uv 包装层坏了"这个假设）：由任务计划程序起同一份载荷。
# 这个脚本**不进仓库**（它会注册一个一次性计划任务，用完自己删），在工件目录里：
# E:/tmp/realmodel/ac_detached_experiment.py + ac_detached_payload.py，输出 detached-in-tree.txt / detached-detached-task.txt

# —— M1 复现（§7.1 的每一条计数都能这样重跑）——
# 1) 单元/契约层：档位、事件白名单、证据包同事实、治理快照同来源、探测年龄不进指纹
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest \
  tests/test_boundary_facts_v20.py tests/test_governance_snapshot_v16.py tests/test_windows_job_v19.py -q
# 2) 前端判定层：角标三档文案、缺键不许编造、面板逐条「未强制」
cd web && PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" \
  node --test --import ./test-bootstrap.mjs "src/components/tasks/*.test.ts"
# 3) 真链路：自建隔离实例（库/设置/密钥都是自己的，:8062），真模型跑一条 echo
PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py prep
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m1b/instance.py start off   # 或 on（开档 + allow_local，真建 Job）
BLANK_CHECK_MODE=off BASE=http://127.0.0.1:8062/api/v1 PYTHONIOENCODING=utf-8 \
  services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/realmodel/check_m1b.py     # on 档换 BLANK_CHECK_MODE=on
# 4) 真浏览器点验：前端 :3122 代理到 :8062，1440x900，ON/OFF/CTRL 三桩同跑
cd web && API_PROXY_TARGET=http://127.0.0.1:8062 BLANK_WEB_DIST_DIR=.next-8062 \
  PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" node ./node_modules/next/dist/bin/next dev -p 3122
PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" node E:/tmp/m1b/boundary-verify.mjs
# 变异反证（装置必须能红）：把 api.ts 里 enforcedAny 写死 true → 单元 1 红 + 点验 2 红；
# 把「无容器（进程在本机）」改回 raw backend → 单元 1 红 + 点验 1 红。
```
