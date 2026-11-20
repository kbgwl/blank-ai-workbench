# 外部产品「完全访问 / YOLO」档的审批边界源码对照（2026-11）

> 调研代理产出。只做调研，未修改项目源码。
> 用户原话：「我已经设置了完全访问了，还一遍一遍让我授权」。
> 目标：用**源码**回答 A/B/C/D，并逐条对应 kbgai 基线 1/2/3/4 给出可落地建议。

---

## 0. 证据来源与标注约定

| 产品 | 证据来源 | 版本 / 分支 | 引用方式 |
|---|---|---|---|
| Claude Code | 本机 `claude.exe` 尾部明文 JS；`E:/rea/claude/extract2.mjs` 抽取出的 `E:/rea/claude/app.js`（39 MB）；抽取脚本 `extract.mjs` 的注释明确「Bun 把源码以明文附在 PE 之后」 | 文件头标注 `// Version: 2.1.271` | Bundle 是**单行压缩 JS，没有行号**；下文用 `app.js @char N` 表示字符偏移 + 还原出的函数名/变量名，这是对压缩 JS 唯一可复核的定位方式 |
| Codex CLI | 本机 `E:/env/npm-global/node_modules/@openai/codex/package.json` 版本 **0.154.0**（原生 Rust 二进制，npm 包不含源码）；源码取 GitHub `openai/codex` tag **`rust-v0.154.0`** 的 `codex-rs/**` 文件 | rust-v0.154.0 | 文件路径 + 真实行号 |
| opencode | GitHub `sst/opencode` 分支 `dev` 的 `packages/core/src/**` | dev（2026-11 时点） | 文件路径 + 真实行号 |
| Cursor / Windsurf | 本机未找到安装产物（`C:/Users/22103` 下无 Cursor/Windsurf 程序目录）；二者源码闭源 | — | **未找到**，不臆测 |

标注：**【事实】**=直接读到的源码；**【推测】**=源码不足以证明、但有多处旁证的推断；**【未找到】**=明确没定位到。

---

## 1. 逐条回答

### A. 完全信任档下哪些操作仍弹审批？写文件是否免审批？「一次授权后不再问」的作用域是什么？

#### A.1 Claude Code（`bypassPermissions`）

**档位定义**【事实】`app.js @char 1674813-1675050`：zod 枚举
`["default","acceptEdits","bypassPermissions","plan","dontAsk","auto"]`，描述原文：
- `'acceptEdits' - Auto-accept file edit operations.`
- `'bypassPermissions' - Bypass all permission checks (requires allowDangerouslySkipPermissions).`

**bypass 的生效边界（关键的「用户以为免了其实还问」）**：
1. **bypass 是会话级，不持久化**【事实】`app.js @char 2956617` 函数 `Op()`：
   `"setMode:'bypassPermissions' is session-scoped; not persisting as defaultMode to ..."`；`@char 2954135` 函数 `Ip()`：`setMode bypassPermissions` 在未 `isBypassPermissionsModeAvailable` 时直接忽略并记日志 "rejected — mode is not available (disableBypassPermissionsMode set, or session not launched in bypassPermissions mode)"。
2. **受限模式不支持 bypass**【事实】`@char 6182255`：`var Hdt="bypassPermissions not supported in restricted mode"; function amr(e){return e.restricted&&(e.permissionMode==="bypassPermissions"||e.allowDangerouslySkipPermissions)?Hdt:void 0}`。
3. **远程/无头执行会降级**【事实】`@char 2569569` 函数 `x(e,o)`：`if(e.forRemoteExecution!==!0 ...) return o; ... return o.mode==="acceptEdits"||o.mode==="bypassPermissions"? c("default",t):t` —— remote 会话里把 acceptEdits/bypass 改回 `default`。
4. **仍会问 / 不允许持久化「总是允许」的操作**：源码用 `suppressAlwaysAllowRule` 字段表达「这张卡不能提供 persistent 的 don't ask again 行」。字段定义 `@char 1860584`；实际命中的场景包括：
   - auto-mode 分类器的 safety check：`@char 6291703` —— `suppressAlwaysAllowRule:!0`，且注释 `"this call trips a check only a person may answer, and nobody is asked about this tool's calls"`；
   - **删除类**请求：`@char 17832861` —— `...s.some((l)=>l.ruling.class==="delete")&&{defaultToNo:!0},suppressAlwaysAllowRule:!0`，即删除卡默认选「否」且不给「总是允许」；
   - 关闭网络时读他人 artifact：`@char 7496854` —— `classifierApprovable:!1, suppressAlwaysAllowRule:!0`；
   - artifact 的 ask 规则命中：`@char 7497168`、`17901057`、`17923052`、`17924289`、`17926442` 等，全部带 `suppressAlwaysAllowRule:!0`。
   - 敏感路径/记忆文件/工作区外写入仍被逐工具检查拦住：`@char 2997200-3001800` 的 `rv()`（edit 权限）里 `kc(d)→Tc`、`Ii(d)→Wi`、`Oi(d)→Bi`、`Di(d)→Ni`、`$i(d)→Fi` 等规则检查排在所有 mode 判断前面；`OF(b)&&my()`（记忆文件）直接返回 `Rc`。
