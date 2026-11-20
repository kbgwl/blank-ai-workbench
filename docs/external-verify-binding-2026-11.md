# 外部调研：校验与「它覆盖的那次改动」如何绑定

调研时间：2026-11-20。要求源码级证据，不采信博客。所有引用给到文件路径与行号（二进制给字节偏移 + 可检索片段）。
凡未读到源码的结论一律标「未找到」或「推测」，不做补全。

## 0. 调研口径与版本

| 对象 | 版本/快照 | 取证方式 |
|---|---|---|
| Claude Code | 2.1.271（二进制内注释） | `claude.exe` 229,622,944 字节，取尾部 40,000,000 字节（窗口起点 189,622,944）。内部为 bun 打包的 minified 单行模块，**没有行号**，故给「尾部窗口偏移 + 片段」 |
| Codex CLI | `openai/codex` main（2026-11-20 抓取） | raw 文件 + Git tree API |
| aider | `Aider-AI/aider` main（2026-11-20） | tarball 解包，带行号 |
| opencode | `sst/opencode` dev（2026-11-20） | raw 文件，带行号 |
| Cline | `cline/cline` main（2026-11-20） | raw 文件 + tree API |
| Continue | `continuedev/continue` main（2026-11-20） | 仅探到工具清单文件（见「未找到」） |
| ESLint（作为指纹过期的参照实现） | `eslint/eslint` main | raw 文件，带行号 |

### 0.1 与本项目现状的对齐（重要，先校准任务书）

任务书说「`validation` 只有 `command` + `exit_code`，没有任何先后比较」——**这一条已经过时**，当前源码里已有：

- `graph.py:6395`：写校验记录时盖戳 `"covered_results": len(state.get("tool_results") or [])`，注释明确「这条校验是在第 N 条工具结果之后跑的」。
- `graph.py:6832-6846`：`_changes_after_validation()` 用该戳判断后面还有没有成功的写动作；**没有戳的老记录一律返回 False**（不追溯判过期）。
- `graph.py:6861-6878`：命中过期时把 `command/exit` 清空、置 `verify_stale=True`、`verify_note="校验之后又改了代码，需要重跑"`。
- `delivery.py:58`、`delivery.py:136`：`verify_stale` 已进结构化交付。

所以真正剩下的缺口是两条，调研围绕这两条展开：

1. **覆盖判据只有「工具结果序号」**：`_is_mutation()`（`graph.py:5945-5950`）只认 `MUTATING_TOOLS`（`graph.py:5906`）或 `data.change_type`。Bash 里的 `sed -i`、格式化器、代码生成器改的文件不算，序号判据会漏。
2. **交付里没有「这条校验验的是哪次改动」的可读表达**：过期时能说「之后又改了」，但没过期时无法回答「它验的就是最终这版」。

---

## 1. 结论摘要（可直接指导改动）

1. **没有一家把「校验通过」当成绑定到某次改动的持久事实来记账。** Claude Code、Codex 都只提供 hook 扩展点；aider 靠「顺序」保证新鲜（改完立刻跑），不记录覆盖关系；opencode 当前连编辑后诊断都还是 TODO。这意味着我们要做的 (a)(b)(c) 在外部**没有现成实现可抄**，能抄的是原料与判据。
2. **可用的绑定原料只有两类，且都被验证过：工具调用/结果序号**（Codex `TurnDiffTracker` 的 per-path `revision`、Claude Code hook 的 `tool_use_id`、本项目已有的 `covered_results`）**和文件内容指纹**（Claude Code 读状态的 `contentHash` = `Bun.hash(content)`）。两者互补：序号便宜但看不见越界写，指纹贵一点但能兜住 Bash/formatter 的写入。
3. **Claude Code 的成熟形态是「编辑前快照、编辑后只报增量」**：`Edit` 工具在落盘前调用 `beforeFileEdited()` 抓该文件诊断基线，编辑后 `getNewDiagnostics()` 只回注新增项。它不需要「校验过期」概念，因为诊断每次编辑后重算且只报差量。代价：只有 IDE 连着才有，且不是 pass/fail。
4. **aider 是唯一「编辑后自动跑校验」的**：`apply_updates()` → `auto_lint` → 建议命令 → `auto_test`，失败原文回注并询问是否修。它靠执行顺序保鲜，因此存在真实过期窗口：**lint 跑在 `run_shell_commands()` 之前**，后续命令（formatter/codegen）改完文件，lint 结果就已不代表最终状态。
5. **内容指纹判过期在工具链里是成熟做法，在 agent 里是空白**：ESLint 缓存用「文件内容哈希 + 配置哈希 + 版本号」决定是否复用 lint 结果；agent 侧只把 contentHash 用在「读后写」的防覆盖。把它挪到「校验覆盖」上成本最低、语义最直。

