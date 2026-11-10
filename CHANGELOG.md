# 变更日志

## 未发布 — 命令级策略、边界来源证明、句柄最小化、路径复验、CPU 限速、IO 速率上限、临时区容量卷、环境缺口补救、AppContainer 网络边界、签名接线、受限令牌后端与「跑不了测试」如实跳过

- **环境缺口不再停在「做不了」（K 批）**：真实读数（安装版 `blank.db`，任务 `4378855e…`，43 步）
  里，agent 确认本机没有 `node`/`npm` 后最终交付只剩「需要你：装上 Node 后重跑 `npm test`」；
  用户催办原话「为什么你看到没有后你不想办法自己装呢」「你怎么不自己装呢」被判成 answer/advise，
  而 advise 会打开只读开关，`run_shell` 连调都调不了。四处改动：
  1. **意图**：`intent.demands_action` 识别两种语序的催办（「你为什么/怎么不……」与
     「为什么/怎么你……」），在 `guess_intent` 里早于 `_OPINION`/advise 判定；`create_plan`
     再加一道确定性覆盖——模型把催办判成 answer/advise 时改判 `act` 并记 `intent.overridden`
     事件。提示词也补了「这类「为什么」不是在要解释，是在说「你去做」」。
  2. **空转断路器**：`where /R C:\ node.exe`、`if exist …`、`dir /s /b` 这类**找程序**不再被当成
     「又用了一次程序」；`cmd /c node -v`、`powershell -NoProfile -Command node -v`、
     `pnpm exec node -v` 仍按使用拦（包装词按原样与 basename 双比，修掉 `/c` 被剥成 `c` 的漏拦）。
  3. **环境缺口闸门**（`services/agent/remediation.py` + `execute_action`）：本任务有**真实的**
     `env_missing` 回执、答复是在把活交回用户（需要你装/装上后/装不了/做不了）、且整个任务没有
     一次安装/下载尝试——三条同时成立就把任务推回执行，并在系统侧注入补齐顺序（现场确认 →
     包管理器/portable/磁盘上找现成的 → 绝对路径或加 PATH 重跑），上限 2 轮；readonly、预算耗尽、
     已经试过安装时豁免。推回只进下一次模型调用，不改用户原话与历史。
  4. **系统提示词**：把「缺东西不是结论，是要做的工作」写成规则；仓库注释/README/记忆里写着
     「本机没有它」只能当线索，不能当证据；确实装不上才收尾，且必须写清试过什么、报错是什么。
  测试：新增 `tests/test_agent_remediation_v24.py` 11 例（真实交付原文 + 真实 `where node` 回执做
  输入，真实 AgentSession/网关跑执行节点），`test_stall_v151.py` 44 例、`test_intent_and_reply.py`
  37 例，合计 92 例全绿；变异 4/4 全红（拆闸门 / 答复不再交回 / 把确认算成安装 / 去掉意图覆盖）。
  后端全量 2512 passed / 24 skipped。**真模型点验未完成**：隔离实例 `:8061` 里存的 DeepSeek Key
  返回 401（invalid api key，2026-11-10 两轮均在第一次模型调用失败），换有效 Key 后按
  `E:/tmp/k_probe.py` 的同一目标重跑；在那之前本条只算单测与装置层证据，不算真模型已验证。