5. **写文件在 bypass 档是否免审批**【事实 + 推测】：逐工具函数 `dPn()`（`@char 6494957`）开头即 `if(n.mode==="bypassPermissions")return{behavior:"passthrough",message:"Bypass mode is handled in main permission flow"}`，即工具层不自己判 bypass，**由主权限流统一放行**。【推测】主流对 passthrough + bypass 直接返回 allow；但上面第 4 点的 safety check（`suppressAlwaysAllowRule`）走的是另一条 ask 通道，因此严格来说「bypass 下写文件通常免问，但删除/敏感路径/他人内容等 safety check 仍会问」。此处未定位到主流的那一行 allow 分支（压缩 JS 中未找到 `bypassPermissions` 与 `behavior:"allow"` 同句的片段），故标为推测。

**「一次授权后不再问」的作用域**【事实】：
- 规则账本 `toolPermissionContext` 分来源：`Cp={userSettings,projectSettings,localSettings,flagSettings,policySettings,cliArg,command,toolsNarrowing,mcpServerPolicy,hostCredential,session}`（`@char 2950229`）；默认上下文 `mm()=> ({mode:"default", additionalWorkingDirectories:new Map, alwaysAllowRules:{}, alwaysDenyRules:{}, alwaysAskRules:{}, ...})`（`@char 2598316`）。
- **写文件**：`rv()` 里 `@char 3000359`：`L=(r.alwaysAllowRules.session??[]).filter(F=>{let V=yr(F).ruleContent;return ph(V)&&!h.some(q=>ih(q,V??""))})`，再 `x=L.length>0?Vlt(h,{...r,alwaysAllowRules:{session:L}},"edit"):null`，命中即 allow；授权建议 `V=F?[{type:"addRules",rules:[{toolName:It,ruleContent:F.pattern}],behavior:"allow",destination:"session"}]` —— **写文件「不再问」记的是「本会话 + 工具 Edit + 路径模式（含目录前缀）」**，这就是「同目录后续文件不再弹卡」的实现。
- **shell 命令**：授权建议 `Klt()`（`@char 3001800`）返回 `setMode:"acceptEdits"` / `addDirectories` / `Dwe(L,"session")`；命令规则则落在 `alwaysAllowRules.command` 等来源里（`d()`、`pHt()`、`@char 2565531-2566105`，`alwaysAllowRules.command` 数组追加）。
- 危险规则会被主动剥掉：`k(e)`/`t2n(e)`（`@char 2567480-2567842`）把 dangerous 规则从 `alwaysAllowRules` 移到 `strippedDangerousRules`，防止用户把 `rm` 之类永久放行。

#### A.2 Codex CLI 0.154.0

**档位**【事实】：
- `codex-rs/protocol/src/config_types.rs:101-118` `enum SandboxMode { read-only, workspace-write, danger-full-access }`。
- `codex-rs/protocol/src/protocol.rs:984-1007` `enum AskForApproval { UnlessTrusted(untrusted), OnRequest(on-request, default), Granular(GranularApprovalConfig), Never }`；`GranularApprovalConfig` 各字段为 false 时「automatically rejected instead of shown to the user」（:996-1001）。
- `codex-rs/protocol/src/protocol.rs:4056-4125` `enum ReviewDecision { Approved, ApprovedExecpolicyAmendment{..}, ApprovedForSession, ApprovedMcpPolicyAmendment, NetworkPolicyAmendment{..}, Denied{..}, TimedOut, Abort }`。

