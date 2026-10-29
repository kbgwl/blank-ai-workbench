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
- **M3 全 Agent 进边界**（已完成，交付记录见 §7.3）：文件工具、子代理、团队、MCP 走同一边界；
  `sandbox_mode` 进用量与事件侧。
  退出判据：一条真跑的任务里，**每个** `agent_path` 都能查出它所在的档位；跨档降档必须失败。
- **M4 自证产品化**（本机部分已完成，交付记录见 §7.4）：`blank doctor --sandbox` +
  证据包里的边界报告 + 安装包内的探针。
  退出判据：在**第二台**机器（不同权限形状）上跑通，输出与本机差异可解释。
  **第二台机器本批没有**：探针与差异载体已交付，这条判据仍未验，见 §7.4 末段。

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

### 7.2 M2 交付记录（2026-10-24）：三档 × 四模式矩阵

判据按 §7 原文逐条对：**12 格每格都有可失败断言**，四条探针都有确定结论。
装置全部是真执行层（`execute_command` → `LocalSandbox` → Job），没有替身。

- **12 格矩阵**（`tests/test_sandbox_matrix_v21.py::test_matrix_cell`，3 档 × 4 模式）。
  每格同时钉两半：
  - 模式侧：`plan` 对 shell/写补丁一律 `DENY`；`accept_edits` 自动放行补丁且规则名为
    `mode:accept_edits`；`bypass` 仍对不可逆命令 `ASK`（`irreversible`）——免问不等于关边界；
    `default` 中风险走 `risk:auto`。
  - 档位侧：`off` → `sandbox is None`、`limits_applied False`、提示含「无隔离」；
    `workspace` → `sandbox == "local"`、`degraded True`、`filesystem_enforced/network_enforced` 恒 `False`、
    本机 `limits_applied True`；`workspace-net` → `tier_blocked True`、限额全 False、提示含
    「不会降级成 off」。
- **拒绝而不是降级**：`workspace-net` 在 `job_only` 机器上返回 `tier_blocked`，网关转成
  `sandbox_blocked` 事件、**不带** `sandbox_backend` 键。这条键的有无就是「命令没跑」与
  「命令落在宿主上」的分界（对照格：`off` 真跑时 `sandbox_backend` 存在且为 `null`）。
- **授权按档位分桶**：`(tier, tool, target_path_prefix)` 三元组。高风险 `python -c` 在 `off`
  档批准后，切回 `workspace` 必须重新 `ASK`；`sk-` 密钥补丁的路径前缀只覆盖审批时的
  `src/a.py`，换路径不得复用。
- **装配层**：非法档 422、`workspace-net` 409；设置里的非法值回落 `workspace`；
  `task.started` 携带 `sandbox.requested/tier/strength/degraded/notice/reason`，
  resume 从 `task.started.payload.sandbox.requested` 读回档位——重启后档位不靠猜。
- **P-NET**：沙箱内 Python 连本机监听端**成功**，事件同时写 `network_enforced False`。
  断言 `connected is (not network_enforced)`，两者不一致就红。本机只能证明「没隔离」那一侧，
  AC 出网默认拒无法验证——§7 已注明。
- **P-PROC**：同一命令 `pids=128` 正常（阳性对照），`pids=1` 时解释器起不来且
  `pids_limit == 1`。限额由内核执行，不是账上写的。
- **P-ESCAPE**：`job_only` 下脚本成功写好工作区外文件，事件诚实写 `filesystem_enforced False`。
  写成即证明，失败即证明真的拦住了——两边都不允许含糊。
- **P-INHERIT**：`off` 档孙进程持续写心跳（阳性对照，测试自行 `taskkill` 收尾）；
  `workspace` 档父进程退出、Job 关闭后心跳大小**不再增长**。对照组先证明装置本身能写出字，
  否则「心跳停了」可能只是脚本没跑起来。

交付过程中抓出并修掉一处**真实运行缺陷**：`DEFAULT_ENV_ALLOWLIST` 少了 `SYSTEMROOT`，
沙箱内 Python 起 socket 直接 `WinError 10106`（Winsock 初始化失败）。P-NET 第一次红就是它——
被误读成「网络边界生效」之前，先看的是连接端根本没收到数据。补入
`SYSTEMROOT/SYSTEMDRIVE/WINDIR/COMSPEC/PATHEXT/TEMP/TMP/USERPROFILE` 后 P-NET 通过；
这不是放宽网络策略，网络仍然 `allowed`，只是别让操作系统管道变量缺失冒充边界。