---

## 2. 逐项目对照

### 2.1 Claude Code 2.1.271

| 项 | 结论 |
|---|---|
| 是否有「编辑后校验」机制 | **没有内建 pass/fail 校验**。只有 hook 扩展点 + IDE 诊断增量。 |
| 绑定方式 | ① hook 载荷带 `tool_use_id` / `tool_name` / `tool_input` / `tool_response` / `duration_ms`，天然可按「某一次工具调用」绑定；② 诊断按**文件 URI** 建基线，编辑前抓、编辑后取差量；③ 读状态按**内容哈希**判是否被外部改过。 |
| 失效处理 | 诊断不用「失效」——每次编辑后重算增量。读状态则是硬失效：内容哈希不符就拒绝写入，要求重读。 |
| 事实/推测 | 事实（源码片段如下） |

关键源码（尾部窗口偏移，v2.1.271）：

- **hook 事件全集**（偏移 2,175,496）：
  `Sf=["PreToolUse","PostToolUse","PostToolUseFailure","PostToolBatch",...,"Stop","StopFailure","SubagentStop",...,"FileChanged",...]`
- **PostToolUse 载荷 schema**（偏移 ≈3,926,603）：
  `hook_event_name:A("PostToolUse"),tool_name:o(),tool_input:ie(),tool_response:ie(),tool_use_id:o(),duration_ms:...`
  ——`tool_use_id` 是「这次调用」的稳定标识，可直接当覆盖戳的锚点。
- **PostToolUse 派发与回注**（偏移 8,544,480）：
  `Ipt="Execution stopped by PostToolUse hook"`；循环里 `Se.additionalContexts` → `hook_additional_context`，`Se.blockingError` → `hook_blocking_error`，`Se.preventContinuation` 直接中止。
- **Stop hook 可阻断**（偏移 2,201,862 / 3,941,363）：`decision:"block"`；配置说明原文「On PostToolUse, the reason is fed back to Claude and the turn continues.」——失败回注是协议内建能力。
- **编辑前诊断基线**（偏移 8,018,055，类 `A_n`）：
  `baseline=new Map; ... lastProcessedTimestamps=new Map; ... async beforeFileEdited(e){ ... callIdeRpc("getDiagnostics",{uri:`file://${e}`}) ... this.baseline.set(h, d?d.diagnostics:[]) }`
- **基线调用点在 Edit 工具内、落盘前**（偏移 11,826,489）：
  `if(n.remoteCall===void 0)await yle.of(n.session).beforeFileEdited(pe);`
- **编辑后只取新增诊断，且限定在刚被 Edit/Write 碰过的文件**（偏移 12,201,049）：
  `async function eri(e,n){ if(!e.some((s)=>zt(s,ze)||zt(s,Wt)))return[]; let r=await yle.of(n).getNewDiagnostics(); ... return [{type:"diagnostics",files:r,isNew:!0}] }`
- **读状态用内容哈希判过期**（偏移 4,745,600）：
  `function Rsn(e){return Bun.hash(e).toString(36)}`；`function RO(e,n){if(e.contentHash!==void 0)return e.contentHash===Rsn(n);return e.content===n}`
  对应报错文案：`"File has been modified since read, either by the user or by a linter. Read it again before attempting to write it."`