**danger-full-access 下仍会问/拒的操作**【事实】`codex-rs/core/src/exec_policy.rs:770-855` `render_decision_for_unmatched_command_for_platform()`：
- `:799-807`：`if dangerous_command_match.is_some() || windows_managed_fs_restrictions_without_sandbox_backend` → `AskForApproval::Never ⇒ Forbidden`，其它档 ⇒ `Decision::Prompt`。**即「危险命令」在完全访问档仍要弹卡（Never 档则直接拒）。**
- 危险命令的判定在 `codex-rs/shell-command/src/command_safety/is_dangerous_command.rs`：`dangerous_command_match_for_platform()` :42-47 → `dangerous_command_match_with_depth()` :49-81（支持 shell 脚本嵌套，wrapper 深度上限 8）→ `dangerous_command_match_for_exec()` :123-151，其中 `:133-135` 仅 `Some("rm") if rm_args_include_force_option(...)` 记为 `ForcedRm`；`:137-146` 递归检查 `sudo` / `env` / `trap` 包裹；Windows 另查 `windows_dangerous_commands::is_dangerous_command_windows`。**结论：Codex 的「危险命令」白名单很窄（强制删除、包裹绕过、Windows PowerShell 词），不像 kbgai 那样把 `git push`/`pip install`/`deploy` 都算高风险。**
- `:820-838`：`OnRequest` + `FileSystemSandboxKind::Unrestricted`（即 danger-full-access）且命令非危险 ⇒ `Decision::Allow`；`Restricted` 下若请求 sandbox override ⇒ `Prompt`。
- execpolicy 的 `prompt` 规则也会导致 Prompt（`prompt_is_rejected_by_policy`，`exec_policy.rs:49-53`）。

**写文件是否免审批**【事实】`codex-rs/core/src/tools/approvals.rs:778-817`：`ApprovalAction::ApplyPatch` 分支中
`if *permissions_preapproved && reason.is_none() { return ReviewDecision::Approved }`（:787-789），即**预授权/沙箱内的补丁直接放行**；否则走 `request_patch_approval`。

**「一次授权后不再问」的作用域**【事实】：
- `codex-rs/core/src/tools/approvals.rs:158-163` `enum ApprovalCacheKey { ExecCommand(UnifiedExecApprovalKey), ApplyPatch(ApplyPatchApprovalKey) }`；`:238-278` `cache_keys()`：
  - 命令：按 `canonicalize_command_for_approval(command)` + `environment_id` + `cwd` + `tty` + 权限参数记键（:248-256）—— **命令规范化后的前缀/整串**；
  - 补丁：按 `files.iter().map(|path| ApplyPatchApprovalKey{environment_id,path})`（:263-276）—— **逐文件路径**。
- 消费点：`with_cached_approval(&self.services, tool_name, cache_keys, || async {...})`（:700 命令、:801-815 补丁），命中缓存即不再向用户提问。【推测】批准确认后会把该 action 的全部 `cache_keys` 写入缓存，因此一次批准覆盖一个补丁里的多个文件；未定位到 `with_cached_approval` 函数体定义，故标推测。
- 持久化规则：`codex-rs/protocol/src/approvals.rs:34-54` `ExecPolicyAmendment` 注释明确 —— 批准「allow commands starting with this prefix」会写入 `prefix_rule(..., decision="allow")`，是**跨会话落盘的 execpolicy 前缀规则**。
- `ReviewDecision::ApprovedForSession`（`protocol.rs:4069`, `:4114` 序列化为 `approved_for_session`）= **本会话记住，不再问**。

#### A.3 opencode

**档位**【事实】opencode 没有单一「bypass」档，而是规则集：`codex` 不同，这里是 `packages/core/src/permission.ts:102-115` `evaluate(action, resource, ...rulesets)` —— `rulesets.flat().findLast(Wildcard.match(action) && Wildcard.match(resource)) ?? {effect:"ask"}`，**默认 effect 是 "ask"**。`Rule` 形如 `{action, resource, effect:"allow"|"deny"|"ask"}`，`pathActions = ["external_directory","read","edit"]`（:94-100）。