同一轮把 `off` 档的文案谎话清掉：`backend_info()` 关档时返回 `backend="none"`、
`label="无隔离（宿主完整权限）"`，且不再构造任何后端；后端不可用时保留可操作原因
（Docker / `BLANK_SANDBOX_ALLOW_LOCAL=1`）。档位事实（`sandbox_tier/requested/degraded/
tier_available/tier_reason/tier_notice`）与执行账（15 键）在事件里分成两组白名单，
`test_boundary_facts_v20.py` 按组断言，面板只读这些键。

**变异反证**（每次 `cp` 备份、跑完 `cmp` 字节还原）：

| # | 变异 | 预期 | 实测 |
|---|---|---|---|
| A | `shell.py` 去掉 `not resolution.available` 阻断 | 所有 `workspace-net` 格 + 阻断事件红 | 精确 5 红 |
| B1 | `permissions.decide` 去掉档位相等比较 | 换档仍复用批准 | 1 红（tier_change） |
| B2 | `_covers_path` 恒 `True` | 路径前缀失效 | 1 红（path_scoped） |
| C | `active_process_limit=0` | 账上不再声称限额 | 1 红（P-PROC 对照） |
| D | Job 记录限额但不设 `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` | `pids=1` 仍能起进程 | 1 红，报「pids=1 时第二个进程仍然起得来」 |
| E | `close_job` 变空操作 | 孙进程继续写心跳 | 1 红，`63 != 27` 字节 |
| F | `off` 档 `label` 改回「本机受限执行」 | 文案谎话回归 | 1 红 |

C 与 D 分开做是有意的：C 证明「限额没接进 Job」会被账本抓到，D 证明「账本声称了但内核
没执行」也会被抓到——后者才是更难发现的那种谎。

计数（从产物取）：`test_sandbox_matrix_v21.py` 27 passed；
受影响九套 + 矩阵合计 **159 passed / 14 skipped**。`workspace-net` 不在本机提供，
§9 第 5 条因此按 (a) 落地：默认 `job_only` + 界面与证据包如实写「文件/网络未由系统强制」，
需要 `os_enforced` 的环境以管理员运行并用 `blank doctor --sandbox` 看差异（M4）。

### 7.3 M3 交付记录（2026-10-24）：整条执行面进同一套边界账

M2 只把**命令类工具**钉在档位上。M3 的退出判据是两句：**每个 `agent_path` 都能查出
它当时在哪一档**；**跨档降档必须失败**。落地时把"边界"拆成两个互不冒充的事实：

- `sandbox_mode`：这次调用按哪一档跑（`off` / `workspace` / `workspace-net`）。
- `boundary_kind`：这次调用实际落在哪一层——
  `in_process`（API 进程内，如文件读写）、`process`（我们起的子进程，受档位约束）、
  `mcp_host_process`（外部 MCP 服务端；#94 之前一律按宿主进程记账，之后 stdio 服务端
  自身已挂进 Job，这个值只表示"调用走外部服务端"，见 §7.5）、
  `blocked`（档位不可用，没执行）。

**档位只能收紧，不能变宽。** 排序 `off(2) > workspace-net(1) > workspace(0)` 落在
`tiers.PERMISSIVENESS` / `assert_not_looser`；任一方为空（当时没记）不冒充知道。三个入口：

- `AgentRuntime.run()`：同一任务第二次运行换到更宽的档位 → 409 `sandbox_tier_downgrade`，
  闸门在装配之前，被拒的那次**一行事件都不写**；
- `AgentRuntime.resume()`：显式带档位时同样过闸；不带则沿用
  `task.started.payload.sandbox.requested`（与 M2 的恢复口径同源）；
- `run_subagent()`：收到比 `parent_tier` 更宽的档位直接返回 `ok=False`，
  且守卫在**任何模型调用之前**——不给"先花一次钱再拒绝"留口子。

**团队群是档位的第二处真源。** `team_groups.sandbox_tier` 建群时解析并落库
（不可用 → 409 `sandbox_tier_unavailable`，不留半个群），成员 Task 一律继承；
群摘要把它读回来，事件侧每个成员任务的 `task.started.sandbox.requested` 都应是这一档。

**用量账。** `model_usage.sandbox_mode` + `model.usage.attribution.sandbox_mode`：
主代理、子代理、团队成员走同一个 `account_call`，空串只表示"这一列上线前的老行"。
证据包 `usage.sandbox_modes` 给去重集合，`sandbox_mode_unattributed_calls` 单独计数。