- **Bash 的 PostToolUse 能拿到改动文件清单**（偏移 2,308,000 设置项原文）：
  「bashEditDiffEnabled … (PostToolUse Bash hooks get the changed-file list in tool_response)」——说明「一次写动作碰了哪些文件」在产品里被视为可见事实。

**对我们的含义**：Claude Code 证明了两件可复用的事——(1) 用 `tool_use_id`/文件集合把「一次动作」标识出来是可行的、便宜的；(2) 「编辑前快照 + 编辑后差量」比自己维护一份 pass/fail 更不容易骗人，但只能覆盖「新出现的问题」，不能回答「测试是否还代表这版代码」。

### 2.2 Codex CLI（openai/codex, Rust）

| 项 | 结论 |
|---|---|
| 是否有机制 | **未找到内建自动 lint/test**；已确认的是 PreToolUse / PostToolUse / Stop / SubagentStop 等 hook，以及按轮次的 diff 追踪。 |
| 绑定方式 | hook 请求携带 `turn_id`、`tool_use_id`、`tool_input`、`tool_response`；`TurnDiffTracker` 用 per-path `revision` 维护「轮初基线 / 当前内容」。 |
| 失效处理 | 无（不记录校验）。Stop hook 用 `stop_hook_active` 防止重复触发死循环。 |
| 事实/推测 | 事实 |

- `codex-rs/protocol/src/hooks.rs:14-34`：事件枚举 `PreToolUse`(14)、`PostToolUse`(18)、`SubagentStop`(32)、`Stop`(34)，另有 `PreModelRequest`/`PostModelRequest`/`Interrupt`。
- `codex-rs/core/src/hook_runtime.rs:197` `run_pre_tool_use_hooks`、`:320` `run_post_tool_use_hooks`、`:376` `run_turn_stop_hooks`。
- 同文件 `:320-345`：`PostToolUseRequest { session_id, turn_id, tool_name, tool_use_id, tool_input, tool_response, ... }`；注释说明 payload 已由工具侧适配成稳定契约，避免内部结构泄漏给用户 hook。
- 同文件 `:379` `stop_hook_active: bool`，`:892-906` `record_additional_contexts()` 把 hook 输出作为 developer message 注入。
- `codex-rs/core/src/turn_diff_tracker.rs:19-24`：
  `struct TrackedContent { content: String, revision: u64 }`
  `:49-64`：`TurnDiffTracker { baseline_by_path, current_by_path, origin_by_current_path, next_revision, ... }`，注释「Tracks the net text diff for the current turn from committed apply_patch mutations, without rereading the workspace filesystem」。
  `:293-294`：每次提交 `let revision = self.next_revision; self.next_revision += 1;`。

**对我们的含义**：`revision` 是单调递增的版本号，比「数组下标」更稳（不受结果列表裁剪/重排影响）。`stop_hook_active` 这个防重入位也值得照抄——如果我们要在收尾强制「补一次校验」，必须有同样的「本轮已因该 hook 继续过一次」标记，否则会无限循环。

### 2.3 aider

| 项 | 结论 |
|---|---|
| 是否有机制 | **有**：默认 `auto_lint=True`，可选 `auto_test=False`。编辑应用完之后立刻跑。 |
| 绑定方式 | **靠顺序，不靠戳**：`apply_updates()` 返回本轮被改的文件集合 `edited`，随后只对这些文件跑 lint；test 与 `edited` 非空绑定。 |
| 失效处理 | 无显式失效。`lint_outcome`/`test_outcome` 每条用户消息开头重置（`init_before_message`），属于「每轮重置」，不是「每次改动失效」。 |
| 失败处理 | 原文回注：`reflected_message = lint_errors` / `test_errors`，并先 `confirm_ask("Attempt to fix ...?")`，用户同意才进入反思循环。 |
| 事实/推测 | 事实 |