- **沙箱临时区容量上限：固定容量 VHDX 卷 + 写满 ENOSPC 行为探针（J2）**：NTFS 没有目录级
  容量配额（`fsutil quota` 只按用户/卷记，按用户设会误伤同一用户的工作区写盘），所以容量上限
  落到**独立卷**上：`create vdisk maximum=N type=expandable` + `attach` + 分区 + `format fs=ntfs
  quick` + `assign mount=<目录>`，每次执行在卷上建独立临时目录并把它设成子进程的 `TEMP`/`TMP`/
  `BLANK_SCRATCH`。只约束临时区，**不约束用户工作区**——工作区容量仍冻结（见「未验证」第 3 项）。
  容量上限由 `SandboxPolicy.disk_mb`（`BLANK_SANDBOX_DISK_MB`，默认 2048，0=关）控制，卷与
  挂载点默认放 `%ProgramData%\blank-quota`（可用 `BLANK_SANDBOX_DISK_ROOT` 改）：受限令牌/AC
  子进程要能**穿过**上级目录才能用挂载点里的临时目录，而 `%LOCALAPPDATA%\Temp` 的 ACL 不给
  Users 遍历权；非管理员退回 `%TEMP%\blank-quota`（挂载本身需要管理员，失败只记账不静默）。
  探针是行为证据：挂一个 64MB 卷、按 4MB 块写满再追加，必须拿到 `OSError errno=28`
  （WinError 112/39）且**第二次写仍失败**才算通过；收口当刻实测（管理员，build 26300）：
  容量 62.93MB、写入 48MB 后 free 3.14MB、`errno=28`、`second_write_failed=true`，耗时 3.8s。
  执行层账本新增 `disk_enforced` / `disk_bytes` / `disk_error`：卷挂载成功、本次临时目录真的
  落在卷上、探针证明写满会失败，三条同时成立才写 True；docker 命令行没有容量参数，容器档恒
  False。受限令牌与 AppContainer 后端的 `prepare` 同时给临时目录授 `M`、给卷根与上级目录授
  `RX`（原先受限子进程根本写不进 TEMP，这一条是接线时真机抓到的）。`PROBE_VERSION` 升到 5；
  app 启动 `warm_boundary()` 在开档且非 docker 时后台预热容量卷；治理快照带 `sandbox.disk_mb`
  与 `enforced.disk`，面板按 `disk_mb → disk` 查账并显示成 `2048m`，未证明时标「未强制」。
  新增 `tests/test_disk_quota_v23.py` 22 例（策略解析、卷缓存与失败不重试、临时区回退、
  假容量卷/非管理员/挂载失败三种负例、真实 64MB ENOSPC、三后端账本真值表、网关白名单、治理
  快照、真实 LocalSandbox 与真实受限子进程写 TEMP、悬空挂载点回收与孤儿清扫）；关联六套件
  （`test_disk_quota_v23` 22 + `test_boundary_facts_v20` 18 + `test_cpu_quota_v169` 11 +
  `test_io_rate_v21` 12 + `test_appcontainer_v22` 29 + `test_governance_snapshot_v16` 13）
  105 例全绿。变异 3/3 全红：拆掉 `scratch`、`cap.disk_quota`、`volume.ok` 任一条件，账本判据
  分别变红；其中 `volume.ok` 第一次**没红**——挂载失败用例传了空 scratch，被另一个条件遮成
  死判据，改成真实形状（临时区退回宿主目录、scratch 非空、卷 ok=False）后才可失败。
  回收路径也抓到一个真缺陷：卷卸下后挂载点变成**悬空目录联接**，`Path.exists()` 跟随联接返回
  False，旧实现整段 `rmdir` 被跳过，本机实测在 `%ProgramData%\blank-quota` 留下 16 个空壳；
  改用 `lexists` 收联接，并让 `_sweep_stale` 顺带清死进程的孤儿挂载点。判据
  `test_detach_removes_a_dangling_mount_junction` 在改回 `exists()` 时实测变红。

- **CPU 限速（Job CpuRate + 行为探针）**：`SandboxPolicy.cpus` 从"只是配置值"变成内核真限。
  Job 通过 `JobObjectCpuRateControlInformation`（ENABLE|HARD_CAP）设限；`CpuRate` 的单位是
  **整机算力的万分之一**（16 核：625 = 1 核，10000 = 整机），不是"单核的百分比"——第一版探针
  按后者读，2000/5000 两档在单线程上毫无变化。换算进 `windows_job.cpu_rate_for`；`cpus`
  不小于整机核数时不设限，并在 `cpu_error` 里说清"没构成限制"，不许记成已强制。
  账本要求两个都真：本次 Job 真接受了 `cpu_rate`，且 `_probe_cpu_rate` 现场压过——同一负载
  （4 个自旋子进程 × 0.5s）跑两轮，对照必须跑满 `workers×seconds×0.4`（装置自检），限速侧
  必须 ≤ 对照的 70%；单核机器构造不出对照，如实报"测不了"而不是猜成支持。收口当刻实测
  （非管理员，build 26300，16 逻辑核）：对照 1.984 CPU 秒 → 限速侧 1.016，阈值 1.389，
  `cpu_rate=625`。`cpu_enforced` 与 `enforced_fields` 的 `cpu` 同口径；生效时 notes 写
  "CPU 上限由 Job CPU rate 强制（整棵进程树合计，按整机算力折算）"。`PROBE_VERSION` 升到 2
  （探针语义变了，进治理指纹）；面板 `job_only` 文案从"只有内存/进程数/进程树"改为
  "资源与生命周期由内核强制"，cpus 只有在 `enforced.cpu=true` 时才不再标「未强制」。
  新增 `tests/test_cpu_quota_v169.py` 11 例，关联四套件 45 例全绿；变异 10/10 全红且红点
  对得上（整机换算、整机核数边界、LocalSandbox 接线、local/restricted 两处账本、机器账本
  的 Job 前提、探针结论抄写、notes、对照自检、限速阈值各至少 1 例）。

