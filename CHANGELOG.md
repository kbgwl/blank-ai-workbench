# 变更日志

## 未发布 — 命令级策略、边界来源证明、句柄最小化、路径复验、签名接线、受限令牌后端与「跑不了测试」如实跳过

- **按句柄复验路径，堵住预检与 open 之间的窗口**：`resolve_inside` / `resolve_for_write`
  判定的是路径字符串，等真正 `open` 时中间目录可能已被换成指向工作区外的 junction
  （Windows）或符号链接（POSIX）。新增 `workspace.open_verified`：先 `os.open`，再按
  **句柄**取最终路径复验——Windows 走 `GetFinalPathNameByHandleW`（剥掉 `\\?\` 与
  `\\?\UNC\` 前缀），POSIX 走 `realpath` + `(st_dev, st_ino)` 与 `fstat(fd)` 比对；
  拿不到最终路径一律拒绝（「验不了」不等于「没问题」）。新建用 `O_CREAT|O_EXCL`，
  任何打开方式都不带 `O_TRUNC`——截断必须发生在复验之后，否则链接会先毁掉外面的文件；
  复验失败时关句柄并删掉自己刚建的那个空文件，外面不留痕。`write_verified` 先试
  `r+b`、不存在再 `wb` 新建，写入都落在复验过的同一个句柄上。接线到
  `read_text_file`、`_patch_on_disk`（读与写共用同一个复验过的句柄，中间没有路径可换）、
  `write_backup`、`restore_backup`。二进制句柄不做换行转换，补丁读写两侧都包
  `io.TextIOWrapper(..., newline=None)`，与原先 `read_text`/`write_text` 口径一致
  （CRLF 读成 LF、写回本机换行），否则 CRLF 仓库里所有补丁都会匹配失败。新增
  `tests/test_path_verification_v168.py` 6 例：先造真目录（预检能过）、换成链接、
  再 monkeypatch 掉预检，只看句柄层拦不拦得住——读、补丁改写、补丁新建、
  备份写四条必须 `path_escape_denied` 且外部内容/文件不变，外加正常读与
  补丁回滚两条对照。关联 34 个套件 449 passed / 14 skipped。变异：关掉复验判定
  → 4 条链接用例红、2 条对照绿；撤掉读接线 → 1 红；撤掉补丁读接线 → 1 红；
  撤掉备份接线 → 1 红；还原后全绿。

- **受限子进程只继承三个标准句柄**：`bInheritHandles=True` 是 stdout/stderr 重定向的前提，
  但它的默认语义是「宿主里所有带继承标志的句柄全递」——浏览器管道、打开的文件、套接字都会
  漏进被沙箱化的一侧。启动改用 `STARTUPINFOEXW` + `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`，
  继承范围压到 NUL/stdin/stdout/stderr 三个；属性表建不出来（`InitializeProcThreadAttributeList`
  或 `UpdateProcThreadAttribute` 失败）就**拒绝启动**，不退回「继承全部」。属性表缓冲区与
  句柄数组都活到 `CreateProcessAsUserW` 返回（缓冲区存的是数组地址，提前回收就是悬空指针）。
  本机实测（build 26300，非管理员）：宿主摆 24 个可继承文件句柄，受限子进程句柄数
  96 → 96（差值 0）；撤掉属性表后 96 → 120（正好多 24）。新增 1 例
  `test_child_inherits_only_the_three_stdio_handles`（父侧 Toolhelp 找到子 cmd，再读
  `GetProcessHandleCount`；判据是基线与加料两次的差值 ≤ 4），受限令牌套件 14 例全绿，
  关联 17 个沙箱/边界/权限套件 246 passed / 14 skipped。变异：撤掉白名单 + 去掉
  `EXTENDED_STARTUPINFO_PRESENT` → 仅该 1 例红，还原后全绿。

- **边界来源证明与降级告警**：治理快照现在能回答「这一步被哪一版手写规则、哪一版探针
  量出来的」。手写规则新增稳定指纹 `ruleset.sha`（按生效规则与顺序计算，注释/空行不动它；
  坏文件整份停用、指纹留空，与「文件为空」区分）；`boundary.PROBE_VERSION` 进
  `Capability`、快照字段 `boundary_probe_version` 与治理指纹——证据包里能把「机器变了」
  和「量它的尺子变了」分开。边界变弱单独写 `boundary.downgraded` 事件：强度档位下降、
  装置自可信变不可信、后端从有变无、任一 enforced 字段 True→False、档位可用→不可用，
  每条原因带前后值；升级不告警，同样的弱边界不重复告警。证据包 `governance` 新增
  `rules_sha` 与 `boundary_downgrades{count, events[seq, step, node, reasons]}`。
  探测节奏沿用现有口径：每个模型调用前现查，探针缓存 TTL 60 秒——**不是**独立定时器，
  这一步没跑模型就不会复查。新增 `tests/test_boundary_attestation_v167.py` 13 例，
  关联五套件 82 例全绿。变异 5/5 全红且红点对得上：指纹不进快照 → 2 例红，关掉降级判定
  → 5 例红，不写降级事件 → 2 例红，探针版本写死 → 1 例红，证据包不装载降级事件 → 1 例红。

- **命令级策略（execpolicy，对标 Codex rules）**：规则与会话批准/驳回不再停在外层命令首词。
  壳包装的内联载荷参与匹配：`cmd /c|/k`、`powershell|pwsh -Command|-c`、`sh|bash|zsh|dash -c`
  递归展开（深度上限 4、片段上限 64），脚本路径（`bash script.sh`）不展开——展开脚本等于替用户
  批准脚本里的任意内容。deny 改成「任一段命中就拒」：`echo hi && rm -rf build` 里真正的删除
  不能再搭无害段的便车；对 `\"`、`` `" ``、`^"`、`""` 多加一个去引号视图，`powershell -Command
  "cmd /c \"rm -rf build\""` 这种转义形状也能拒到，去引号视图只追加在 deny 侧，只收紧不放宽。
  allow 仍要求每一段都被覆盖：`cmd /c "git status"` 可放行，`cmd /c "git status && rm -rf src"`
  不行；旧形状的整串规则（`allow run_shell(cmd /c "git status")`）照样有效。会话驳回同口径
  （任一展开片段命中即拒）。新增 `BLANK_SHELL_POLICY=allowlist`：没有命中显式规则/会话批准/工具档的
  `run_shell` 一律回到人手；默认 `ask` 保持产品现状，排在全放行档与 `approve_high_risk` 之后——
  这两者本身就是用户/调用方当场写下的授权。新增 `tests/test_exec_policy_v166.py` 18 例，关联七个
  权限套件 194 例全绿。变异 4/4 全红且红点对得上：撤掉壳展开 → 9 例红（含两条端到端），
  deny 退回逐段全覆盖 → 6 例红，撤掉去引号视图 → 仅转义形状那 1 例红，关掉 allowlist 闸 → 仅
  默认拒绝档那 1 例红。端到端正反对照：同一 `cmd /c "del src\core.py"`，deny 规则下文件还在、
  allow 规则下文件真被删——红不是「命令本来就跑不起来」。