- `aider/coders/base_coder.py:105-109`：`auto_lint = True`、`auto_test = False`、`test_cmd = None`、`lint_outcome = None`、`test_outcome = None`。
- `aider/coders/base_coder.py:868-869`：`init_before_message()` 内 `self.lint_outcome = None; self.test_outcome = None`。
- `aider/coders/base_coder.py:1599-1607`：`if edited and self.auto_lint: lint_errors = self.lint_edited(edited); ... self.lint_outcome = not lint_errors; if lint_errors: confirm_ask("Attempt to fix lint errors?") → self.reflected_message = lint_errors; return`
- `aider/coders/base_coder.py:1609-1614`：`shared_output = self.run_shell_commands()` —— **排在 lint 之后**。
- `aider/coders/base_coder.py:1616-1624`：`if edited and self.auto_test: test_errors = self.commands.cmd_test(self.test_cmd); ...`
- `aider/coders/base_coder.py:1681`：`def lint_edited(self, fnames)`，逐文件 `self.linter.lint(...)`，把错误拼成字符串返回。
- `aider/args.py:543-559`：`--auto-lint` 默认 True；`--auto-test` 默认 False（帮助原文「Enable/disable automatic testing after changes (default: False)」）。

**对我们的含义**：aider 的「只 lint 本轮改过的文件」正好对应我们的「改动文件集合」，成本很低；但它的顺序漏洞（lint → 任意命令 → test）说明：**跑校验之前必须把所有可能写文件的步骤收口**。我们的 `verify_hook` 是在工具循环里插跑，天然比它安全，但要确认校验之后没有会改文件的收尾步骤。

### 2.4 opencode

| 项 | 结论 |
|---|---|
| 是否有机制 | **格式化有，校验/诊断没有**。 |
| 绑定方式 | 格式化绑定在写入提交上：`FileWrite.commit()` 返回 `result.formatted`，工具随即把「格式化后的内容」当 diff 返回给模型——模型看到的是格式化后的最终态，不是自己刚写的那版。 |
| 失效处理 | 不涉及。 |
| 事实/推测 | 事实（含一处显式 TODO） |

- `packages/core/src/tool/plugin/edit.ts:254-257`（**原文**）：
  ```ts
  /** Deferred edit behavior and UX integrations remain visible at the model-facing seam. */
  // TODO: Publish watcher/file-edit events after watcher integration exists.
  // TODO: Add snapshots / undo after design exists.
  // TODO: Add LSP notification and diagnostics after LSP runtime exists.
  ```
  ——`dev` 分支当前**没有**编辑后诊断，且是明确记账的待办，不是「认为不该做」。
- `packages/core/src/tool/plugin/edit.ts:355-363`：
  `const result = yield* fileWrite.commit(target, replaced); const formatted = result.formatted ?? (readText(...)).text; return { output: ..., metadata: { files: [fileDiff(result.resource, source, formatted)] } }`
  ——注意 `fileDiff(resource, source, formatted)`：源是本轮读到的 `source`，终态是 `formatted`，中间任何格式化副作用都包含在 diff 里。
- `packages/core/src/formatter.ts:49-90`：`Formatter.file(target)` 按扩展名匹配 formatter，`processes.run(cmd)`，`exitCode === 0` 才算成功；失败只 `logError`，不阻断写入。
- `packages/core/src/plugin/hooks.ts`：插件 hook 有 `tool` 域（`ToolHooks` / `ToolFailures`），注释「a Tool.Error rejects the call before it runs」，属扩展点，不是内建校验。

**对我们的含义**：**「写入即格式化，并把格式化后的终态作为工具结果」**是一个被验证过的、能消掉一大类「校验后又变」的做法。我们的 `edit_file`/`write_file` 若接了 formatter，必须在同一工具结果内返回终态，并让改动指纹基于终态计算。

### 2.5 Cline

| 项 | 结论 |
|---|---|
| 是否有机制 | 有**诊断增量**工具函数（按文件做 deepEqual 差集），与 Claude Code 同族；当前 main 的接线点未定位。 |
| 绑定方式 | 两次快照（old/new `FileDiagnostics[]`）按 `filePath` + 逐条 `deepEqual` 求新增。 |
| 失效处理 | 不涉及「过期」，只报新增。 |
| 事实/推测 | 函数实现=事实；「谁在编辑后调用它」=**未找到**（见第 5 节） |