- **AppContainer 后端：Windows 上的文件与网络边界由内核拒（需管理员）**：
  `BLANK_SANDBOX_APPCONTAINER=1` 打开，`runner.get_sandbox` 的选择顺序是容器 → AppContainer
  → 受限令牌 → local。固定包名 `BlankSandbox`（`CreateAppContainerProfile`，已存在则
  `DeriveAppContainerSidFromAppContainerName`），`CreateProcessW` + `STARTUPINFOEXW` 挂
  零能力包 SID 与三句柄白名单，`CREATE_SUSPENDED` → 挂 Job → `ResumeThread`。
  授权只给工作区/临时目录 `M` 与工具链根 `RX /T`；祖先路径不授权——AC 令牌保留
  `SeChangeNotifyPrivilege`，实测用全新包名、零祖先授权时 AC 内 Python 照常启动
  （`E:/tmp/i-probe/no_ancestor.py`），而给祖先写 DACL 会让自检从 3.9s 涨回 200s+。
  准入是**行为证据**而非能力推断：同一个 AC 子进程里量到 AC `{'bind':'ok','loopback':'ok',
  'external':'PermissionError:13','secret':'PermissionError:13','ws_write':'ws-ok'}`、
  宿主对照 `{'external':'ok','secret':'SYNTHETIC-NOT-A-KEY',...}`；任何一条对照红掉整次
  判定作废（`appcontainer_unverified`），后端不会被选中。账本里 `filesystem_enforced`
  与 `network_enforced` 只读这份结论；`workspace-net` 在 AC 下明确拒绝（零能力包的方向与
  "要联网"相反，且不许降级成 off）。`boundary.PROBE_VERSION` 升到 3，新增
  `appcontainer_file_boundary` / `appcontainer_network_boundary` / `appcontainer_check`
  三个事实字段；`doctor` 先 `self_check(force=True)` 再取边界账；设置页与任务面板把
  `isolation=appcontainer` 单列为一种隔离（不再印成宿主直连）。
  一个必须修的差异：cmd 对**未加引号的路径型首词**会走搜索解析，AC 令牌缺目录访问权时
  报「拒绝访问。」；同一命令在宿主与受限令牌后端正常（`E:/tmp/i-probe/restricted_quote.json`）。
  隔离实验证明只有首词引号决定成败（`E:/tmp/i-probe/ac_quote_isolate.json`：`"exe" 参数`
  成功、`exe 参数` 失败、`"exe" -c print(1)` 正常）。新增 `quote_leading_path` 只给路径型
  首词补引号（裸名 `npm`/`python` 不动，否则会关掉 PATH 搜索），四种命令形状（字符串/argv
  列表 × 脚本文件/`-c`）在真实执行层全部 exit 0，读数一致（`E:/tmp/i-probe/ac_runner_e2e.json`）。
  新增 `tests/test_appcontainer_v22.py` 29 例（judge 真值表与对照优先级、引号归一化、
  `enforced_fields`/档位两条边界分开记、enforcement 账本、选择门、workspace-net 拒绝、
  治理与设置页文案）；关联八套件 165 例全绿；变异 10/10 全红（含"缺自检结论当全绿"与
  "网络事实顶替文件事实"两条补靶）。