**每个 `tool.completed` 都带** `sandbox_mode` 与 `boundary_kind`——包括只读工具：
只读也有边界，只是边界在进程内。审计账（`tool-calls.jsonl`）的 `detail` 写同一对值，
事件与审计账可以逐条对账。键的语义沿用 M1：`sandbox_backend` 缺席=没起进程；
值为 `null`=进程落在宿主上；`sandbox_blocked`=档位不可用未执行。

**MCP 边界必须单独说。** `MCPStatus.as_dict()` / `MCPBundle.as_dict()` 的 `boundary`
在 #94 之后有四个真事实：`job_object`（stdio 服务端已挂进 Job）、
`host_process_unbounded`（挂不进去，红字，仍可连接）、`remote`（HTTP 型，进程在远端）、
`none`（没连上）/ `unknown`（连接失败，不知道在哪跑）。汇总取最坏一条，并给出
`unbounded_servers` 与 `by_boundary`；`task.started.mcp` 原样带走，供证据包读。
工具名 `mcp__*` 的调用在事件里仍记 `mcp_host_process`（调用走的是外部服务端，
不等于它没进 Job）。详见 §7.5。

**证据包新增 `boundaries`**：`sandbox`（档位/后端/强度/文件与网络是否强制/提示）、
`mcp`（边界、说明、服务端清单、`calls` 计数）、`tool_calls` 按 `boundary_kind` 计数、
`sandbox_modes`（事件里实际出现过的档位）。`tools[]` 每条也贴回
`boundary_kind/sandbox_mode`；老任务没有这个键的调用落进 `unattributed`，
不从工具名反推成 `in_process`。

**迁移 0025**：`model_usage.sandbox_mode`、`team_groups.sandbox_tier`，`VARCHAR(20)`、
`NOT NULL`、`server_default=''`；老行保持空串，不许补成 `workspace`。
`tests/test_agent_boundary_m3_v46.py` 用真 alembic 升级到 0024 → 插老行 → 升 head →
断言老行仍是空串 → 降回 0024 且行数不丢。

**判据与计数**（从产物取）：`test_agent_boundary_m3_v46.py` 9 passed
（档位排序、运行入口 409、子代理拒绝、子代理用量行、四种边界种类、证据包汇总、
MCP 三态、用量账、迁移往返）；`test_team.py` 17 passed（含群档位继承与建群拒绝）；
受影响十一套 + 新套件合计 **224 passed**；API 全量实跑 **1075 passed / 23 skipped**
（2026-10-24，5 分 11 秒），无新增失败。

**变异反证**（每次 `cp` 备份、跑完 `cmp` 字节还原）：

| # | 变异 | 预期 | 实测 |
|---|---|---|---|
| A | `runtime.run` 去掉档位闸门 | 第二次换 off 仍 200 | 1 红（期望 409 实得 200） |
| B | `run_subagent` 用自身当父级（等于去掉继承守卫） | 更宽档位仍继续执行 | 1 红（走到适配器才发现） |
| C | `account_call` 不写行上的 `sandbox_mode` | 行上读回空串 | 2 红（`['',''] != ['off','off']`） |
| D | 网关不写 `boundary_kind` | 事件缺边界种类 | 2 红（工具事件 + 证据包计数） |
| E | `MCPStatus.as_dict` 去掉 `boundary` | 状态无法回答在哪跑 | 1 红（KeyError） |
| F | 证据包不返回 `boundaries` | 报告整块消失 | 1 红（KeyError） |
| G | 团队 `_request` 不传群档位 | 成员任务回落默认档 | 1 红（`off != workspace`） |
| H | 建群时不检查档位可用性 | 不可用档也建出群 | 1 红（201 != 409） |

**诚实的缺口**：

- `in_process` 不经过 Job，路径边界仍靠 `SafetyPolicy` 的路径闸门与审批——这不是
  新漏洞，只是 M3 把它**记成了事实**，不再混进"没起进程所以没有边界"。
- HTTP 型 MCP 的"进程"其实在远端；`remote` 描述的是"不归本机任务边界管"这个
  共同事实，不声称部署位置。#94 之后 stdio 服务端不再是"一律宿主"：能挂进 Job 的
  就是 `job_object`，挂不进去才是 `host_process_unbounded`（红字）。
- `boundary_kind` 从 M3 起才有；老任务一律 `unattributed`，界面不得按工具名补猜。