**仍会问/拒**【事实】：
- `permission.ts:212-238` `evaluateInput()`：先 `denied()` 判 deny（:290-297 抛 `BlockedError`）；再合并 `savedRules()`（:176-184，来自 `PermissionSaved`，按 projectID）；任一资源 `ask` 则整体 `ask`（:216-217）。
- `Unattended`（:59-65, :227-237）：无人值守场景（如 schedule check）下，未被 allow 的请求**直接 deny**，message：`Permission ${action} needs approval, and nobody is present to give it`。
- 写文件授权：`packages/core/src/file-write.ts:68-80` `FileWrite.authorize()` → `permission.assert({action:"edit", resources:[target.resource], save:["*"], metadata:preview...})`。**注意 `save:["*"]`：点「总是允许」会把 `{action:"edit", resources:["*"]}` 存进 saved 表（project 级）。**

**「一次授权后不再问」的作用域**【事实】`packages/core/src/permission.ts:334-395` `reply()`：
- `reply === "always" && request.save?.length` → `saved.add({projectID, action: request.action, resources: request.save})`（:369-375）—— 写文件保存的是 `edit` + `save`（实际为 `["*"]`，见上）；shell 则是 `parsed.commands.map(c=>c.save)`（`packages/core/src/tool/plugin/shell.ts:346-351`），**逐命令解析出的作用域**。
- 紧接着 :380-392 **遍历同一 sessionID + 同一 messageID 的所有 pending 请求**，重新 `evaluateInput`，若新规则已 allow，则直接 `bus.publish(Replied,"always")` + `Deferred.succeed` —— 这就是「同一轮里同批工具调用只弹一次卡、其余自动跟上」的直接实现。

#### A.4 Cursor / Windsurf
**【未找到】** 本机无安装产物、源码闭源，无法给出函数名/行号。不臆测其行为；只说明：若要做同类设计，可直接抄 A.1-A.3 的「会话级路径规则 + 批内待批一起结算」模型。

---

### B. 审批请求过期怎么处理？自动批准、自动拒绝、还是让任务收敛失败？

| 产品 | 机制 | 结论 |
|---|---|---|
| **Claude Code** | URL fetch 类审批卡：`app.js @char 7467864` `var Zms=300000, egs="The permission request for this URL was not answered in time. Ask the user to approve the fetch or include the URL in a message, then try again."`；`tgs()` 中据此判超时 | **300000ms = 5 分钟超时；不自动批准，也不静默拒绝，而是报错让用户重试/把 URL 写进消息**。【事实】 |
| Claude Code（无头/异步 agent） | `@char 6871897` `function LZ(e){return{behavior:"deny",message:qen(e),decisionReason:{type:"asyncAgent",reason:"Action requires interactive approval and permission prompts are not available in this context"}}}` | 需要交互但无界面 ⇒ **转为 deny（任务收敛失败）**，不会悬挂。【事实】 |
| Claude Code（模式漂移防护） | `@char 19845512` 权限应答的 mode 断言：`if(a!==void 0)return a===i?{policy:"accept"}:{policy:"hold",holdCause:"mode-mismatch"}; return i==="bypass"?{policy:"hold",holdCause:"no-mode-asserted"}:{policy:"accept"}` | 审批应答跨模式/缺少模式断言 ⇒ **hold（不兑现）**，防止旧授权在新档位下生效。【事实】 |
| **Codex** | `codex-rs/core/src/tools/approvals.rs:434-463` `ApprovalResolution::into_tool_result()`：`ReviewDecision::TimedOut => Err(ToolError::Rejected(guardian_timeout_message(model_info)))`；`protocol/src/approvals.rs:115-121` `GuardianAssessmentStatus::{InProgress,Approved,Denied,TimedOut,Aborted}` | **超时 = 拒绝并把失败回给模型（任务收敛为工具失败）**，不是自动批准。【事实】未找到 guardian 超时的具体秒数常量（【未找到】）。 |
| **opencode** | `permission.ts:161-174` `close()`：`Effect.forEach(pending, item => bus.publish(Replied,{reply:"reject"}) + Deferred.fail(DeclinedError))`；`:262-265` `create()` 在 `closed` 时直接 `Deferred.fail` | **服务关闭/会话结束时所有 pending 请求一律 reject**；`:227-237` 无人值守时未 allow 即 deny。【事实】该文件内**未找到**单独的「卡片 N 分钟后 TTL 过期」逻辑（【未找到】）。 |
| **kbgai 基线** | `graph.py:4248-4263` `approval_ttl_seconds()` 默认 1800 秒（夹在 60s~24h）；注释自陈历史 bug：卡片在、按钮能点，点下去 409「审批请求已过期」，任务永远停在 `waiting_approval`，界面写「进行中」，既不收敛也无重试入口 | 这是**用户「一遍一遍让我授权 / 死按钮要重跑」的直接嫌疑**；其它产品都选择「超时→拒绝并收敛失败」，kbgai 当前是「超时→悬挂」。【事实】 |