- **Job IO 速率上限：接进执行层，由行为探针现场证明（J1）**：上一轮冻结的结论
  「接受设置但不节流」是**假阴性**——`JOBOBJECT_IO_RATE_CONTROL_INFORMATION` 里多插了
  一个 `MaxBandwidthStrict`，`ControlFlags` 落到结构外，`SetInformationJobObject` 返回 True
  但限速根本没开。按 48 字节 `NATIVE_V1` 布局重写后，2026-11-10 在本机（管理员、build 26300）
  用真实执行形状（父进程建 Job → 起子进程 → assign → 持续写盘）复测：对照 680.9 MB/s；
  请求 16/32/8 MB/s 时限速侧实测 16.1/32.5/8.0 MB/s（误差 <1.5%）。`SandboxPolicy.io_bps`
  （`BLANK_SANDBOX_IO_BPS`，默认 `128m`，0=不设）经 `windows_job.create_job(io_bps=...)`
  设进 local / 受限令牌 / AppContainer 三个后端；执行层账本新增
  `io_enforced` / `io_bps` / `io_error`：Job 接受 `io_bps` **且** `_probe_io_rate` 现场
  证明节流，两个都真才写 True；docker 命令行没有带宽参数，容器这一档恒 False。
  探针固定请求 16 MB/s、写 32MB：对照必须 ≥ 2× 限速档（否则如实报"构造不出对照"），
  限速侧 ≤ 对照×0.7 才算通过；收口当刻读数：对照 507.9 MB/s、限速侧 18.8 MB/s、阈值 355.6。
  `PROBE_VERSION` 升到 4；`boundary.enforced_fields` 从 7 项扩到 8 项（新增 `io`），
  治理快照带 `sandbox.io_bps`，面板按 `io_bps → io` 查账并把 134217728 显示成 `128m/s`。
  探针进程内只跑一次（CPU ~3s + IO ~2.5s），app 启动已在后台线程 `warm_boundary()` 预热。
  新增 `tests/test_io_rate_v21.py` 12 例；变异 4/4 全红且红点对得上：LocalSandbox 接线
  删掉（账本 False）、`ControlFlags` 清零（复现旧假阴性：Set 成功、探针报
  "限速未生效：344.1 > 阈值 240.9"）、`enforced_fields` 丢掉探针证据（Job 在就点绿）、
  面板漏掉 `io_bps → io` 映射。范围：只覆盖命令执行；fs-broker 的文件工具与 MCP 长驻进程
  未纳入；磁盘**容量**配额仍冻结（见下方「未验证」第 3 项）。

- **远程 MCP over OAuth 2.0（发现 → client_credentials → 刷新）**：`mcp.json` 的 `auth`
  段从"只有静态 headers"扩到真令牌生命周期。只走无人工的 `client_credentials` +
  `refresh_token`：桌面/后端进程里没人能点浏览器授权页，`authorization_code` 明确拒绝
  （`MCPStatus.skipped` 带原因），不假装支持。发现按 MCP 授权规范走两跳：RFC 9728
  受保护资源元数据（`/.well-known/oauth-protected-resource`，路径感知候选 + 同源兜底）
  给出授权服务器，RFC 8414 / OIDC 元数据给出 `token_endpoint` 与
  `token_endpoint_auth_methods_supported`；元数据的 `issuer` 与发现到的 issuer 不一致就拒。
  非 loopback 一律要求 https（只有 `http://127.0.0.1`/localhost 放行），令牌与发现请求都走
  `trust_env=False` 的客户端——环境里的代理变量不该把 Bearer 令牌捎到别处。
  令牌侧：`client_secret_basic` / `client_secret_post` 两法（显式 `token_url` 时也按
  `auth_method` 生效）；RFC 8707 `resource` 绑定目标服务端。JWT **只解包、不验签**——
  令牌是刚经 TLS 从令牌端点取回的，本机要核的是"是不是发给我、是不是还活着"：`aud`
  缺失或不符一律拒（期望值与实际值都写进错误），`exp` 已过拒，未写 `expires_in` 时用
  `exp - now` 调度，提前 30 秒刷新；opaque 令牌无法在本机核对，状态里如实写
  `audience_checked=false`，靠服务端 401 驱动。401 后只刷新重试一次（先 `refresh_token`，
  被拒退回 `client_credentials`），不做刷新风暴。令牌、刷新令牌与 client_secret 不进错误
  文案（`_redact`）、不进状态/证据（`facts()` 只有类型、剩余秒数与核对结论），
  `OAuthConfig.client_secret` 的 `repr` 已关。新增 `tests/test_mcp_oauth_v170.py` 24 例
  （假授权服务器覆盖发现两跳、两种客户端认证、受众/过期/opaque、refresh 被拒回退、
  401 只刷一次、令牌不入状态与错误）。测试当场揪出三个实现缺陷：显式 `token_url` 配
  `auth_method=post` 会静默退回 Basic；同一秒内签发的 JWT 逐字节相同，"拒绝第一枚"
  会把刷新后的新令牌一起拒掉（改每个测试令牌带唯一 `jti`）；`close()` 抹掉令牌后
  再读 `facts()` 会 KeyError。

