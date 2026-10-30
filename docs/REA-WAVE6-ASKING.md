# REA 第六波：Codex 0.154.0 与 Claude Code 2.1.271 怎么实现"不懂就问"

日期：2026-10-30
背景：0.8.13/0.8.15 给自家做了一层"写盘前事实闸门"（`agent/unknowns.py`）——用户没给过的
电话/邮箱不许落盘、由服务端反问。用户问：「用 REA 逆向 codex 和 claude，看看他们怎么实现的
这部分功能」。本文只回答这一件事：**这两家怎么让模型在不知道时开口问，以及问到之后怎么处理。**

方法：只认实物，两条腿走路：

1. **静态**：本机二进制的字符串产物（`E:/env/rea/out/codex.uniq.txt` 90,511 条 /
   `claude.uniq.txt` 318,134 条），以及包内 `sdk-tools.d.ts`（工具入参的权威定义）。
   引用带产物行号，可用 §9 命令复读。
2. **动态**（本轮新增装置）：把两家的 `base_url` 指到本机抓包代理
   （`E:/tmp/rea6/capture_proxy.py`，127.0.0.1:8787，原样存 body，只回一个最小合法响应），
   跑一次最小请求，得到**模型真正收到的那份东西**：`instructions` / `system` + `tools`。
   这一步把"从字符串猜"换成了"看现网报文"。

## 0. 本轮取证清单

| 实物 | 形状 | 用途 |
| --- | --- | --- |
| `codex.exe` | 298,169,136 B，Rust 单文件 | 提示词、工具 handler 的报错串 |
| `claude.exe` | 229,622,944 B，Bun 单文件 | 提示词、`confirmWithUser` 的返回类型 |
| `capture/req-01-v1_responses.json` | 54 KB，Codex 真实请求 | **8 个工具** + 17,174 字指令 |
| `capture/req-01-v1_messages_beta_true.json` | 80 KB，Claude 真实请求（`-p`） | **23 个工具** + 5,657 字系统提示 |
| `capture/req-03-...json` | 85 KB，Claude 真实请求（`--permission-mode plan`） | 证明 plan 模式**也不加**提问工具 |
| `sdk-tools.d.ts` | 168 KB | `AskUserQuestionInput`（第 1058 行）1-4 问 × 2-4 选项 |

实测环境：Codex 0.154.0（`CODEX_HOME=E:\env\codex\home`）；Claude Code 2.1.271
（`CLAUDE_CONFIG_DIR=E:\env\claude\home`，网关 bigmodel / glm-4.5-flash，入口 `sdk-cli`）。
抓包时两家的**模型**都不是官方模型（我们的网关），但 `tools` / `instructions` / `system`
由客户端组装，与模型无关——这正是我们要看的东西。

## 1. 一句话结论

两家都不是"遇到不懂就停下来问"，而是**把"问"绑在一个阶段上**：

- **Codex**：提问工具是 **Plan 模式专属**。Default 模式（也就是干活模式）的指令里
  一次都不提这个工具，还明确写"强偏好自己做合理假设"。
- **Claude Code**：提问工具跟着**有没有人能回答**走——非交互入口（`-p` / SDK）里
  干脆不把 `AskUserQuestion` 放进工具表。

两家都在做同一件事：**让"问"发生在写之前的那一阶段，而不是在写的过程中被打断。**
这跟我们 0.8.15 的做法是两条路：我们是"写的时候被拦下"（兜底），他们是"写之前先问清"（前置）。
两者不冲突，但**只做我们这一层，用户看到的仍然是"它干到一半才来问"**。

## 2. Codex：提问是 Plan 模式的专属能力

### 2.1 工具定义（现网报文原文，`codex_tools.json`）