- **受限令牌后端（Windows，默认关）+ 文件边界成为独立事实**：`BLANK_SANDBOX_RESTRICTED=1` 时，
  Docker 不可用且自检全绿就改用 `CreateRestrictedToken`（限制 SID = Users / Everyone /
  Authenticated Users / 登录会话 SID）+ Job 启动命令。文件边界由 NTFS 真的拒；网络没有
  AppContainer 就拦不住，账本固定 `network_enforced=false`——拿受限令牌冒充网络隔离，就是本立项
  要消灭的那类谎。准入自检不是装饰：同一个受限子进程里必须同时出现「合成私钥读不到」与「工作区
  读写成功」，另加宿主令牌、授权后、去掉限制 SID 三条对照；任一对照红掉，整次判定作废、后端不许启用。
  规则文件（`BLANK_SANDBOX_RULES_FILE`，不设则用包内 `rules.default.json`）只接受 `default_action=deny`，
  任何覆盖 `~/.ssh` 等保护路径的授权会让整份规则作废，未知字段或空占位符直接报错。
  Job 改为 `CREATE_SUSPENDED` → 按句柄挂入 → `ResumeThread`，消掉「按 PID 再 OpenProcess」的竞态。
  2026-11-09 本机（非管理员，build 26300）实测：`BLANK_SANDBOX_RESTRICTED=1 blank doctor --sandbox`
  → 拦住 6 / 未强制 1（P-NET 网络）/ 不确定 0，八条账全部一致、退出码 0；同一命令用默认 local 后端
  → 拦住 2（P-PROC / P-INHERIT）/ 未强制 5（P-READ / P-WRITE / P-ENV / P-NET / P-ESCAPE）。
  三个实测坑写进实现：受限令牌下 cmd 拒绝执行 `.cmd`（改 inline `/d /s /c`）、
  `DETACHED_PROCESS` / `CREATE_NO_WINDOW` 让 Python 起不来（0xC0000142，改 flags=0 + `SW_HIDE`）、
  `icacls /T` 配 `(OI)(CI)` 对文件静默跳过（改根目录 + 逐文件两段式授权）。
  账本拆分：`local` 不再借机器能力，文件/网络恒 False、来源写 `measured_job_object`；受限令牌后端的
  `filesystem_enforced` 来自行为自检，`network_enforced` 恒 False。新增 30 例（规则 16、受限令牌与后端
  选择 13、事件循环不阻塞 1）；后端全量 **2360 passed / 24 skipped / 0 failed**（798.84s，`EXIT=0`）。
  边界探测原本把行为自检同步跑在事件循环里，六个进程并跑时首条命令从 6-9s 拖到 22-24s；现在 `local`
  与 `restricted` 两条算账路径都走线程，新用例用会睡 0.6s 的替身数事件循环空转次数（撤掉 `to_thread`
  即红：0 次 < 10 次）。
  收口说明：改动期三次全量各有一次团队轮次用例在 30s 内没等到收尾（origin 批准续跑 / midround 降上限 /
  run_id 形状，每次只红一个）；这些用例单跑、同文件子集与 HEAD 对照均绿，插桩显示行为自检只在进程早期
  跑了一次 0.24s，因此按「团队夹具在共享临时库上的时序敏感」记录在案，没有调大超时。上面这轮全量干净。