- **更新源分离签名（Ed25519）与 CycloneDX SBOM**：原来的更新只校验 `sha256`——摘要与
  feed 来自同一个源，源被攻陷时两者可以一起改。现在 feed 条目必须带 Ed25519 **分离签名**：
  签名覆盖 `{version,url,sha256,size,sbom}` 固定顺序、无空白的规范字节
  （`canonicalUpdatePayload` 由壳端与发版脚本共用），`size` 必须是正整数、摘要统一小写。
  公钥钉死在 `desktop/shell-policy.cjs`（`UPDATE_PUBKEY_PEM`，Ed25519），私钥只在发版人
  手上、不进仓库；未打包时允许 `BLANK_UPDATE_PUBKEY_PEM` 注入公钥供接线测试，
  `app.isPackaged` 为真时这条旁路关闭。`verifyUpdateSignature` 对缺签名、缺 size、坏
  base64、非 64 字节、公钥不可解析各给可读原因且绝不抛。`main.cjs` 两道闸：
  **下载前**验签（不过就不取包、托盘写「更新：不可用」、不留安装入口），**装前**再验一次
  签名并按实际字节重算摘要。`scripts/make_update_feed.mjs` 现在必须给
  `--sign-key`/`BLANK_UPDATE_SIGNING_KEY`，自检含"改任一被签字段必拒"与"未签名必拒"，
  私钥与壳内公钥不配对时当场失败、不产出装不上的 feed。
  随包 SBOM 由 `scripts/make_sbom.mjs` 生成 CycloneDX 1.5：npm 组件逐条来自
  `desktop/package-lock.json` 的 `packages`（含传递依赖），Python 组件来自
  `services/api/uv.lock`；serialNumber 由组件集合哈希派生（同组件集合同序号）；
  `metadata.properties` 写入安装包文件名、sha256 与字节数，SBOM 与二进制能对上号。
  自检当场揪出一个口径错误：同名同版本在锁文件里因嵌套 `node_modules` 出现多次
  （392 条 purl 只有 344 个唯一值），组件是"库"不是"安装位置"，改为按 purl 去重后才写出。
  实测（2026-11-10，真产物 `desktop/dist/Blank-AI-安装版-0.11.2.exe`，131457034 字节
  sha256=`3746586e…bbf40`）：SBOM 344 个组件（npm 272 + pypi 72）自检全过；用仓库外私钥
  签发 feed 后，壳内钉住的公钥验签通过，改 version/url/sha256/size/sbom 任一项都被拒，
  未签名条目被拒，`planInstall` 对换包（摘要全 0）被拒；缺安装包时脚本退出码 2。
  桌面套件 101 例全绿（新增 `update-policy` 6 例 + `main-wiring` 1 例：unsigned/badsig
  必须在下载之前拒绝）；关键路径变异：撤掉下载前验签，接线用例立刻变红。
  仍未落地的是 exe 的 Authenticode 证书（`sign:check` 退出码 2，只出未签名包），
  见下方「未验证」第 5 项。

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

以下六项是本轮内核升级里**没有**落地或**没有**在本机真验的部分。冻结在这里，不写进
「已完成」；每项都留下判据、红了长啥样与哑弹风险，条件具备时照着验。
（CPU 限速、IO 速率上限、远程 MCP 的 OAuth、更新源分离签名与 SBOM 已从这张表移出，见上方条目；
Windows 网络出口强制已由 AppContainer 落地，见上方条目与第 1 项的剩余部分；第 2 项
Linux 原生后端按项目决定移出范围，判据只作留档。）

1. **网络出口强制（P0）**——Windows 已落地（上方 AppContainer 条目：零能力包、
   同进程内对外 TCP 被拒为 `PermissionError:13`、宿主对照在同一时刻外连成功、
   账本只读这份行为证据），**仍未成立的是**：`local` 与 `restricted` 后端（未开
   AppContainer 或没有管理员权限时）账本恒 `network_enforced=false`，面板如实标未强制；
   WFP 式的「保留宿主文件访问、只拦出口」没有实现。Linux 路径按项目决定不再做。
   剩余判据：local/restricted 下账本必须为 false、doctor 必须报未强制，不许借
   「机器能建 AC」冒充本次已强制；若将来接 WFP，同一个子进程内回环监听连接成功、
   对外 TCP 被拒且账本为 true，撤掉规则后对外连接必须成功（变异对照）。红了长啥样：
   local 档账本写 true，或只在开启 AppContainer 的机器上把开关关掉仍报已强制。
   哑弹风险：WFP 规则绑错进程（只拦父不拦子）、Windows 版本差异导致过滤器静默不生效、
   把「AC 内某一次外连失败」当成「所有出口都被拦」（IPv6、代理、原始套接字未逐一验）。