- `apps/vscode/src/integrations/diagnostics/index.ts:7-26`：
  ```ts
  export function getNewDiagnostics(oldDiagnostics: FileDiagnostics[], newDiagnostics: FileDiagnostics[]): FileDiagnostics[] {
      const oldMap = new Map<string, Diagnostic[]>()
      for (const diag of oldDiagnostics) oldMap.set(diag.filePath, diag.diagnostics)
      ...
      const newProblemsForFile = newDiags.diagnostics.filter(
          (newDiag) => !oldDiags.some((oldDiag) => deepEqual(oldDiag, newDiag)),
      )
      ...
  }
  ```
- 同文件 `:31-61`：`diagnosticsToProblemsString(diagnostics, severities)` 把诊断渲染成 `- [source severity] Line N: message`，说明它是给模型看的文本。

**对我们的含义**：差集的判据是「诊断条目的结构相等」，而不是「文件哈希」。这比文件哈希更精确（只关心错误是否新出现），但要求有诊断源（LSP/IDE）。我们若没有 LSP，只能用文件哈希兜底。

### 2.6 Continue

| 项 | 结论 |
|---|---|
| 是否有机制 | **未找到**。内置工具清单里没有 lint/test/verify 类工具。 |
| 证据 | `core/tools/builtIn.ts:1-31` 枚举 `LSTool`、`CodebaseTool`、`EditExistingFile`、`SingleFindAndReplace`、`MultiEdit` 等，无校验工具。 |
| 事实/推测 | 仅此为事实；「确认全仓无编辑后自动校验」=未完成，标未找到 |

### 2.7 参照实现：ESLint 结果缓存（问题 3 的正面答案）

| 项 | 结论 |
|---|---|
| 是否有机制 | **有**，是「校验结果按内容指纹失效」的标准实现。 |
| 绑定方式 | 缓存条目 = 文件内容指纹（可配 metadata=mtime+size 或 content=checksum）+ **配置哈希**（含 ESLint 版本、Node 版本、序列化后的 config）。任一不符即不复用。 |
| 失效处理 | 惰性失效：下次读取时比对，不匹配直接丢弃重算。 |
| 事实/推测 | 事实 |

- `lib/cli-engine/lint-result-cache.js:34`：`const validCacheStrategies = ["metadata", "content"];`
- 同文件 `:53-57`：`hashOfConfigFor(config)` → `hash(`${pkg.version}_${nodeVersion}_${stringify(config)}`)`
- 同文件 `:95-104`：
  「useModifiedTime: If true (default), use mtime and size … useCheckSum: If true, use hash of the content … For the "content" cache strategy, it is important to set useModifiedTime to false.」
  `fileEntryCache.create(..., { useModifiedTime: !useCheckSum, useCheckSum })`
- 同文件 `:169-191`：
  `const fileDescriptor = this.fileEntryCache.getFileDescriptor(filePath); ... const changed = fileDescriptor.changed || fileDescriptor.meta.data.hashOfConfig !== hashOfConfig; if (changed) return null;`
- 同文件 `:228-235`：写回时同时存 `hashOfConfig` 与 `results`。

**对我们的含义**：这就是「校验覆盖戳」的完整范式——**戳 = (被校验对象的指纹, 校验器版本/配置指纹)**。我们的 `validation` 记录缺的正是后者：命令字符串本身不足以区分「pytest 换了参数/依赖升级」。

---

## 3. 可抄的做法（按改造成本排序）

### P0：不动架构，只补两个字段与一句交付文案（半天内）