```json
{
  "type": "function",
  "name": "request_user_input",
  "description": "Request user input for one to three short questions and wait for the response. This tool is only available in Plan mode.",
  "parameters": {
    "properties": {
      "questions": {
        "description": "Questions to show the user. Prefer 1 and do not exceed 3",
        "items": {
          "properties": {
            "header":  { "description": "Short header label shown in the UI (12 or fewer chars)." },
            "id":      { "description": "Stable identifier for mapping answers (snake_case)." },
            "options": { "description": "Provide 2-3 mutually exclusive choices. Put the recommended option first and suffix its label with \"(Recommended)\". Do not include an \"Other\" option in this list; the client will add a free-form \"Other\" option automatically." },
            "question":{ "description": "Single-sentence prompt shown to the user." }
          },
          "required": ["id", "header", "question", "options"]
        }
      }
    }
  }
}
```

三个细节值得抄：`id` 是**必填**且要求 `snake_case`（答案按 id 回填，不靠顺序）；
`options` 是**必填且不能为空**（运行时也会挡：`request_user_input requires non-empty options
for every question`，`codex.uniq.txt` L77230）；`header ≤ 12 字符`。

顺带一个反面读数：**exec（无人）模式下这个工具仍然出现在工具表里**（我们抓到的就是
8 个工具含它），只是 handler 会拒绝——`request_user_input is not supported in exec mode for
thread ...`（L23400）、`requires an interactive stdin terminal`（L77229）。
工具列着但调了必失败，模型只能靠指令自己收敛（见 §7 不借）。

### 2.2 两种模式，两套指令

Default 模式（现网抓到的 17,174 字指令里，**"request_user_input" 出现 0 次**）：

> In Default mode, strongly prefer making reasonable assumptions and executing the user's
> request rather than stopping to ask questions.
> Use the `request_user_input` tool only for optional questions where the answer would
> materially improve the quality of the work.
> If `request_user_input` returns no answers, continue with best judgment instead of asking
> again or treating the turn as blocked.
> （`codex.uniq.txt` L1799，collaboration_mode 模板 default 分支）

Plan 模式（同一模板的 plan 分支，字符串产物 L15873 起）：

> ## Asking questions
> Critical rules:
> * Strongly prefer using the `request_user_input` tool to ask any questions.
> * Offer only meaningful multiple-choice options; don't include filler choices...
> * In rare cases where an unavoidable, important question can't be expressed with reasonable
>   multiple-choice options (due to extreme ambiguity), you may ask it directly without the tool.
> You SHOULD ask many questions, but each question must:
> * materially change the spec/plan, OR
> * confirm/lock an assumption, OR
> * choose between meaningful tradeoffs.
> * not be answerable by non-mutating commands.
> **Bias toward questions over guessing: if any high-impact ambiguity remains, do NOT plan yet—ask.**

### 2.3 最值得抄的一条：两类未知，处理方式相反

Plan 模式模板里有一段 `## Two kinds of unknowns (treat differently)`：

> 1. **Discoverable facts** (repo/system truth): explore first.
>    * Before asking, run targeted searches and check likely sources of truth
>      (configs/manifests/entrypoints/schemas/types/constants).
>    * Ask only if: multiple plausible candidates; nothing found but you need a missing
>      identifier/context; or ambiguity is actually product intent.
>    * Never ask questions you can answer from your environment.
> 2. **Preferences/tradeoffs** (not discoverable): ask early.
>    * Provide 2–4 mutually exclusive options + a recommended default.
>    * If unanswered, proceed with the recommended option and record it as an assumption in
>      the final plan.

这条把"该不该问"从态度问题变成了**分类问题**：能查的自己查，查不到的（偏好、取舍）早问，
而且**不答也有默认动作**（按推荐项走并写成假设）。我们现在的提示词是一整块"先问再干"，
没有这层分类——它既会把可查的事拿去问人，也会在真正该问的偏好上自己拍。

### 2.4 提问工具不许拿来问权限

> Never use the `request_user_input` tool for permission requests or permission-related
> escalations.
> If explicit user input is required for another reason before progress can safely continue,
> do not use the `request_user_input` tool. Ask the user directly with one concise plain-text
> question instead. Never write a multiple choice question as a textual assistant message.
> （L41035 / L37248）