2. **Linux 原生沙箱后端（P1，已移出范围，仅留档）**——项目决定不做 Linux 场景；
   CI `api` job 仍在 Ubuntu 上跑全量，但那只是回归，不构成 Linux 上的边界承诺——
   代码里没有 landlock/seccomp/unshare，Linux 上非 Docker 后端不提供本机文件/网络边界。
   若将来重启：需要 Linux 内核 ≥ 5.13（Landlock）或可用的非特权 user namespace；判据是
   在 `ubuntu-latest` 上，子进程写工作区外被内核拒（EACCES/EPERM，不是路径预检返回的
   4xx）、对外连接被拒、自检阴性/阳性对照都成立。红了长啥样：探针只走路径字符串判断、
   或内核不支持时用例 `skip` 却被当成通过。哑弹风险：runner 关闭非特权 userns、Landlock
   ABI 低于所需版本、受限令牌代码被错误地当成跨平台后端。
3. **工作区磁盘容量配额（P2）**——沙箱**临时区**（TEMP/TMP/`BLANK_SCRATCH`）的容量上限
   已在本轮落地（见上方 J2 条目：固定容量 VHDX 卷 + 写满 ENOSPC 行为探针，账本
   `disk_enforced` 与探针同源）；仍冻结的是**工作区/每任务根**的容量上限：命令往工作区
   写盘不受这个卷约束，`disk` 只承诺临时区。2026-11-10 管理员权限下已用 VHDX 探针证实
   内核路径可行（`E:/tmp/i-probe/vhd_quota_probe.py`）：64MB 动态 VHDX + diskpart
   attach/分区/格式化/挂载到目录，写满时内核返回 `OSError errno=28 ENOSPC`（容量 62.93MB、
   写 48MB 后 free 3.14MB、VHD 文件 68MB），detach 成功、无残留盘与目录。需要：把固定
   容量卷扩到工作区或每任务根（与 J3 任务级隔离一起做更自然），并做多任务卷生命周期
   回收。判据：写盘到配额上限后继续写失败且总占用不再增长（ENOSPC，不是我们的路径预检）；
   detach 后挂载点可删、无残留卷。红了长啥样：工作区磁盘无限增长、失败来自路径闸门而
   不是内核、卷 detach 不掉。哑弹风险：卷名/挂载点在异常退出后残留、多个任务共用一个卷
   时互相写爆、把「写满报错」当成「文件边界」写进 `filesystem_enforced`。
4. **任务级隔离（P3）**——现状：工作区属于项目，同一项目的任务共享可写根；隔离靠路径
   闸门与审批，不是 OS 命名空间。需要：每任务独立根 + 跨任务绝对路径也不可达（受限令牌/
   AppContainer）。判据：任务 A 写入标记文件，任务 B 用绝对路径与相对路径都读
   不到、写不进；同机另一 OS 用户同样不可达。红了长啥样：只拦相对路径/`..`，同用户进程
   用绝对路径照样打开。哑弹风险：工作区根互相嵌套、junction/符号链接绕过、同一 OS 用户
   下文件权限天然可读。
5. **Authenticode 代码签名与干净机装包复核（P3）**——更新源的**分离签名**（Ed25519）与
   CycloneDX SBOM 已在本轮落地（见上方条目：下载前验签、装前复验、篡改任一被签字段即拒；
   SBOM 由锁文件生成并带上安装包摘要）。仍冻结的是 exe 本身的 Authenticode 签名：配置已
   接线（发布者=无锡空白格人工智能科技有限公司），但本机没有证书（`sign:check` 退出码 2，
   只出未签名包）。需要：真实代码签名证书 + 一台干净机器。判据：干净机器上
   `signtool verify /pa` 通过；篡改安装包后更新被拒（本机已用真产物与真私钥验过，见上）；
   Windows SmartScreen 不再报未知发布者。红了长啥样：未签名包可安装、签名坏掉仍继续、
   证书过期后旧包验不过（缺 RFC3161 时间戳时）。哑弹风险：证书主体与发布者名不一致、
   时间戳服务不可用导致签名静默降级、开发构建绕过校验。
6. **第二台机器差异验证（#76，外部机器）**——现状：边界对账装置已进仓库，但只在本机
   （build 26300）跑过，权限覆盖非管理员与管理员两档；Windows 版本、ACL 语义、
   AppContainer/受限令牌可用性差异没有实测。需要：第二台干净机器（建议不同 Windows 版本）。判据：同一装置在第二台
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