### 7.4 M4 交付记录（2026-10-24）：把"我们说安全"变成"你自己跑一遍"

**入口**：`blank doctor --sandbox`（`packages/cli/src/index.mjs` 转发到
`python -m blank_api.doctor`，用同一个包的 `services/api/src`）。也可直接
`python -m blank_api.doctor --sandbox --json <path>`。`--task <id> --api <url>`
额外核对一条真跑任务的证据包（P-AUDIT）。

**八条边界全部真执行**，不是读配置猜：在 `workspace` 档里跑子进程，逐个尝试
读工作区外文件、写工作区外文件、读 `HKCU\Environment`、连本机监听端、越限额起第二个
进程、用 `icacls` 给自己加 ACE、让孙进程在父进程退出后继续写心跳；P-AUDIT 则直接读
证据包。每条结论只有四个词：`enforced` / `not_enforced` / `unknown` / `not_tested`。
探针装置自带阳性对照（监听端必须先收到 doctor 自己的连接；pids 必须先跑 128 的对照组；
无 Job 对照组的孙进程必须先写出心跳），对照失败一律 `unknown`，不许冒充结论。

**行为和账本对质**：每条探针走的就是 `execute_command`，报告里同时写本次执行的
`filesystem_enforced` / `network_enforced` / `pids_enforced` / `tree_kill_enforced`。
行为说"没拦住"而账本说"已强制"（或反过来）时 `consistent=false`，doctor 退出码 1。
`unknown` 也退出 1；`not_tested`（容器后端、未给任务号）不算失败，但必须在报告里写明原因。

**本机实测**（job_only，装置可信）：P-READ / P-WRITE / P-ENV / P-NET / P-ESCAPE 五条
`not_enforced`（全部与账本一致），P-PROC / P-INHERIT `enforced`（pids=1 第二个进程起不来；
Job 关闭后孙进程心跳停止），P-AUDIT 在隔离实例 `:8061` 上跑一条真任务后 `verified`
（`1 条工具调用都带边界字段，计数一致`）。同一命令指向 M3 之前的老任务时，
P-AUDIT 报"证据链不成立"并退出 1——这是正例的负对照，不是文案。

**输出**：人读的表格 + `--json` 完整报告（`capability`、三档解析、`probe_enforcement`、
八条 `boundaries`、`summary`、`exit_code`）。差异可解释性就落在这份 JSON 里：
换一台机器，`capability.strength`、`apparatus_ok`、三档 `available/degraded/reason`
和八条状态会一起变，不需要重新解释一遍文案。

**安装包内的探针**：`desktop/verify-package.cjs` 新增 3b 段，用包内解释器
`python -m blank_api.doctor --sandbox` 真跑一遍，断言装置可信、八条都跑了、无 `unknown`。
`electron-builder --dir` 后实跑 21 项全过；把包内 `doctor.py` 临时改名做负对照，
该段立即报"包内边界探针执行失败"、verify 退出 1，改名恢复后 `cmp` 字节一致。
顺带记下打包坑：electron-builder 的 npm 依赖收集需要 node/npm 在 `PATH`，只给
`node.exe` 绝对路径会以 "No JSON content found in output" 失败（构建日志有、退出码非 0）。

**判据与计数**：`tests/test_doctor_v47.py` 7 passed（拒绝/超时/对照失败的状态归属、
P-AUDIT 五种缺字段、退出码、本机八条对质、无后端时不硬跑）；API 全量实跑
**1082 passed / 23 skipped**（2026-10-24，4 分 57 秒，rc=0，无新增跳过）；5 条变异全部实测红并
`cmp` 字节还原：

| # | 变异 | 预期 | 实测 |
|---|---|---|---|
| A | 任何非零退出都算 `enforced` | 脚本语法错误被读成"拦住" | 1 红 |
| B | `pids_verdict` 不检查对照组 | 装置坏了也出限额结论 | 1 红 |
| C | 审计不检查 `boundary_kind` | 老任务被误判"已核" | 1 红 |
| D | P-READ 无条件报 `enforced` | 行为与账本对不上 | 1 红 |
| E | 退出码忽略 `unknown/inconsistent` | doctor 假绿 | 1 红 |

**诚实的缺口**：

- **第二台机器没有**：退出判据原文要求"在第二台机器上跑通，输出与本机差异可解释"。
  本批只交付了可复现的差异载体（doctor JSON + `verify-package` 的同一段），
  没有第二台机器就是没有，不能拿本机四次重跑冒充。