Codex 把"要授权"和"要信息"分成两条完全不同的通道：授权走审批（approval policy /
auto-review），信息走提问，两者不共用按钮。**我们恰好相反**——审批与提问共用同一条
interrupt 通道，只靠 `kind` 区分（这是我们的取舍，换来了 UI 只有一套；代价是语义混在一起）。

### 2.5 计划即审批

> Do not ask "should I proceed?" in the final output. ... if you have included a
> `<proposed_plan>` block in your response.
> （L30813）

计划用 `<proposed_plan>` 交给客户端渲染，客户端给"实施 / 改"按钮。也就是说
**"要不要开始干"这个问题本身不占一次提问**，它被结构化成一次计划提交。

## 3. Claude Code：提问是"有没有人能回答"的产物，答案是有类型的

### 3.1 实测：非交互入口不带提问工具

`-p`（sdk-cli）抓到的请求里 **23 个工具，没有 `AskUserQuestion`**：

```
Agent CronCreate CronDelete CronList Edit EnterWorktree ExitWorktree Glob Grep
ListAgents NotebookEdit PowerShell Read ReportFindings ScheduleWakeup SendMessage
Skill TaskOutput TaskStop WebFetch WebSearch Workflow Write
```

加 `--permission-mode plan` 再抓一次（85 KB），**仍然没有**（同一份 23 个）。

这条读数比任何提示词都硬：Claude 的做法是**没人可回答就不给工具**，而不是给了再在
handler 里拒绝。工具表的形状本身就是"当前有没有人类在旁边"的答案。

> 反证（我们只能拿到静态证据）：包内 `strings` 有 `AskUserQuestionPermissionDialog`
> （L103490）、`ui.render / component=AskUserQuestion`（L67992）、
> `AskUserQuestion always includes a Skip button and a free-text input box for custom answers`
> （L132555），说明交互态确实有这块 UI。本机无 TTY，交互态的**工具表**未能实测（见 §8）。

### 3.2 工具入参（`sdk-tools.d.ts` 第 1058 行）

- 1-4 个问题；每题 **2-4 个选项**；`header` ≤ 12 字符；
- 选项 `label` + `description` 必填，`preview` 可选（用于并排对比代码/mockup）；
- 原文：`There should be no 'Other' option, that will be provided automatically.`
- 有 `multiSelect`；Skip 是 UI 内置的第三条出口（不在选项里）。

与 Codex 的差别：Claude 4 问 4 选项，Codex 3 问 3 选项；两家都不许把"其他/跳过"写进选项。
**注意：这两家的"选项"都是给"偏好/取舍"用的**。我们闸门问的是"你的真实电话是多少"——
这类事实天然是填空不是选择，照搬"必须 2-4 个选项"会把我们的问题问歪（见 §6 #150）。

### 3.3 "没有答案"是一等公民（这是最该抄的一条）

内部调用方 `confirmWithUser` 走完权限层后，返回值被归一成四种（`claude.uniq.txt` L107251）：

```js
function VCs(e,n,r,s,d){
  if(V7t(e,n)) return {kind:"unanswered", reason:"no_one_to_ask"};
  if(r.aborted) return {kind:"aborted"};
  switch(e.behavior){
    case "allow":
      // 用户选了：{kind:"chose", value}
      // 用户自己打字了：{kind:"declined", typedAnswer}
      // 按了跳过 / 空：{kind:"unanswered", reason:"no_selection"}
    case "deny":
      // 异步代理拒的 / 其他"没人可问"：{kind:"unanswered", reason:"no_one_to_ask"}
      return {kind:"declined"};
    case "ask": return {kind:"declined"};
  }
}
```

并且**每个调用方都必须穷举 `kind`**，各自映射成业务动作（例如 `retry_fallback` /
`cancelled`），不存在"当成空字符串继续跑"这条路。

