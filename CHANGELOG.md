# 变更日志

## 未发布 — 命令级策略、签名接线、受限令牌后端与「跑不了测试」如实跳过

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