---

### C. 有没有防止「同一批工具调用对每个文件弹一次卡」的机制？

| 产品 | 机制 | 评价 |
|---|---|---|
| **opencode**（最直接） | `packages/core/src/permission.ts:380-392`：`reply("always")` 后遍历**同一 session + 同一 messageID** 的 pending，重新 evaluate，已 allow 的批量 `Deferred.succeed`；写文件 `save:["*"]`（`file-write.ts:76`）使「总是允许编辑」一次覆盖所有待批编辑 | **有**，且是会话/消息级批量结算，可直接借鉴。【事实】 |
| **Claude Code** | `rv()` 里会话路径规则 `L=(r.alwaysAllowRules.session??[]).filter(ph(V)&&!h.some(ih))`（`app.js @char 3000359`），`ruleContent:F.pattern` 是**目录/路径模式**；`addRules` 的 `destination:"session"`。授权一次后，同目录其它文件的 `Vlt(...,"edit")` 命中即 allow | **有**，靠「路径模式 + 会话规则」间接实现，而不是显式批量结算。【事实】 |
| **Codex** | `ApprovalAction::cache_keys()`（`tools/approvals.rs:238-278`）对 ApplyPatch 生成**逐文件** key；`with_cached_approval` 包住一次 `request_patch_approval`（:801-816）。一次用户批准 → 多个文件 key 入缓存 | **有**，【推测】批内多文件经同一次批准一起缓存；未定位到 `with_cached_approval` 定义处确认。 |
| **kbgai 基线** | `graph.py:4310-4311` `for call in pending:` 逐个调用 `interrupt()`（:4517），每张卡独立身份 `approval_identity(task_id, call_key, tool, args_hash)`（:4212-4234）；`remember()` 支持 `SCOPE_ONCE / SCOPE_PREFIX / SCOPE_TOOL`（`permissions.py:1000-1025`），其中 `SCOPE_TOOL` 记 `(sandbox_tier, tool, path_scope(arguments))`（:1018） | **有作用域账本，但缺「批内一次批准 → 同批其余 pending 自动结算」**；模型一轮写 6 个文件会产生 6 张卡。见建议 3。 |

---

### D. 用户设了「完全访问」后，还有哪些「以为免了其实还会问」的坑？界面如何提前说明？

**跨产品共性坑（均为源码事实）**：

1. **「完全访问」不等于永久**：Claude 的 bypass 是 session-scoped，不写默认档（`app.js @char 2956617`）；远程执行降级为 `default`（`:2569569`）；受限模式直接不支持（`:6182255`）。
2. **高风险 safety check 不给「总是允许」**：Claude 的 `suppressAlwaysAllowRule`（`:1860584`）+ 删除类 `defaultToNo`（`:17832861`）；Codex 的 dangerous 命令即使在 danger-full-access + OnRequest 下仍 Prompt（`exec_policy.rs:799-807`），在 `Never` 档直接 Forbidden。
3. **「装依赖 / git push」是否算高风险，各产品口径不同**：Codex 的 dangerous 判定很窄（`danger.rs:123-151` 主要认 forced `rm` + 包裹绕过），`git push`/`pip install` 不算；kbgai 的 `classify_action`（`automation.py:159-190`）明确把 `pip install`/`npm i `/`yarn add`/`git push`/`deploy`/`migrate`/`drop table`/`rm -rf`/`del /s` 都算高风险，且在 `MODE_BYPASS` 下会 `ask`（`permissions.py:1188-1207`）。**而 kbgai 前端文案却写「装依赖」是自动允许的（见基线 1）——这正是用户抱怨的核心来源。**
4. **粗粒度「总是允许」的反噬**：opencode 写文件点「always」保存的是 `edit` + `resources:["*"]`（`file-write.ts:76`），等于全项目编辑永久放行；Claude 则会剥掉 dangerous 规则（`k()`/`t2n()`，`app.js @char 2567480`）避免这种宽账。
5. **过期/无人值守的语义不统一**：Claude 5 分钟 URL 超时提示重试（`@char 7467864`）、无头 ask 转 deny（`:6871897`）；Codex 超时转 ToolError::Rejected（`:457-459`）；opencode 关闭即 reject（`:161-174`）；kbgai 是 409 悬挂。