还有一条硬约束：内部要问之前先断言权限层真的会弹卡——

```js
let O = await M3.checkPermissions(b.data, n);
if (O.behavior !== "ask")
  throw new R(`confirmWithUser(${r}): AskUserQuestion.checkPermissions answered ${O.behavior}, not ask`);
```

意思是：**我打算问人，那就必须真的问到人；权限配置把它自动放行了，宁可抛错也不静默继续。**
这正是我们上一轮那个 bug 的镜像：卡没弹出来，任务却往前跑了。

### 3.4 答案怎么回到上下文

`[User answered AskUserQuestion]: <选项>` 作为一条 **user 文本块**回注，并打上
`harnessMarked:!0`（L184606）。同一位作者在审阅侧还写明：审阅代理应当"把用户选的那个
选项当作直白意图"，但**只能按选项本身的粒度记账，不能顺带当成宽泛授权**
（L89926：`credit the user with the option they picked ... not with any standing or broad
authorization`）。我们的 `decide()` 只放行被确认的那一处值，口径一致。

### 3.5 全局基调：默认别停，plan 模式才多问

主系统提示（抓包 `claude_system.txt` 只有 5,657 字，入口 `cc_entrypoint=sdk-cli`）：

> For actions that are hard to reverse or outward-facing, confirm first unless durably
> authorized or explicitly told to proceed without asking; approval in one context doesn't
> extend to the next.

完整 CLI 里那条更直白（`claude.exe` 偏移 202,089,330）：

> Bias toward working without stopping for clarifying questions — when you'd normally pause to
> check, make the reasonable call and keep going; they'll redirect you if needed. If the user,
> a skill, or the shape of the task suggests they want you to ask (with [AskUserQuestion] or
> otherwise), do so. And even absent that signal, it's still fine to stop when you're genuinely
> blocked — unclear direction, missing input, a decision only they can make.

**注意最后半句**：`missing input` 和 `a decision only they can make` 被明确列为"停下来问"的
合法理由——这正好覆盖我们闸门管的那类事实（只有用户知道的电话/邮箱）。
而 plan 模式的注入提醒则要求问到底（偏移 202,065,300）：

> Answer the user's query comprehensively, using the [AskUserQuestion] tool if you need to ask
> the user clarifying questions. If you do use the [AskUserQuestion], make sure to ask all
> clarifying questions you need to fully understand the user's intent before proceeding.

### 3.6 计划审批与提问严格分开

> ... to request plan approval. Do NOT ask about plan approval in any other way - no text
> questions, no AskUserQuestion. Phrases like "Is this plan okay?", "Should I proceed?",
> "How does this plan look?" ... MUST use [ExitPlanMode].
> （L59401 / L74910；plan 模式版 L147497：`the user cannot see the plan until you call
> [ExitPlanMode] for approval`）

和 Codex 的 `<proposed_plan>` 是同一个决定：**"行不行 / 干不干"属于审批通道，
"要什么"属于提问通道**，两家都不许混。我们把两者塞进同一条 interrupt，是本轮最该
重新考虑的一处（不是为了现在改，而是要知道自己站在哪）。

## 4. 三家并排

| | 我们（0.8.15） | Codex 0.154 | Claude 2.1.271 |
| --- | --- | --- | --- |
| 提问通道 | 与审批共用 interrupt，`kind` 区分 | 独立工具 + 独立 `UserInputAnswer` 数据模型 | 独立工具 + 权限层强制 `ask` |
| 什么时候能问 | 任何一步（含写盘前被闸门拦下） | **Plan 模式**；Default 只许"可选提升质量"的问题 | 交互态随时；**非交互就不给工具** |
| 没人可问会怎样 | 30 分钟过期，`answers={}`，值仍被拦（不编） | `continue with best judgment`（继续猜）+ handler 拒绝 | `{kind:"unanswered", reason:"no_one_to_ask"}`，调用方必须处理 |
| 问题形状 | 1-4 问，选项 0-6（可纯填空） | 1-3 问，选项 **必填 2-3**，首项即推荐 | 1-4 问，选项 **2-4**，有 multiSelect / preview |
| 答案缺口处理 | `decide()`：allow/blank/value/delegate/none | 没答案=按推荐项走，并写进计划里的假设 | 四类 `kind` 穷举，禁止当空串 |
| 写盘前查事实出处 | **有**（`unknowns.py`，编的电话/邮箱不许落盘） | 无 | 无 |
| 计划即审批 | 无 | `<proposed_plan>` 块 | `ExitPlanMode` 工具 |