- **代码签名接线（待证书）**：`desktop/package.json` 的 `win.signtoolOptions` 写死发布者与证书主体
  「无锡空白格人工智能科技有限公司」、`sha256`、RFC3161 时间戳；新增 `npm run sign:check` 只读自检
  （0=证书就绪 / 2=本机没有该公司证书 / 1=配置或查询有问题）。本机实测退出码 **2**：证书库里按代码签名
  用途过滤后为 0 张，也没有 `.pfx/.p12/.cer`——私钥不在仓库里，正式分发前必须装证书或用 `CSC_LINK`。
  `dist` 改走 `dist-build.cjs`：配了 `certificateSubjectName` 而不加这层时，electron-builder 会去证书库找、
  找不到直接失败（本机实测 `Cannot find certificate 无锡空白格人工智能科技有限公司`、退出码 1）；现在
  有证书按指纹签、配了 `CSC_LINK` 交给外部证书、都没有则只关签名并明说「本次只出未签名包」。
  同一棵树上 `node dist-build.cjs dir` 实测退出码 0、产出 `dist/win-unpacked/Blank AI.exe`（未签名）。
  三态覆盖项与「覆盖项真的能清掉主体名」用 electron-builder 自己的 `deepAssign` 钉住，变异 3/3 全红。
- **Go 项目缺工具链不再被当成代码失败**：`run_tests` 识别 `go.mod` 后先探测 `go` 是否在 PATH；缺工具链时
  返回 `status=skipped` 并带上「本机没有 go 工具链」，验证节点把它落成带理由的 `validation.completed`，
  交付文案会明说未验证。2026-11-09 复测（`evals/results/results-2026-11-09T04-47-06-167Z.json`）：
  T11/T28 的 `validation.status=skipped`、`unverified_risks` 写明缺工具链，**不再空转修复回路**；
  但任务最终仍是 `failed`——脚本化桩的固定交付文案照样声称「测试通过」，`no_false_claim` 按实判红
  （`failure_reason=交付文本声称测试通过，实际验证结果不支持`）。门禁本机 **0.875 < 0.938**
  （红 T07/T29 `files_missing` 外加 T11/T28）；阈值没动，评测机装 Go 后才会回到 0.938。