**界面提前说明边界的现成做法**【事实 + 推测】：
- Claude 把「这次 ask 能不能给『不再问』」做成结构化字段 `suppressAlwaysAllowRule` / `defaultToNo` / `classifierApprovable`，宿主渲染审批卡时据此**省略**持久化选项（`app.js @char 1860584` 的注释直接指示 host：「Hosts rendering approve options should omit any persistent-rule affordance when set」）。
- Claude 的审批建议里直接带 `destination:"session"` / `addDirectories`（`@char 3001800`），用户点之前就能看见「仅本会话 / 仅这个目录」。
- kbgai 已经在卡片里带 `permission_rule` / `command_prefix`（`graph.py:4525-4526`）与 `approval_options`（:4412-4416）——**有数据，但前端文案没把「装依赖/git push 仍会问」写进「完全访问」的说明里**。

---

## 2. 对 kbgai 的可落地建议（逐条对应基线 1/2/3/4）

### 基线 1 — 前端档位文案（`web/src/components/bot/composer.tsx:240-260`）

**问题（事实）**：`permissionOptions` 里 `bypass` 的 `description` 写「工作区内自动读写文件、跑测试、**装依赖**、自动修复」（:251），但 `warning` 只列「大量删除、工作区外路径、发布、不可逆操作」（:252）。而后端 `classify_action()` 把 `pip install`/`npm i `/`yarn add` 判为 `install_dependency`，在 bypass 下依然 ask。用户据此产生「我设了完全访问怎么还问装依赖」的直接落差。另注：基线描述里的 `accept_edits` 档在本前端**并未暴露**（实际只有 default/bypass/plan 三档；后端 `MODE_ACCEPT_EDITS` 在 `permissions.py:1257` 存在）。

**改哪个函数/位置**：`composer.tsx` 的 `permissionOptions` 常量（:241）。
**文案改法**：
- `bypass.description` 删掉「装依赖」，改为「工作区内自动读写文件、跑测试、自动修复」。
- `bypass.warning` 补齐真实仍会确认的项，例：「仍需逐次确认：**安装依赖（pip/npm/yarn）、git push、数据库迁移、删除（rm -rf）、发布/部署**，以及工作区外路径与不可逆操作」。
- 参照 Claude 的结构化字段：若卡片 API 能回传类似 `suppressAlwaysAllowRule` 的标志，前端在卡片上明确显示「此项不支持『本会话总是允许』」，而不是让用户点了没生效。

### 基线 2 — 后端权限决策（`services/api/src/blank_api/services/permissions.py:1071-1270`）

**问题（事实）**：
- `decide()` 中 `MODE_BYPASS` 的 approve 在 `:1208-1209`，而 `risk:ask` 在 `:1270`（在其后）——`MODE_BYPASS` 命中后先 `:1208` 返回，永远走不到 `risk:ask`。但 `:1195-1197` 的注释却写「risk='high' 由下面 `risk:ask`/`irreversible` 两道闸门负责」，`irreversible` 在 `:1174`（确实在 bypass 前），`risk:ask` 不在。**注释与代码顺序矛盾**；`risk="high"` 的工具若没被 `classify_action` 命中，在 bypass 下会被静默放行。
- `classify_action()`（`automation.py:159-190`）用 `in` 子串匹配命令：`"npm i "`（带尾空格）、`"deploy"`、`"git push"` 等，易误判/漏判；且 `network_access` 在 `HIGH_RISK_ACTIONS`（:48）里但 `classify_action` 从不返回它——curl/wget 不会被识别为网络访问。

**改哪个函数**：`PermissionPolicy.decide()`（重点 `:1188-1209` 及 `:1267-1270` 顺序）+ `automation.classify_action()`（`:159-190`）。
**改法**：
1. 先把注释与代码对齐：明确「bypass 只挡 §13 动作签名；risk 标签本身不构成 bypass 下的 ask」。若要让 `risk="high"` 在 bypass 下也问，把 `risk:ask` 闸门前移到 `:1188` 之前；否则改注释，别留误导。
2. `classify_action` 改为**先 tokenize 再匹配**（参考 Codex `danger.rs:123-151` 的 `dangerous_command_match_for_exec`：按首个可执行名分派、`sudo`/`env`/`trap` 包裹递归、解析 shell 脚本字面命令），去掉 `"npm i "` 的尾空格脆弱匹配；补上 `network_access`（curl/wget/http 类）分类，否则前端说「网络仍会问」而实际不问。