**我们的独特项**：两家都**没有**"写之前查这个值有没有出处"这一层。Codex 靠 plan 阶段
先把偏好问掉，Claude 靠模型自觉 + 审阅代理事后判，两家对"模型编一个像样的电话写进页面"
都没有确定性拦截。这一层是我们自己长出来的，别丢。

**我们的缺口**：两家都把"问"放在**阶段**上（先 plan 再干），我们只有"拦截"没有"阶段"。
所以用户体感仍然是"它先干到一半，才跑来问我要电话"。

## 5. 可借清单（每条都带能失败的判据）

### #147 两类未知：能查的自己查，查不到的早问（借 Codex §2.3）

提示词里把"先问再干"拆成两段：`discoverable → 先查再问`、`preference → 早问 + 给推荐项 +
不答就按推荐走并写成假设`。
**判据**：新增 `test_prompt_v148.py` 断言提示词含两段且顺序在「做事规则」之前；
再用真机探针跑「帮我做个公司官网」，看它是否**先查工作区**（工具序列里出现 read/glob）
而不是一上来就问公司名。

### #148 从零任务先出方案，再动手（借 Codex `<proposed_plan>` / Claude `ExitPlanMode`）

对"从零创建"类任务（空工作区 + 交付物是页面/文档），第一步产出方案（要什么页、联系方式
从哪来、哪些值需要用户给），方案落成一次审批，再进入执行。
**判据**：空目录 + 「做个公司官网」→ 事件流里必须出现 `plan.proposed` 且在执行第一个
写调用之前；`pending_approvals` 在写之前出现一次。**成本**：多一轮往返，只对 from-zero
任务开，存量仓库改造不开（避免又变成"每步都停"的旧毛病）。

### #149 "没人可问"要成为显式类型（借 Claude §3.3）

现在未回答只是 `answers={}`，事件里记 `answered: 0`，语义靠下游推断。改成
`question.answered` 的 payload 带 `outcome: answered | unanswered | no_one_to_ask | expired`，
并有调用方分支：`unanswered` → 照拦 + 交付里明说缺什么（已有）；`no_one_to_ask`（客户端
不在线/超时）→ 收尾时把"缺哪几处"写进交付，而不是只说"做完了"。
**判据**：把 30 分钟压到 1 秒的测试里，事件 payload 必须出现 `outcome="expired"`，且
交付正文含"待补充"。

### #150 选项与推荐位：只给"偏好类"问题加约束，事实类保持填空（借两家 schema，但不照抄）

Codex 强制"选项非空 + 首项即推荐"，Claude 强制"2-4 选项 + label/description"。
我们的闸门问的是**值**（电话/邮箱），不能强制选项。做法：`normalize_questions` 增加
`kind: "choice" | "value"`——`choice` 走两家的校验（2-4 选项、label+description、推荐位），
`value` 保持现在的纯填空（允许 0 选项 + 自由文本）。
**判据**：单测——`kind="choice"` 且选项 <2 时返回 `fail`；`kind="value"` 且选项为空时照常通过。

## 6. 不建议照搬

- **Codex 的"Default 模式别问、自己做假设"**。这正是我们这个 bug 的来源：用户没给电话，
  它按"合理假设"编一个。对**代码实现细节**这句是对的，对**只有用户知道的事实**是错的。