- Docker 后端的八条行为探针一律 `not_tested`：doctor 不越界探测容器内路径；容器边界
  的证据是 `policy.docker_flags` 与运行时合同，不是本机行为自证。
- P-ENV 在 AppContainer 下可能读到被虚拟化的 `HKCU` 视图；若那天出现"行为未拦住、
  账本说强制"，doctor 会报不一致并退出 1，需要人看报告而不是自动解释掉。
- P-AUDIT 只对 M3 之后的任务成立；老任务的 `unattributed` 会让它红，这是有意的。

### 7.5 #94 交付记录（2026-10-26）：MCP stdio 服务端进 Job，进不去的红字

**缺口形状**：M3 把 MCP 调用记成 `mcp_host_process`，那只是"不归任务边界管"的记账，
不是边界本身。stdio 服务端是我们自己 `create_subprocess_exec` 起来的进程，却没挂
Job——内存、进程数、退出清理全都不由内核强制；状态与证据包只有 `host_process`
一个词，连"能不能挂"都没回答。

**落地**（`services/mcp/client.py`、`services/mcp/bridge.py`、`services/rea/adapter.py`）：

- `StdioClient.start()` 在 spawn 之后、握手之前建 Job 并 `AssignProcessToJobObject`。
  限额默认取 `SandboxPolicy`（`512m` / `128`），`mcp.json` 每个服务端可用
  `memory` / `pids` 覆盖；认不出的单位退回默认值，**不许悄悄变成"不限"**。
  握手失败、连接关闭都必须关 Job 句柄（`KILL_ON_JOB_CLOSE` 才会带走整棵树）。
- 边界事实不是猜的：`IsProcessInJob` 直接问内核，结果连同
  `job_created/job_assigned/job_error/memory_enforced/memory_bytes/pids_enforced/
  pids_limit/tree_kill_enforced/note` 一起快照；关句柄之后快照不变，账上说的仍是
  这次真实生效的边界。
- 词表：`job_object` / `host_process_unbounded` / `remote` / `mixed` / `none` /
  `unknown`。挂不进去**不拦任务**（坏服务端不该让整个任务起不来），但 note 以 `⚠`
  开头，状态、汇总、证据包、界面一起标红；HTTP 型一律 `remote`，不许写成 Job。
- 汇总取最坏：任一个服务端没进 Job，整包就是 `host_process_unbounded`，并列出
  `unbounded_servers`；`by_boundary` 给分布。证据包 `boundaries.mcp` 原样带走，
  REA adapter 也改读真实 facts（此前固定写"宿主进程"）。
- **close 的顺序被钉住了**：先关 Job，再等进程。反过来先 `await process.wait()` 会
  白等——`wait()` 要等所有 stdio 管道断开，而孙子进程可能正握着 stdout。实测旧顺序
  每次都吃满 `KILL_GRACE=2.0s`，新顺序 0.00–0.03s。没进 Job 的服务端，其孙子不归
  我们管，close 的等待有界，不许把收尾挂死。

**判据与计数**：`tests/test_mcp_job_v94.py` **11 passed**（10 轮重跑 + warnings 升
error 全绿，整文件 0.64s）；相关七套 **130 passed**；API 全量 **1314 passed /
23 skipped**（2026-10-26，6 分 53 秒，rc=0）；web **194 passed**，`tsc --noEmit` rc=0。

| 判据 | 阳性 | 阴性/对照 |
|---|---|---|
| 真的在 Job 里 | `IsProcessInJob(mcp pid)` 为 True | 同一时刻无关进程在同一 Job 上必须 False |
| 限额由内核强制 | 768m 的 Job 里分配 256MB 返回 `hogged:268435456` | 128m 的 Job 里同一调用抛 `MCPError`，进程 rc≠0 且 stderr 含 `MemoryError` |
| 关连接＝整棵树走 | 孙子在**工具调用里**出生，`IsProcessInJob` 为 True；close 后 5s 内死 | 孙子的出生点若在赋值窗口外，这条就退化成竞态，故不许在启动时派生 |
| 挂不进去 | 注入 create/assign 失败后仍可 `list_tools` | facts/status/bundle/evidence 全是 `host_process_unbounded` + `⚠` + 注入原因 |
| HTTP | `remote` + "进程在远端"说明 | 不许出现 `job_created=True` |
| mcp.json 限额 | `memory=320m`、`pids=12` 落到真 Job facts | 垃圾单位读回空串＝用默认值 |