### 基线 3 — 审批门（`services/api/src/blank_api/services/agent/graph.py:4266-4635`）

**问题（事实）**：
- `approval_ttl_seconds()`（`:4248-4263`）默认 1800s，注释已自陈死按钮/409/任务悬挂的历史事故。**当前代码只设置 `expires_at`（`:4510`）写进卡，没有在过期后主动收敛任务。**
- `for call in pending:`（`:4310`）对每个待批调用单独 `interrupt()`（`:4517`），一轮 6 个 `write_file` 会出 6 张卡。`remember(SCOPE_TOOL)`（`:4555` 经 `permissions.py:1018`）虽有 `(tier, tool, path_scope)` 账本，但**没有「批内一次批准 → 同批 pending 自动结算」**。

**改哪个函数**：`_approval_gate()`（`:4266`），配合 `PermissionPolicy.remember()`（`permissions.py:1000`）。
**改法**：
1. **过期收敛**：新增后台/前台巡检，卡超过 `expires_at` 未答时，按 Codex `ReviewDecision::TimedOut → ToolError::Rejected`（`approvals.rs:457-459`）的语义**自动记为 denied** 并给模型一条明确回执（例如「审批请求已过期，按拒绝处理；请调整方案或改为更小范围重试」），调用 `permissions.forget()` 并把任务从 `waiting_approval` 推进；界面显示倒计时，不再出现死按钮。
2. **批量免打扰**：抄 opencode `permission.ts:380-392` —— 用户点了 `SCOPE_TOOL` / `SCOPE_PREFIX` 后，立即遍历本轮剩余 `pending`，对每个 call 重新 `_decision()`，若已命中新写入的账本（`approved_tools`/`approved_prefixes`），就跳过 `interrupt()` 直接放行；前端把同 `messageID`、同工具、同目录前缀的卡合并成一张「本轮 N 个文件，是否允许该目录本会话可写」。

### 基线 4 — 用户实测场景（完全访问下搭 shopbench，反复授权 / 多轮重跑）

**问题（事实）**：截图左侧多条同名「雷姬」会话疑似重跑；用户「一遍一遍让我授权」= Card 逐文件弹（问题 3） + 过期死按钮导致重跑（问题 3 的 TTL） + 装依赖/git push 仍问但文案说免了（问题 1/2）三因叠加。

**改哪个函数/位置**：无单一函数，按上面三处联动：
1. 先改 `composer.tsx:241` 文案，消除「完全访问=什么都不问」的错误预期；
2. 再改 `_approval_gate()` 的批量结算 + TTL 收敛，让「设置一次 → 同类不再问」真正生效，即使偶尔问也不会卡死；
3. 在卡片里把已有的 `permission_rule` / `command_prefix`（`graph.py:4525-4526`）翻译成人话，例如：`v165:high_risk → 「安装依赖属于高风险，完全访问档也会先问你一次」`、`risk:ask → 「这个操作风险高，需要你确认」`，让用户知道「为什么问」而不是以为授权失效。
4. 验收建议：用用户同款 shopbench 任务（6 步、`write_file`×N、`run_shell` 测试/起服务）跑一遍，断言「装依赖若无明确授权则只弹一次卡；同目录批量写只弹一次；过期卡在 TTL 后任务状态必然离开 waiting_approval」。

---

## 3. 一句话结论

Codex / Claude / opencode 的共同答案是：**「完全访问」从来不是「零审批」，而是「低风险免问 + 高风险/删除/越界仍问 + 一次授权按会话路径/命令前缀记住」**；它们靠（1）结构化规则作用域（会话路径模式 / execpolicy 前缀 / saved 表）、（2）批内待批一起结算、（3）超时一律转拒绝并收敛失败 来避免「反复问 + 卡死」。kbgai 已有作用域账本和 30 分钟 TTL 的雏形，缺的是**前端文案对齐真实拦截项**、**classify_action 的结构化判定**、以及**批内结算 + 过期收敛**这两块；按上文基线 1/2/3/4 逐条改即可。