- **Codex 在 exec 里仍列出提问工具、调用时再拒绝**。工具表与真实能力不一致，模型只能靠
  指令自我约束（它的提示词里那堆"only when it is listed in the available tools"就是补丁）。
  我们要么给、要么不给，别给一个调了就报错的。
- **Claude 的"非交互就不给工具"**。他们是 CLI，没终端就等于没人；我们的产品是桌面应用，
  用户就在旁边看着，提问通道必须在。
- **把提问做成"选项选择"**。选项适合偏好（用哪个库、什么风格），不适合事实（电话是多少）。
  硬把事实塞进选项，用户最后的动作一定是选"其他"再手打。

## 7. 本轮没有验证的

1. **Claude 交互态的真实工具表**：本机无 TTY，只能给字符串证据（`AskUserQuestion` 的 UI 与
   权限检查存在），未能抓包证明它在交互态被列出来。可失败判据很明确：有 TTY 时抓一次
   `/v1/messages`，`tools` 里应出现 `AskUserQuestion`。
2. **Codex 的 plan 模式报文**：`codex exec` 没有开关进 plan 模式（plan 是 TUI/app-server 的
   协作模式），所以 §2.2 的 plan 模板来自二进制字符串，不是现网报文。
3. **Claude 完整 CLI 系统提示词**：抓到的 5,657 字是 `sdk-cli` 入口的精简版；完整版（含
   `Bias toward working...` 那段）只能从二进制偏移读到片段，未见完整拼装结果。
4. 两家的模型都不是官方模型（走的是本机网关），本文结论只依赖客户端组装部分
   （`tools` / `instructions` / `system`），与模型无关。

## 8. 复现命令（全部可离线重跑）

```bash
# 0. 起抓包代理（127.0.0.1:8787，body 落到 E:/tmp/rea6/capture/）
python E:/tmp/rea6/capture_proxy.py

# 1. Claude：非交互入口的工具表（23 个，无 AskUserQuestion）
E:/env/bin/claude.cmd -p "只回复 ok" < /dev/null \
  --settings '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:8787","ANTHROPIC_AUTH_TOKEN":"x"}}'
#    再看 plan 模式：同一命令加 --permission-mode plan

# 2. Codex：现网报文（8 个工具，含 request_user_input）
OPENAI_API_KEY=x E:/env/bin/codex.cmd exec --skip-git-repo-check -s read-only < /dev/null \
  -c model_provider=cap \
  -c 'model_providers.cap={name="cap",base_url="http://127.0.0.1:8787/v1",env_key="OPENAI_API_KEY",wire_api="responses"}' \
  "reply ok"

# 3. 从抓到的 body 里取工具表与指令
python - <<'PY'
import json,pathlib
b=json.loads(pathlib.Path("E:/tmp/rea6/capture/req-01-v1_responses.json").read_text(encoding="utf-8"))
print(len(b["tools"]), [t["name"] for t in b["tools"]], len(b["instructions"]))
PY

# 4. 静态复读（带行号，可对上本文引用）
python E:/tmp/rea6/probe.py E:/env/rea/out/codex.uniq.txt "request_user_input" 200
python E:/tmp/rea6/probe.py E:/env/rea/out/claude.uniq.txt "AskUserQuestion" 200

# 5. 按字节偏移读二进制原文（本文引用的两处 Claude 提示词）
python E:/tmp/rea6/binwin.py E:/env/npm-global/node_modules/@anthropic-ai/claude-code/bin/claude.exe 202089330 1180
python E:/tmp/rea6/binwin.py E:/env/npm-global/node_modules/@anthropic-ai/claude-code/bin/claude.exe 202065300 1500
```

> 抓包装置只写不审：它把 `Authorization` 头一起落盘。**这些文件里有网关 token，
> 不进仓库、不外传**（本文引用的都是 `tools` / `instructions` / `system` 三处，不含凭据）。