**变异电池**（`E:/tmp/m94/mutate_mcp.py`：每条靶复制一份 `src`、改一处、
`PYTHONPATH` 指副本并**先证明解释器加载的是副本**；对照轮不改字节；结束核对仓库
源码 sha256 未变）。对照轮 11 passed / rc=0，9 条靶全部实测红，`check` 逐字比对后
`[ALL-OK] … 源码未变`、rc=0：

| 靶 | 实测红集合（与写死期望逐字相等） | 失败原因（实测） |
|---|---|---|
| M1 建 Job 不 assign、账上仍写已挂 | 清树、赋值失败标注、内存限额、Job 成员 | Job 成员断言（`inside is True`）变红；128m 下分配未失败（`DID NOT RAISE MCPError`） |
| M2 关连接不关 Job 句柄 | 清树 | `close 吃满 2.02s 宽限期` |
| M3 挂失败仍报 `job_object` | 创建失败、赋值失败 | `'job_object' == 'host_process_unbounded'` |
| M4 HTTP 写成已挂 Job | HTTP 边界 | `'job_object' == 'remote'` |
| M5 状态忽略客户端事实、按传输猜 | 管理器读数 | `'host_process_unbounded' == 'job_object'` |
| M6 汇总忽略未进 Job 的服务端 | 汇总、证据、创建失败、赋值失败 | `'mixed' == 'host_process_unbounded'` |
| M7 证据包丢掉未进 Job 名单 | 证据透传 | `[] == ['bad']` |
| M8 无视 mcp.json 的 memory | 管理器、内存限额、Job 成员 | `536870912 == 268435456`（同理 320m） |
| M9 先等进程再关 Job | 清树 | `close 吃满 2.00s 宽限期` |

**真浏览器点验**（`E:/tmp/m94/settings_mcp_verify.mjs`，2026-10-26）：Chrome
headless 1440x1000 打 `:3121/settings#tools`（前端 dev、代理到隔离实例 `:8061`），
配置同时放 `fake-stdio`（`256m`/`32`，stdout 走 Job）与 `remote-stub`（HTTP）。
12 条判定 **ALL PASS**，读数现取自 `GET /settings/mcp`（256MB、32 个进程、2 个已连接、
5 个工具），不是写死的：

- stdio 行：`fake-stdio 已连接 stdio 4 个工具 · fake 已挂 Job：内存 256 MB / 32 个进程 / 关连接带走整棵树`
- remote 行：`remote-stub 已连接 http 1 个工具 · remote-stub 进程在远端，本机 Job 管不到；出站请求仍走审批与审计`
- 负对照：remote 行不许出现「已挂 Job」；按不存在的名字定位必须找不到行（定位器自检）
- **装置能红**：去掉 `boundary === "job_object"` 这个前置条件（让远端行也渲染 Job 行）后
  恰好多出 1 条 FAIL（`负对照：remote 行不许出现「已挂 Job」`），rc=1；还原后复读
  ALL PASS、rc=0。截图 `E:/tmp/m94/settings-mcp.png`。
- 点验期间还发现 HTTP 桩自己的分帧缺陷：`http.server` 默认 HTTP/1.0，202 又没给
  `content-length`，httpx 读成 `ReadError`——这是桩的问题，改成 HTTP/1.1 + 显式长度后
  `remote` 行即正常；被测代码没有因此改动。

**诚实的缺口**：

- **赋值窗口消不掉**：spawn 之后才能挂 Job，窗口里已派生的孙子不在 Job。这与命令
  沙箱同源（`windows_job.assign_pid` 的 docstring 写明了），本批只是把 stdio 服务端
  也放进了这条路，没有声称严格。判据因此把孙子的出生点放进工具调用，避免用竞态
  冒充证据。
- `JOB_OBJECT_LIMIT_PROCESS_MEMORY` 是**每进程**上限，不是整个服务端树的总量；
  服务端再派生多个子进程，每个各有一份。
- Job 只提供内存/进程数/生命周期，**不是 AppContainer**：MCP 服务端的文件与网络
  边界仍按宿主权限，和命令沙箱的三档不是一回事；界面文案不许写成"已沙箱"。
- 挂不进去时服务端照常以宿主权限运行——这是产品选择（不因坏配置拦住任务），
  红字是补偿，不是强制。
- 边界外（`host_process_unbounded`）的孙子进程不归我们清理；close 有界返回，可能
  留下未关闭的管道直到 GC。全量套件里既有的 fs-broker 传输告警与本套无关。