- **桌面 README 纠偏**：自动更新段落按现状改写（检查 → 下载 → 校验 → 用户点击安装），不再声称"不下载不安装"。

### 未验证（需外部条件，不计入「已完成」）

以下七项是本轮内核升级里**没有**落地或**没有**在本机真验的部分。冻结在这里，不写进
「已完成」；每项都留下判据、红了长啥样与哑弹风险，条件具备时照着验。

1. **网络出口强制（P0）**——现状：`local` / `restricted` 后端账本都是
   `network_enforced=false`，只有 Docker 档靠 `--network none` 成立。需要：管理员权限 +
   WFP（Windows）或 netns/cgroup（Linux）。判据：同一个子进程内，回环监听连接成功、
   对外 TCP 连接被拒，且账本为 true；撤掉规则后对外连接必须成功（变异对照）。红了长啥样：
   规则下对外仍连通却记 true，或只拦指定 IP/域名而 IPv6、代理、原始套接字照走。哑弹风险：
   WFP 规则绑错进程（只拦父不拦子）、Windows 版本差异导致过滤器静默不生效。
2. **Linux 原生沙箱后端（P1）**——现状：CI `api` job 已在 Ubuntu 上跑全量，但非 Docker
   的 Linux 后端不存在（代码里没有 landlock/seccomp/unshare），Linux 上拿不到本机文件/
   网络边界。需要：Linux 内核 ≥ 5.13（Landlock）或可用的非特权 user namespace。判据：
   在 `ubuntu-latest` 上，子进程写工作区外被内核拒（EACCES/EPERM，不是路径预检返回的
   4xx）、对外连接被拒、自检阴性/阳性对照都成立。红了长啥样：探针只走路径字符串判断、
   或内核不支持时用例 `skip` 却被当成通过。哑弹风险：runner 关闭非特权 userns、Landlock
   ABI 低于所需版本、受限令牌代码被错误地当成跨平台后端。
3. **CPU / 磁盘 / IO 配额（P2）**——现状：Job Object 只强制内存、进程数与进程树；
   `cpu` / `disk` 在账本里恒 False（非管理员拿不到可靠限速，磁盘配额需要管理员）。需要：
   管理员权限或可用的磁盘配额/IO 限速接口。判据：持续压满 CPU 的子进程在 N 秒窗口内被
   压到约定比例；写盘到配额上限后继续写失败且总占用不再增长；账本与实测一致。红了长啥样：
   压测进程吃满全部核心、磁盘无限增长，面板却写 true。哑弹风险：Job CPU 限速只在部分
   系统版本有效、磁盘配额按卷生效会误伤其他用户、IO 限速接口不可用时静默忽略。
4. **任务级隔离（P3）**——现状：工作区属于项目，同一项目的任务共享可写根；隔离靠路径
   闸门与审批，不是 OS 命名空间。需要：每任务独立根 + 跨任务绝对路径也不可达（受限令牌/
   AppContainer/Linux 后端）。判据：任务 A 写入标记文件，任务 B 用绝对路径与相对路径都读
   不到、写不进；同机另一 OS 用户同样不可达。红了长啥样：只拦相对路径/`..`，同用户进程
   用绝对路径照样打开。哑弹风险：工作区根互相嵌套、junction/符号链接绕过、同一 OS 用户
   下文件权限天然可读。