1. **校验戳从「结果下标」升级为「结果下标 + 改动集合指纹」**（对应本项目 (a) 的剩余部分）。
   - 现状已有 `covered_results`（`graph.py:6395`）。补 `covered_files: {path: content_hash}`（或 `{path: [mtime, size]}`，冲突时以 hash 为准），在跑校验前对 `_change_facts()` 得到的文件集合逐个取指纹。
   - 判过期时 `covered_results` 与指纹**取或**：序号说「后面又写过」或指纹说「文件已不是那版」都算过期。这样能兜住 Bash/格式化器这类不进 `MUTATING_TOOLS` 的写入（当前 `_is_mutation` 的盲区，`graph.py:5945-5950`）。
   - 指纹算法直接抄 Claude Code 的形态：`sha256(content)` 或 `Bun.hash` 同类（Claude Code 偏移 4,745,600；ESLint 用 checksum，`lint-result-cache.js:95-104`）。不要只用 mtime——Claude Code 自己在 NotebookEdit 里保留 mtime 分支（偏移 10,969,806，`await I_(O)>F.timestamp`），但 Edit 路径已经用 contentHash，说明纯时间戳不够。
2. **交付行回答「验的是哪次改动」**（对应 (c)）。
   - 现在 `delivery.py:109` 只输出 `验证：<cmd>（退出码 N）`。建议在同一行尾追加覆盖范围，例如 `验证：pytest -q（退出码 0，覆盖 3 个文件 / 改动 #12 之后）`。
   - 措辞要跟 `delivery.py:32` 的既有约定一致：`验证：` 前缀只留给真跑过且未过期的记录；过期仍走 `未跑校验：校验之后又改了代码，需要重跑`（已有，`graph.py:6873`）。
   - 结构化侧同步加 `verify.covered_files` / `verify.covered_through`（`delivery.py:130-137` 已有 `missing`/`stale` 的位置）。
3. **失败回注保持现状即可，但补一条「同一改动不重复跑」**。
   - Codex 的 `stop_hook_active`（`hook_runtime.rs:379`）是防死循环的标准做法。我们的 `verify_hook` 节流是「每 3 次改动 / 隔 4 步」（`verify_hook.py:25-27`），本质是同一目的，但它是**全局计数**，不区分「这次改动是否已经验过」。建议在 `validation` 上记 `covered_results`，`should_check()` 里加一条：若当前 `len(tool_results) == covered_results` 则不再跑（幂等）。

### P1：把「校验必须晚于最后一次改动」变成收尾闸门（1-2 天）

4. **收尾前做一次 gate**：进入交付渲染前，若 `changed_files > 0` 且（无校验 或 stale），先尝试补跑一次（受 `BLANK_POST_EDIT_CHECK` 配置约束），仍失败才降级为 `未跑校验：…`。
   - 这是 Claude Code `Stop` hook `decision:"block"`（偏移 2,201,862）与 Codex `run_turn_stop_hooks`（`hook_runtime.rs:376`）的共同形态：**在「要停」的那一刻检查，而不是在「每次编辑」时检查**。好处是只跑一次、不会打爆上下文；坏处是必须防重入。
   - 降级路径必须保留：没配命令、环境不可用、用户拒绝授权，都要能说清原因（`verify_hook.py` 已有的 `failure_note` 与 `validation.status == "skipped"` 分支，`graph.py:6370-6380`）。
5. **校验前先收口写动作**。aider 的反例（lint 在 `run_shell_commands` 之前，`base_coder.py:1599-1614`）说明：只要校验之后还有任何可能写文件的步骤，戳就白盖。我们的 loop 要在跑校验前确认没有待执行的写工具调用，并把 formatter 之类的副作用归到「同一次写」里（opencode 的做法：`edit.ts:355-363` 把格式化后的终态作为工具结果返回）。

### P2：编辑前快照 / 编辑后差量（成本高，暂不建议）

6. Claude Code 的 `beforeFileEdited` + `getNewDiagnostics`（偏移 8,018,055 / 12,201,049）与 Cline 的 `getNewDiagnostics`（`diagnostics/index.ts:7-26`）都需要一个诊断源（LSP/IDE）。我们没有，硬做只能退化成「跑一次 lint 比对新旧输出」，成本接近 P1 的 gate，收益却只有「新增问题」，不如直接做 gate。

---

## 4. 明确不要抄的（附理由）