### 7.6 #76 现状（2026-10-27）：第二台机器还没来，装置与基线先钉住

**这条没完成，也不装完成。** #76 要的是另一台 Windows 机器上的真实报告；本机重跑
多少次都不算——对账器自己会拒绝：两份报告的 `host.host_id` 相同就判 `same_host` 并
返回 1。本轮把除"那台机器"以外的部分都做实了：

- **机 A 基线**（`E:/tmp/m76/machine-a.json`，2026-10-27，非管理员，
  `BLANK_SANDBOX_ALLOW_LOCAL=1`）：`schema=2`、`job_only`、`apparatus_ok=true`；
  P-PROC / P-INHERIT **拦住**，P-READ / P-WRITE / P-ENV / P-NET / P-ESCAPE **未强制**，
  P-AUDIT **未测**（未传 `--task`）；doctor 自身 rc=0（未测不算失败）。
- **第二台机器的一键包**：`E:/tmp/m76/run-on-machine-b.ps1`（UTF-8 BOM，PowerShell 5.1
  可解析）只做只读探针，打印提权状态、报告 sha256 和"把 JSON 拷回机 A"的提示；
  用法与判据写在 `E:/tmp/m76/README-machine-b.md`。
- **对账装置自检**（`E:/tmp/m76/compare-check.py`，用真实基线派生，5/5 PASS）：
  1. 基线形状有效（schema≥2 / 装置可信 / 有 host_id / 八条探针）；
  2. 同机两份 → `same_host` + rc=1（**不许把本机重跑冒充跨机**）；
  3. 只换 host_id、事实全同 → `consistent` + rc=0 + 零差异（**不许凭空造差异**）；
  4. host_id + `probe_enforcement.pids_enforced` + P-PROC 一起变 → `differences_explained`
     + rc=0，P-PROC 归因到 `pids_enforced`（归因链真的接上）；
  5. host_id + 只改 P-AUDIT（跑法标志不动）→ `unexplained_difference` + rc=1
     （**装置必须能红**）。
- **CLI 级复核**：用上面的 runner 在本机又跑了一遍（`same-machine-rerun.json`），
  `doctor --compare machine-a.json same-machine-rerun.json` 输出 `same_host`、rc=1，
  说明"换台机器才算数"这条不是只活在单测里。

第二台机器到了以后，按 README 第 2 节对账，读数落进 §10 的复现命令；`consistent` 或
`differences_explained` 且 rc=0 才能把 #76 收口，`unexplained_difference` 要补实测
归因表，不能改期望迁就。

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
   **#94 已按后者落地**：stdio 能挂 Job 就挂，挂不进去照常可用但状态/证据/界面标
   `host_process_unbounded`；这条不再是未决项。
4. 产品默认档：`workspace` 是默认，但桌面壳仍然传 `BLANK_SANDBOX_ENABLED=false`（见第 6 条），
   桌面装机常态还是 `off`。把桌面壳也改成开档会让既有用户的命令突然换一种边界，
   需要一次显式的迁移与告知——**这一条仍未决**。
5. **M0 之后的新问题（已在 M2 按 (a) 落地）**：本机只有 `job_only`。三选一——
   (a) 默认 `job_only` + 界面写清"文件边界未由系统强制"，想要 `os_enforced` 的用户自己以管理员运行，
   `blank doctor --sandbox` 告诉他差异在哪；
   (b) 提供一次性提权安装步骤（建一个 AC 档案 / 建专用账号），把边界做成真的，代价是安装体验变重；
   (c) 在非管理员 Windows 上**不提供**命令执行沙箱档，只跑审批模式。
   采纳 (a)：它不改安装体验、不撒谎，且 (b)(c) 都能作为 (a) 的后续叠加。
   `workspace-net` 在本机被**拒绝**（`tier_blocked`）而不是降级成 `off`——`off` 的权限严格更大，
   悄悄降级等于在用户不知情时扩权。界面前提已在 M1c 交付，M2 补齐了 12 格矩阵与四条探针（§7.2）。
6. **已裁决（#144，2026-10-29）**：用户当场否掉了「用我们的软件要先装 Docker」这条前提。
   默认改成 **Docker 可选**：`BLANK_SANDBOX_ALLOW_LOCAL` 缺省 `true`，探测不到可用容器后端时
   `workspace` 档退 `LocalSandbox`（本机 Job 限额，文件/网络如实标「未由系统强制」）；
   `BLANK_SANDBOX_ALLOW_LOCAL=0` 才是"只准容器"的部署选择，那种情况下没有容器才整任务 409。
   第 4 条（桌面壳传 false）没有动：桌面用户本来就不需要 Docker，这一批只把裸 API 那条默认路修好。

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