5. **远程 MCP 的 OAuth 与令牌生命周期（P3）**——现状：`http` 传输已支持，但鉴权只有
   静态 `headers`（含 `${VAR}` 展开），没有发现、刷新、audience/过期校验。需要：可用的
   IdP 或测试授权服务器。判据：过期令牌与错误 audience 必须被拒，刷新成功后旧令牌失效，
   令牌不进入日志/事件/证据包。红了长啥样：过期或错 audience 的令牌仍被接受，或令牌原文
   出现在事件负载里。哑弹风险：发现地址可被 SSRF、把静态 Header 当成 OAuth、刷新令牌
   落盘明文。
6. **供应链：更新包签名与 SBOM（P3）**——现状：更新 feed 带 `sha256`，下载后按实际文件
   重算比对；代码签名配置已接线（发布者=无锡空白格人工智能科技有限公司），但本机没有
   证书（`sign:check` 退出码 2，只出未签名包），没有 SBOM。需要：真实代码签名证书 +
   可复现的构建产物。判据：干净机器上 `signtool verify` 通过；篡改安装包后更新被拒；
   篡改 feed 里的摘要并同时换包也必须被拒（摘要需另有签名，否则同源篡改测不出来）；
   SBOM 与安装包实际依赖一致。红了长啥样：未签名包可安装、签名坏掉仍继续、SBOM 与包内
   文件对不上。哑弹风险：摘要与包来自同一被攻陷的源、开发构建绕过校验、SBOM 只生成不比对。
7. **第二台机器差异验证（#76，外部机器）**——现状：边界对账装置已进仓库，但只在本机
   （build 26300、非管理员）跑过；Windows 版本、ACL 语义、AppContainer/受限令牌可用性
   差异没有实测。需要：第二台干净机器（建议不同 Windows 版本）。判据：同一装置在第二台
   机器上输出逐项一致的边界账（或差异有明确原因），受限令牌后端自检全绿才启用。红了长
   啥样：第二台机器上后端自检假绿、文件边界在真实路径上不成立。哑弹风险：只在相同 build
   上复跑、对照项与判定项用同一套代码自证。

## v0.11.2 — 收口「说真话」，接通更新源（2026-11-09）

发布结论：本版不是新功能，是把「看起来通过」逐条换成「真的对得上」。安装包按修复后的源码重打，
提交 `2bd45d8`，已同步到远端 `main`。

- **审批身份闭环**：校验通道与跨恢复续跑同样按 `approval_id + tool_call_id + tool_name + arguments_hash`
  四元匹配，错一个即 409，不再留「旧卡复用」的口子。
- **读图不再静默降级**：`deepseek-flash` 被正确认出支持图片，附件随 `/turns` 进上下文；不支持时明说原因。
- **卡住时说清楚**：任务超时一次打印 13 项现场（任务/运行/派发/供应商/最近事件/解释器/租约…）。
- **触发器边界**：暂停/取消后的状态与按钮一致，watch 根目录之外永不放行。
- **自动修复说真话**：修不好时不再报成功，`verdict` 按实际复核结论给。
- **代理变量不再炸掉所有模型调用**（本版新修）：`NO_PROXY` 里带方括号 IPv6（`[::1]`，curl 的写法）时
  httpx 会把它拼成非法 URL，构造客户端即抛 `InvalidURL: Invalid port: ':1]'`——代理一次没用上，
  所有模型/通道调用先全炸。入口处把方括号归一（httpx 会给裸地址补回方括号），绕过语义不变；
  回归用真子进程钉住，摘掉归一那行即红。因 `src/` 有改动，安装包重打。
- **更新源接通**：`pnpm feed:update` 按壳端契约生成 `latest.json`（版本 + http(s) URL + 64 位 sha256），
  壳端纯函数自检 8 项 + HTTP 全链路 10 步验通；默认不联网，须配 `BLANK_UPDATE_FEED_URL`，安装须用户点击。