1. **不要抄 aider 的「lint → 任意 shell → test」顺序**（`base_coder.py:1599-1624`）。lint 与 test 之间夹着 `run_shell_commands()`，模型建议的 formatter/codegen 会在 lint 之后改文件，lint 结论当场过期。要抄就抄它「只 lint 本轮改过的文件」这一点，顺序反过来：先收口写，再校验。
2. **不要抄「只按 mtime 判过期」**。Claude Code 在 NotebookEdit 里保留了 mtime 分支（偏移 10,969,806），但同一产品的 Edit 路径已改成 `contentHash` 比较（偏移 4,745,600，`RO()` 函数）。mtime 在 `git clone`、checkout、格式化器 touch 时会假阳性，ESLint 文档也专门提醒 content 策略必须关掉 mtime（`lint-result-cache.js:95-104`）。
3. **不要把「校验通过」存成不可撤销的布尔**。Claude Code、Codex 都没有这个字段；aider 的 `lint_outcome` 也只是每轮重置的临时值（`base_coder.py:868-869`），全仓没有任何地方消费它做门禁。说明「一次通过 = 永久通过」在工程上没人敢用。
4. **不要抄「依赖 IDE 连接才有诊断」**。Claude Code 的 `beforeFileEdited` 在 `mcpClient.type !== "connected"` 时直接 return（偏移 8,018,055 起，`if(!this.initialized||!this.mcpClient||this.mcpClient.type!=="connected"...)return`）。headless/CI 场景下这条链路整段失效，不能作为交付判据的唯一来源。
5. **不要照抄「每次编辑后都跑全量测试」**。aider 把 `--auto-test` 默认设为 False（`args.py:554-559`），opencode 干脆没有这个能力（`edit.ts:256` TODO）。成本是明摆着的：全量测试分钟级，编辑是秒级。我们的节流（每 3 次改动 / 隔 4 步）方向是对的，只需补「幂等」而不是取消节流。

---

## 5. 未找到的部分（明确留白，不编）

1. **Cline 当前 main 分支「编辑后调用 `getNewDiagnostics`」的接线点**：`apps/vscode/src/integrations/diagnostics/index.ts` 只有函数定义；`apps/vscode/src/core/` 下已无 `task/index.ts`，agent loop 迁到 `sdk/packages/core`，未在其中定位到调用方。故只能确认「能力存在」，不能确认「何时触发」。
2. **Continue 是否存在编辑后自动校验**：仅确认 `core/tools/builtIn.ts:1-31` 的工具清单里没有 lint/test/verify；未做全仓检索，不能断言「完全没有」。
3. **任何 coding agent 用「内容哈希」判断「校验结果过期」的先例**：没有找到。内容哈希在 agent 里只用于「读后写」防覆盖（Claude Code `contentHash`）；在工具链里用于「结果缓存失效」（ESLint）。我们要做的「校验覆盖戳 = 文件指纹」在这两类之间，属于空白，没有直接对标物。
4. **明确写「我们故意不做校验绑定，因为成本大于收益」的原文**：没有找到。最接近的两条都不是「明确拒绝」——opencode 是 `TODO`（`edit.ts:256`，等 LSP runtime），aider 是默认值选择（`args.py:554`，`--auto-test` 默认 False），都没有给出理由文字。Claude Code 与 Codex 则把这件事整体外包给 hook，源码里没有「为什么不做」的说明。
5. **Claude Code 是否存在内建「编辑后自动 lint/test」**：在 40MB 明文段内未找到除 IDE 诊断与 hook 之外的实现；不排除在未解包的 bytecode 模块里，故结论限定为「明文段内未找到」。

---

## 6. 一句话给实现者

外部没有可直接搬的「校验覆盖」实现；**能搬的是三样判据**：Claude Code 的 `contentHash`（指纹）、Codex 的 `revision` 单调版本号（序号）、ESLint 的 `(内容哈希, 配置哈希)` 二元组（校验器版本）。把这三样合成一个 `validation.covered = {results_index, files_hash, command_hash}`，收尾时比一次、交付里写清楚，就够了。