# —— M4 复现（§7.4）——
# 八条边界真执行（本机 job_only 必须得到 2 拦住 / 5 未强制 / 1 未测；rc=0）
cd services/api && BLANK_SANDBOX_ALLOW_LOCAL=1 PYTHONIOENCODING=utf-8 PYTHONPATH=src \
  .venv/Scripts/python.exe -X utf8 -m blank_api.doctor --sandbox --json E:/tmp/doctor.json
# Node 入口（同一套探针）
BLANK_SANDBOX_ALLOW_LOCAL=1 /e/env/node/node-v24.20.0-win-x64/node.exe \
  packages/cli/src/index.mjs doctor --sandbox
# P-AUDIT 正例：先跑一条真任务（M3 之后），再核对；老任务必须是 rc=1
cd services/api && BLANK_SANDBOX_ALLOW_LOCAL=1 PYTHONIOENCODING=utf-8 PYTHONPATH=src \
  .venv/Scripts/python.exe -X utf8 -m blank_api.doctor --sandbox \
  --task <task-id> --api http://127.0.0.1:8061/api/v1
# 判据与变异
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_doctor_v47.py -q
# 安装包内的探针：打包后自检（缺 doctor.py 时 3b 段必须红）
cd desktop && PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" node node_modules/electron-builder/out/cli/cli.js --dir
PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" node verify-package.cjs

# —— #94 复现（§7.5）：MCP stdio 服务端进 Job ——
# 1) 11 条判据：内核成员（含阴性对照）、内存一红一绿、清树、挂失败红字、HTTP remote
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest \
  tests/test_mcp_job_v94.py -q
# 同样的 11 条在 warnings 升错误下重跑 10 轮，确认没有传输泄漏
cd services/api && for i in $(seq 1 10); do PYTHONIOENCODING=utf-8 \
  .venv/Scripts/python.exe -X utf8 -m pytest tests/test_mcp_job_v94.py -q \
  -W error::pytest.PytestUnraisableExceptionWarning -W error::ResourceWarning \
  -p no:cacheprovider || break; done
# 2) 相关套件（MCP / 边界 / REA / doctor）
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 -m pytest \
  tests/test_mcp_job_v94.py tests/test_agent_boundary_m3_v46.py \
  tests/test_rea_mcp_adapter_v108.py tests/test_mcp_tools.py \
  tests/test_boundary_facts_v20.py tests/test_doctor_v47.py \
  tests/test_doctor_compare_v49.py -q
# 3) 前端：MCP 未进 Job 的警告文案 + 类型
cd web && npm test && npx tsc --noEmit
# 4) 变异电池：measure 先量红集合，check 逐字比对（对照轮必须 11 passed / rc=0）
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m94/mutate_mcp.py measure
services/api/.venv/Scripts/python.exe -X utf8 E:/tmp/m94/mutate_mcp.py check
# 5) 真浏览器点验：隔离实例 :8061（mcp.json 放 fake-stdio + remote-stub）→ 前端 :3121
#    先起 HTTP 桩 E:/tmp/m94/http_mcp_stub.py（:8099），再重启 :8061 让 bundle 重连
PATH="/e/env/node/node-v24.20.0-win-x64:$PATH" node E:/tmp/m94/settings_mcp_verify.mjs
#    反证：把 mcp-panel.tsx 里 boundary === "job_object" 前置去掉 → 恰 1 条负对照 FAIL、rc=1
# 6) #76 可交付部分（§7.6）：机 A 基线 + 对账装置自检（第二台机器仍需外部机器）
cd services/api && PYTHONIOENCODING=utf-8 PYTHONPATH=src BLANK_SANDBOX_ALLOW_LOCAL=1 \
  .venv/Scripts/python.exe -X utf8 -m blank_api.doctor --sandbox --json E:/tmp/m76/machine-a.json
cd services/api && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -X utf8 E:/tmp/m76/compare-check.py
cd services/api && PYTHONPATH=src .venv/Scripts/python.exe -X utf8 -m blank_api.doctor \
  --compare E:/tmp/m76/machine-a.json E:/tmp/m76/machine-a.json    # 必须 same_host、rc=1
```