- **验收读数**：后端 **2325 passed / 0 failed / 23 skipped**（633.50s，`EXIT=0`）；前端 296 passed，
  `tsc`/lint/构建 `EXIT=0`；安装包自检 **44 项全 ok**；版本一致性 6/6；安装包 sha256 `3746586eb260…`。
- **仍未验证（如实列出）**：CI 未在真 GitHub runner 跑过；#76 第二台机器差异验证未做；
  签名证书未采购（产物 `NotSigned`）；更新源未托管到公网。评测 benchmark 55 例通过率 71%（3 例假完成、1 例未授权高风险）。

## v0.11.1 — 通知通道：「到点了」真的会弹出来（2026-11-08）

- **原生系统通知**：桌面壳直接弹 Windows toast，点一下唤回窗口并跳到那条对话（`?thread=` 深链）。
- **判定仍在后端，壳只负责投递**：要不要打扰由 `agent/notify.py` 判（策略 + 免打扰），结论连同理由写进
  `notify.decided` 事件；`GET /api/v1/notifications` 只吐 `disturb=true` 的那几条，壳不复判。
- **文案按来源分档**：定时起的弹「到点了」，盯梢的弹「有变化了」，审批弹「等你批准」（并出声）。
- **三条降噪**：按 id 去重、窗口正被看着就不弹、一分钟超过 3 条先压着（审批例外）。
- **免打扰入口**：设置 → 执行与智能体 →「通知与免打扰」，填 `22:00-08:00`（跨零点也行），只压「做完/有变化」。

## v0.11.0 — 让 Super Agent 真的会醒（2026-11-08）

- **定时真的到点**：不再登记一条永远不响的待触发；到点在桌面形态下自己起跑（以前只入队、没有 worker 认领）。
- **「盯着工作区」有真实的事件生产者**：每拍扫一次文件 mtime。
- **消息通道做成配置项**：企业微信/钉钉/飞书在「设置 → 消息通道」填完即用。
- **自动更新接上真实下载与摘要校验**：默认不碰网络，须配 `BLANK_UPDATE_FEED_URL`。

> v0.9.1 / v0.10.0 当时未在此文件立条目，概述见 README「更早 / 再上一版」段。

## v0.9.0 — Super Agent Upgrade（2026-11-03）

发布结论：55 项 coding 基线真实跑通（NVIDIA Mistral Large 4.1T，独立评测后端），completionRate 91% / passRate 98% / testPassRate 100% / misEditedFiles 0 / falseDone 0 / falseClaim 0 / unauthorizedHighRisk 0，达全部硬性发布门槛。

- **运行时升级**：Router/Worker 双层架构落地，新增事件触发、后台执行、Watchdog、断点恢复与低打扰交付。
- **触发器与调度快照**：新增 agent 触发器（triggers）与 dispatch snapshot，配套 alembic 迁移 `0031_dispatch_snapshot.py`、`0032_agent_triggers.py`。
- **意图三分法 + 双重预算**：answer/advise/act 判定与步数 + token 双重预算落地，闲聊不再被误判为改代码任务。
- **交付闸门与空转断路器**：写盘前事实闸门（unknowns）与 `_enforce_no_false_claim` 交付闸门上线；空转断路器拦三类浪费回路，拦截用独立 `blocked` 状态、不冒充 `denied`。
- **评测修复**：`run.mjs` 对供应商 429/529 限流归 `provider_failed` 走退避重试（此前静默记 `setup_failed` 漏跑）；评测库 NVIDIA provider 失效 key 已更新。
- **桌面包**：同步 alembic `0031/0032` 迁移，`verify:desktop` 全绿。
- **已知残留**：coding-035 为 fixture 前提与评测口径错位（agent 满足书面 acceptance 但未触碰预期文件），不属能力回退，列入后续整改（修 fixture 或放宽 `expectedFilesTouched` 与 acceptance 的 AND 关系）。
- **未验证项**：真实 Electron GUI 18 项取证因 headless 环境限制待真机补（已如实标注，未冒充）。

## v0.1.0

- 初始 tag（基线）。注：0.1.0–0.8.x 期间为未打 tag 的迭代构建。
