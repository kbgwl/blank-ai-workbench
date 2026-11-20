# 流式工具调用参数被 token 上限截断：四套实现源码对照（2026-11-20）

> 调研方式：**读源码，不引用博客**。四套来源分别为本机明文 JS 提取物、本机编译产物 + 官方仓库源码、官方仓库源码。
> 约定：**【事实】** = 源码可核实；**【判断】** = 基于事实的工程推断；**未找到** = 明确没查到，不编造。
> 目标：回答 A（怎么处理和用哪个函数）、B（retryable 还是 fatal、界面文案）、C（有没有"太大就提示分块"、在哪一层）。

---

## 0. 来源与取证口径

| 项目 | 版本 | 源码位置 | 取证方式 |
|---|---|---|---|
| Claude Code | 2.1.271 | `E:/rea/claude/app.js`（59,012 行；从 `E:/env/npm-global/node_modules/@anthropic-ai/claude-code/bin/claude.exe` 尾部明文 JS 提取，`extract2.mjs` 从偏移 191,874,208 起导出） | 直接读函数体；该文件绝大多数行是数百 KB 的压缩长行，故正文同时给**字符偏移**与换算后的**行号** |
| Codex CLI | 0.154.0 | 本机 `@openai/codex` 为 Rust 编译产物（无源码）；源码取官方 `openai/codex@rust-v0.154.0`，下载到 `E:/rea/codex-src/*.rs` | 逐行读 Rust 源码；编译产物字符串 `E:/projects/codex-reverse/raw/utf8.txt` 交叉验证 |
| opencode | commit `d33b5783338b` | `anomalyco/opencode`（原 sst/opencode），源码下载到 `E:/rea/codex-src/oc_*.ts` | 逐行读 TS 源码 |
| DeepSeek Harness (DSH) | `dsh-v0.2.2-rc.5` | `deepseek-ai/deepseek-harness`，源码下载到 `E:/rea/codex-src/dsh_*.ts` | 逐行读 TS 源码 + 仓库内 `.agents/notes/` 设计记录（这是仓库源码的一部分，不是博客） |

---

## 1. Claude Code

### A. 截断后怎么处理（函数 + 关键代码）

Claude Code 有**四条独立路径**，按触发条件分工【事实】：

**(1) 到达 `max_tokens` 且有可见正文 → 注入"续写 + 拆小"提示，最多 3 次**

提示词常量（`app.js:2267` 附近，字符 ~1,232,600）：

```js
npt = "Output token limit hit. Resume directly — no apology, no recap of what you were doing. "+
      "Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces."
```

判定与循环（`app.js:8975 / 8986`，字符 ~8,136,217 / 8,203,544）：

```js
var OKe = 3;                                  // 恢复上限
function Har(e){ return e?.type==="assistant" && e.apiError==="max_output_tokens" }
function lPs(e,n,r){ return e?.type==="assistant" && e.isApiErrorMessage===!0 &&
  e.truncatedAfterOutput===!0 && (Oi(r)==="subagent" ||
  Oi(r)==="main" && n.options.isNonInteractiveSession) &&
  P("tengu_truncated_response_recovery",!0) }

if(Jo!==void 0 && (Har(Jo)||Jd)){
  let zu = Jd ? Po : Po+1;
  if(zu<=OKe){
    let nf = !Jd && cPs(Za, na([...uo, ...Za.filter(Zy=>!Zy.isApiErrorMessage)]));
    let Hd = nf ? void 0
      : ke({content:npt, isMeta:!0, turnCompanion:!0, now:$e.now, uuidFn:$e.uuid});
    if(Hd!==void 0) yield Hd;
    at = {messages: Hd===void 0 ? [...uo,...Za.filter(...)] : [...uo,...Za,Hd],
          resumeIncompleteThinking: nf || void 0,
          maxOutputTokensRecoveryCount: zu,
          transition:{reason:"max_output_tokens_recovery", attempt:zu}};
    continue
  }
  ...
  yield Jo   // 用尽后把错误消息呈现给用户
}
```

关键点【判断】：**重试时没有放大 max_tokens**——恢复分支里 `maxOutputTokensOverride:void 0`。它把同一份请求重发，但追加一条元消息让模型"从截断处接着写、并且把剩余工作拆得更小"。`CLAUDE_CODE_MAX_OUTPUT_TOKENS` 只是全局配置，不是自动放大的产物（`app.js:10648`）。

**(2) 纯思考块被切断（无正文）→ 服务端 resume 续接思考**

```js
function Par(e,n,r){                          // app.js:8975 附近
  if(!lCt(e) || !P("tengu_thinking_block_resumption",!1)) return !1;
  if(n!==!0) return g("query_thinking_block_resumption","not_resumable"),!1;
  if(r>0 && r+YRs>=ap(e,fp())) return g("...","skipped_context_limit"),!1;
  return !0
}
function cPs(e,n){                            // 判定能否续接
  let r=e.filter(h=>!h.isApiErrorMessage && !Dbe(h)), s=r.length===1?r[0]:void 0;
  if(s===void 0 || s.message.content.length!==1) return !1;
  let d=s.message.content[0];
  return d!==void 0 && Az(d) && GSe(d) &&
         s.message.stop_reason==="max_tokens" &&
         !Lbe(s,$n(n)) && Par(n, s.message.resumable, K6(s.message.usage))
}
```

`cPs` 命中时设置 `resumeIncompleteThinking`，走 Anthropic 服务端的思考块续接；未命中时退回 (1) 的 `npt` 提示。需 `message.resumable===true` 且开关 `tengu_thinking_block_resumption` 打开【事实】。

**(3) 正文流被切断（有正文）→ "从中途续写"提示，最多 3 次**

```js
rpt = "Your response above was cut off mid-stream. Resume directly from where it stops — "+
      "no apology, no recap. If none of it survived, answer the request from the start."
opt = "Your response above was cut off mid-stream and only your next message is delivered. "+
      "Write the complete response again from the start — no apology, no mention of the cut-off."
```
`Lf!==void 0 && Jo===Lf` 分支（`app.js:8986`）：`Po<OKe` 时注入 `rpt`（主会话）/`opt`（子代理）后 `continue`；用尽则 `yield Jo`。

**(4) 工具调用参数本身坏掉/被切 → 整轮重试一次，再失败就致命**

这是与本项目 bug 最直接相关的一条【事实】（`app.js:8986`，字符 ~8,205,500）：

```js
if((Jo?.message.stop_reason??tg)==="tool_use" && yi.length===0 && !Jo?.isApiErrorMessage){
  let zu = at.transition?.reason!=="malformed_tool_use_retry";
  i("tengu_malformed_tool_use_response",{will_retry:zu, model:St(di), text_has_leaked_invoke:Mar(Za)});
  if(zu){
    for(let Cy of Za) yield {type:"tombstone", message:Cy};
    let Hd = ke({content:Zdt, isMeta:!0, turnCompanion:!0, now:$e.now, uuidFn:$e.uuid});
    yield Hd;
    at = {messages:[...uo,Hd], transition:{reason:"malformed_tool_use_retry"}, ...};
    continue
  }
  let nf = No({content:"The model's tool call could not be parsed (retry also failed).", ...});
  return yield nf, mot(nf,Zn), nD(Zn,D,nf), {reason:"malformed_tool_use_exhausted"}
}
```

配套提示词（`app.js:2267`）：

```js
Zdt = "The previous response failed to produce a valid tool call. Please retry the tool call now."
yVn = "Your tool call was malformed and could not be parsed. Please retry."
```

关键判据：`stop_reason==="tool_use"` **但解析出的工具调用数为 0**（`yi.length===0`）。截断的 arguments 正是"模型声称要调工具、却解析不出完整调用"的典型成因【判断】。

**流式解析层本身在做什么**（与 kbgai 的 `openai_compat.py` 对应）：`input_json_delta` 的 `partial_json` 被逐片累积到 `tool_use.input`（`app.js:9256`，字符 ~9,077,798）：

```js
case "input_json_delta":
  if(Ac.type!=="tool_use" && Ac.type!=="server_tool_use") throw ...
  Ac.input += Cm.partial_json;
```
累积阶段**不校验 JSON**，校验发生在工具执行层（见 C）。

### B. retryable 还是 fatal？界面文案

- (1) max_tokens：**可恢复 3 次**；第 4 次仍超 → 呈现给用户：`"Claude's response exceeded the ${$T} output token maximum. To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."`（`app.js:10648`，字符 ~9,085,008，事件 `tengu_max_tokens_reached`，`apiError:"max_output_tokens"`）【事实】。
- (3) 正文截断：可恢复 3 次，用尽后呈现原始错误【事实】。
- (4) 工具调用坏掉：**只重试 1 次**；再失败 fatal，文案 `"The model's tool call could not be parsed (retry also failed)."`【事实】。
- 注意 (4) 的重试是**重置计数**的：`maxOutputTokensRecoveryCount:0`，不受 (1) 的 3 次预算约束【事实】。

### C. "太大就提示分块"机制在哪一层

**有，且在提示词层**（不是工具层、也不是流式解析层）【事实】：
- (1) 的 `npt` 明确写了 `Break remaining work into smaller pieces.`；这是 max_tokens 命中时注入的元消息。
- 触发条件是"输出 token 上限"，**不是**"检测到单次 write_file 内容过大"。

**工具层另有两种相关但不同的保护**【事实】：
- 工具输入 JSON 解析失败时不静默丢弃、也不立刻致命，而是把错误作为 `is_error` 的 tool_result 回给模型（字符 ~8,388,200，`app.js:9256`）：
  ```
  ${name} was called with input that could not be parsed as JSON.
  You sent (first N of M bytes): ...
  Common causes: unescaped backslashes in file paths (use / or \\), unescaped control characters, or truncated output. Retry with valid JSON.
  ```
  返回体：`tool_result` + `<tool_use_error>InputValidationError: ...</tool_use_error>` + `is_error:!0`。
- `edit` 工具有字节上限，超限时报错并给替代动作：`"edit: ${t} is ${l.size} bytes, exceeds ${f}-byte limit. Use bash (sed/awk) to edit a large file."`（`app.js:18961`）【事实】。这是"检测单次写入/编辑过大 → 提示改用别的方式"，但指向 bash 而非 append/分块。

**未找到**：Claude Code 里"检测到 write_file 参数将超过输出预算 → 主动建议分块/append"的专门机制；也未找到 arguments 片段跨轮拼接续写（它选择整轮重试或丢弃坏调用）【事实：未找到】。

---

## 2. Codex CLI（0.154.0）

### A. 怎么处理

源码在官方 Rust 仓库；本机安装产物是编译好的 `codex.exe`（`E:/env/npm-global/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/...`），无明文源码，所以以下全部来自官方源码。

1. ** Responses API 把函数参数当原始字符串**，`codex-rs/protocol/src/models.rs:1050-1053`：
   ```rust
   // The Responses API returns the function call arguments as a *string* that contains
   // JSON, not as an already‑parsed object. We keep it as a raw string here and let
   // Session::handle_function_call parse it into a Value.
   arguments: String,
   ```
   流式组装由 `codex_response_stream` 负责，本仓库的 `ResponseItem::FunctionCall` 不带 `status/incomplete` 字段（`models.rs:1043-1059`）【事实】。

2. **`build_tool_call` 原样透传参数字符串**，`codex-rs/core/src/tools/router.rs:246-262`：
   ```rust
   ResponseItem::FunctionCall { name, arguments, call_id, .. } => {
     let tool_name = ToolName::new(namespace, name).with_default_namespace();
     Ok(Some(ToolCall { tool_name, call_id,
       payload: ToolPayload::Function { arguments },
       encrypted_function_args }))
   }
   ```
   即解析层**不判完整性**【事实】。

3. **真正的 JSON 解析在工具处理器**，`codex-rs/core/src/tools/handlers/mod.rs:85-91`：
   ```rust
   pub(crate) fn parse_arguments<T>(arguments: &str) -> Result<T, FunctionCallError>
   where T: for<'de> Deserialize<'de> {
     serde_json::from_str(arguments).map_err(|err| {
       FunctionCallError::RespondToModel(format!("failed to parse function arguments: {err}"))
     })
   }
   ```
   错误类型只有两种（`codex-rs/tools/src/function_call_error.rs:3-9`）：
   ```rust
   pub enum FunctionCallError {
       RespondToModel(String),  // 把错误回给模型，让它重试
       Fatal(String),
   }
   ```
   截断导致的不完整 JSON 落入 `RespondToModel` → **模型侧可重试**，不是致命错误【事实】。

4. **主采样循环只重试"传输层"错误**，`codex-rs/core/src/session/turn.rs:1440-1520`：
   ```rust
   Err(err) => match err.details() {
     CodexErrorDetails::ContextWindowExceeded => { ...; return Err(err) }
     CodexErrorDetails::UsageLimitReached(e) => { ...; return Err(err) }
     _ => err,
   },
   ...
   if !err.is_retryable() { return Err(err) }
   handle_retryable_response_stream_error(&mut retry_state, max_retries, err, ...).await?;
   ```
   `codex-rs/core/src/responses_retry.rs:44-129` 只处理连接断开/WS→HTTPS 回退，UI 文案 `"Reconnecting... {n}/{max}"` / `"Falling back from WebSockets to HTTPS transport."`【事实】。
   **没有**针对 output-token 截断的专门重试分支，**没有**放大 `max_tokens`，**没有**续写 arguments 拼接【事实：未找到】。

### B. retryable / fatal 与文案

- 参数 JSON 坏：`FunctionCallError::RespondToModel("failed to parse function arguments: {err}")` → 非 fatal，错误作为模型可见的工具结果回灌【事实】。
- 传输截断：按 retryable 处理，最多 `stream_max_retries()` 次（`turn.rs:1441`），UI 显示 "Reconnecting..."【事实】。
- 重试次数耗尽：`CodexErrorDetails::RetryLimit` → `CodexErrorInfo::ResponseTooManyFailedAttempts`（`codex-rs/protocol/src/error.rs:444`）【事实】。
- 未找到 Codex 专门的"输出 token 上限导致工具参数截断"的用户文案；`max_output_tokens` 在二进制里是工具参数（如 `write_stdin`）而非该场景错误码【事实：未找到】。

### C. 分块提示

**未找到**"单次写入过大 → 提示分块/append"的机制。参数解析只给一句通用的 `failed to parse function arguments`，不判断是不是因为太长、也不建议拆小【事实：未找到】。

---

## 3. opencode（anomalyco/opencode @ d33b578）

### A. 怎么处理（四层联动，最完整）

**(1) 工具参数累积与校验**，`packages/ai/src/protocols/utils/tool-stream.ts`：
- 流式累积原文：`append` 把每片 `text` 追加到 `tool.input`（第 186 行）；`inputDelta` 用 `partial-json` 的 `parse` 做实时预览（第 64-77 行）。
- 收尾时解析（第 203-218 行）：
  ```ts
  // Finalize one pending tool call: parse the accumulated raw JSON, remove it
  // from state, and reject malformed local arguments without repairing them.
  export const finish = (route, tools, key, input?) => Effect.gen(function*(){
    const tool = tools[key]; if(!tool) return {tools};
    return { tools: withoutTool(tools,key),
             events: finishEvents(tool, yield* toolCall(route, tool, input)) };
  })
  ```
  `toolCall`（第 79-98 行）：`parseToolInput(route, tool.name, raw)` 失败且非 provider-executed → `Effect.succeed(LLMEvent.toolInputError({...}))`，即**变成模型可见的工具错误结果**，不抛异常；provider-executed 才 fail【事实】。
  `finishAll`（第 225-238 行）用于 OpenAI Chat 这种没有逐工具 stop 事件的协议，在终态 `finish_reason` 时统一收尾【事实】。

**(2) 输出 token 上限 → 续写 + 拆小提示**，`packages/core/src/session/runner/step.ts:328-329`：
```ts
const needsContinuation = input.prepared.request.toolChoice?.type !== "none" && record.needsContinuation
if (record.finish?.finish === "length") return Outcome.OutputLimit({ needsContinuation })
```
`packages/core/src/session/runner/llm.ts:39` 定义提示：
```ts
const CONTINUE_AFTER_OUTPUT_LIMIT =
  "Your last response hit the output token limit (stop reason: length). Do not apologize, recap, or repeat yourself. Break the remaining work into smaller pieces."
```
同文件 `OutputLimit` 处理器（第 346-378 行）查最近 3 条消息：
```ts
const exhausted = recent.length===3 &&
  recent.every(row => row.type==="assistant" && row.finish==="length")
if (exhausted) return yield* new StepFailedError({
  error:{ type:"output-limit",
          message:"Response still truncated after two output token limit continuations" } })
if (outcome.needsContinuation) return true
yield* bus.publish(SessionEvent.Synthetic, { sessionID, text: CONTINUE_AFTER_OUTPUT_LIMIT })
assistantMessageID = SessionMessage.ID.create()
```
即**最多 2 次续写**，第 3 次仍 length 才失败【事实】。

**(3) "工具调用被 length 截断"的专门识别**，`packages/ai/src/llm.ts:303-312`（`decodeObject`，针对强制 schema 工具）：
```ts
// A cut-off call arrives with truncated or empty input, so its decode error would hide the real cause.
if (response.finishReason.normalized === "length")
  return Effect.fail(invalid("output hit the token limit before the forced tool call completed"))
```
也就是说：opencode **明确知道** "length + 空/半截工具参数" 的真实原因是 token 上限，不让 JSON 解码错误掩盖它【事实】。

**(4) 通用重试层**，`packages/core/src/session/runner/retry.ts`：`SessionRunnerRetry.decide` 用 `ModelRetry.policy` 决定 retry/delay，并发 `SessionEvent.RetryScheduled`；`step.ts:239-258` 把它接到 llmFailure【事实】。

### B. retryable / fatal 与文案

- 工具参数非法 JSON：`toolInputError` 事件，模型可重试（非 fatal）【事实】。
- length 截断：`OutputLimit` 续写 **2 次**；第 3 次 fatal，用户看到 `"Response still truncated after two output token limit continuations"`（`type:"output-limit"`）【事实】。
- 未知 finish reason：构造 `InvalidProviderOutputError`，classification `"incomplete-stream"`（`step.ts:212-223`），可被当作中断流重试（`step.ts:336-338`）【事实】。

### C. 分块提示

**有，提示词层**【事实】：`CONTINUE_AFTER_OUTPUT_LIMIT` 里 `Break the remaining work into smaller pieces.`，由 runner 在 length 时以 `SessionEvent.Synthetic` 注入。
**未找到**"检测单次 write 过大 → append"的专门分支；工具执行层只做通用 JSON 校验【事实：未找到】。

---

## 4. DeepSeek Harness（dsh-v0.2.2-rc.5）

### A. 怎么处理

**(1) 明确区分"参数坏掉"和"被 length 截断"**，`packages/llm/llm-pi-ai/src/stream.ts:150-151, 203-231`：
```ts
// ... throws LlmError (STREAM_CLOSED) if the source ends without a terminal event, or
// MALFORMED_RESPONSE when completed tool arguments are invalid. Length-limited
// output retains its terminal reason so the assembler can discard incomplete calls.
...
case 'toolcall_end':
  if (event.toolCall.argumentsError !== undefined) {
    argumentsError ??= `Tool call "${event.toolCall.name}": ${event.toolCall.argumentsError}`
    break                       // 丢弃这个不完整的工具调用，不发 block-end
  }
  yield { type:'block-end', block:{ type:'tool-call', ...,
          arguments: JSON.stringify(event.toolCall.arguments) } }
...
case 'done':
  yield { type:'usage', ... }
  if (argumentsError !== undefined && event.message.stopReason !== 'length') {
    throw new LlmError(argumentsError, 'MALFORMED_RESPONSE')
  }
  yield { type:'finish', reason: mapStopReason(event.message, contextWindow), ... }
```
即【事实】：
- 参数解析失败 **且不是 length** → `MALFORMED_RESPONSE`（致命）。
- 参数解析失败 **且是 length** → 不抛错，保留 `length` 终态，让 assembler 丢弃不完整调用。

**(2) stop reason 映射**，同文件 `mapStopReason`（第 102-135 行）：
```ts
case 'length': return { kind: 'max-tokens' }
case 'toolUse': return { kind: 'tool-calls' }
```
**(3) agent loop 收尾**，`packages/core/agent-loop/src/agent.ts:590`：
```ts
if (finish.kind === 'max-tokens') return { kind: 'max-tokens' }
const toolCalls = message.content.filter(b => b.type === 'tool-call')
if (toolCalls.length === 0) return { kind: 'completed' }
```
即 max-tokens 被当作**独立的 turn 结束原因**（不是 error），被丢弃的半截工具调用自然不会执行；没有自动整轮重试、没有放大 max_tokens、没有续写 arguments 拼接【事实】。

**(4) 传输截断分类**，同文件 `classifyPiAiError`（第 44-72 行）：
```ts
// A stream truncated before the provider's terminal event ...
// The connection dropped mid-response, so this is a transport truncation, not a model-level error.
if (/stream ended (?:before|without)\b/i.test(message)) return 'TRANSPORT'
if (/\b413\b/... ) return 'INVALID_REQUEST'
```
即"连接中断"与"输出超限"是两类，前者可重试、后者是 turn end【事实】。

### B. retryable / fatal 与文案

- `MALFORMED_RESPONSE`：非 `length` 的参数错误，致命【事实】。
- `max-tokens`：非 error，是 turn end；不自动重试【事实】。
- UI 文案：仓库设计记录 `.agents/notes/archived/bug-fix/2026-08-12-max-tokens-turn-end-notice.md` 明确：
  > "a warning StateDot, a localized title, and guidance that **the truncated output is preserved and sending 'continue' resumes in a new turn**. It shows no token numbers: the event carries none, and the notice must not fabricate budget data the provider did not report."

  该节点由 `packages/client/ui-chat/src/client/conversation-nodes/turn-max-tokens.ts` 实现：匹配 `turn/end` 且 `reason.kind === 'max-tokens'`，锚定在关闭的 Assistant 与 turn-tail 之间；**没有**继续/重试按钮（记录里写"deferred"）【事实】。
- 未找到长度截断场景的自动"拆小"提示词【事实：未找到】。

### C. 分块提示

**未找到**"单次写入过大 → 提示分块/append"机制。DSH 的做法是：保留已生成内容 + 显示 max-tokens 提示 + 让用户自己发 "continue"（新 turn）【事实】。

---

## 5. 横向对照表

| 问题 | Claude Code | Codex CLI | opencode | DSH |
|---|---|---|---|---|
| 流式层见到半截 JSON 怎么办 | 只累积 `partial_json`，执行层校验 | 只透传 `arguments: String`，执行层校验 | 累积 raw，finish 时 `parseToolInput`，失败→`toolInputError` | 累积 `argumentsDelta`，`toolcall_end` 出错则丢弃该调用 |
| length + 半截工具参数 | `tool_use` 且 0 调用 → 注入 `Zdt` 重试 1 次；或 `npt`/`rpt` 续写 | 未找到专门分支；落入通用 `failed to parse function arguments` | 明确识别（`decodeObject`：`output hit the token limit before the forced tool call completed`）；`length` → `OutputLimit` 续写 2 次 | 明确区分：`length` → `max-tokens` turn end，丢弃不完整调用，不抛 `MALFORMED_RESPONSE` |
| 自动放大 max_tokens | **否**（全部分支 `maxOutputTokensOverride:void 0`） | 未找到 | 未找到 | 未找到 |
| 续写 arguments 片段拼接 | 否（整轮重试/丢弃） | 否 | 否（不修复 malformed args） | 否（丢弃不完整调用） |
| retryable / fatal | 1 次重试后续仍失败 = fatal；max_tokens 3 次；`apiError:"max_output_tokens"` | `RespondToModel` = 模型可重试；传输按 retry limit | 2 次续写后续仍失败 = StepFailedError | 非 length = `MALFORMED_RESPONSE` 致命；length = turn end |
| 用户文案 | `Claude's response exceeded the N output token maximum...` / `The model's tool call could not be parsed (retry also failed).` | `Reconnecting... n/max` / `ResponseTooManyFailedAttempts` | `Response still truncated after two output token limit continuations` | max-tokens 行：截断内容保留，发 "continue" 新 turn 续；无重试按钮 |
| "太大→分块"在哪层 | **提示词层**（`npt`：`Break remaining work into smaller pieces.`） | 未找到 | **提示词层**（`CONTINUE_AFTER_OUTPUT_LIMIT`） | 未找到（只给 UI 提示让用户 continue） |

四家**没有一家**做 "arguments 片段拼接"（选项 c）；主流是 (a) 有限次整轮续写/重试 + 提示拆小，或 (d) 明确终止并告知用户；Codex 额外是工具层把坏参数回灌给模型重试【事实】。

---

## 6. 对 kbgai 的可落地建议（逐条对应基线 1/2/3）

> 以下均为【判断】。原则：不要自研 arguments 拼接（四家都没做，风险高），而要**把截断从"静默 fatal → HTTP 500"改成"有界续写 + 明确文案"**，并在工具层前置拦截超大写入。

### 对应基线 1：`openai_compat.py:277-281 / 303-308`、`openai_responses.py:344-346 / 367-372`

**改哪个函数**：两处 `raise ModelError("model_stream_truncated", ...)` 的调用点；以及 `ModelError` 定义（补充结构化字段）。

**怎么改**：
1. 在 `ModelError` 上附带结构化上下文，而不只是拼好的中文 `reason`：`tool_name`、`partial_args_chars`、`finish_reason`、`call_id`。这样 graph 层可判定而不是靠字符串匹配。（目前 `reason` 是 `"流在工具调用 write_file 的参数中途结束：10706 字不是完整 JSON"`，上游拿不到机器可读字段。）
2. **不要**在这里尝试把半截 JSON 拼成完整参数后派发——与四家一致，截断调用必须视为"未完成"，保持现有"不许派发、不许写回上下文"的语义。
3. 把 `retryable=True` 的语义收窄：它现在会被 `_transient_may_retry`（`graph.py:2328`）当成"原样重发"，这正是基线 3 的病根。建议新增独立 code 语义（例如保留 `model_stream_truncated` 但从通用 transient 集合里排除），让它的恢复只走下面第 2 条的专门分支。

### 对应基线 2：`graph.py:1833-1896` 的 while 循环（尤其最后的裸 `raise`）

**改哪个函数**：给 `while True` 的 `except ModelError` 块新增一个 `model_stream_truncated` 分支，位置放在 `context_overflow` 分支之后、最后 `raise` 之前；并新增两个辅助函数（仿 `_continuation_wanted` / `_with_continuation` 的写法）和一组常量。

**怎么改**（直接对齐 opencode + Claude Code，二者做法一致）：
```python
MAX_TRUNCATION_CONTINUATIONS = 2   # 抄 opencode 的"two continuations"

TRUNCATED_TOOL_PROMPT = (
    "Your last response hit the output token limit while writing tool arguments. "
    "Resume directly — no apology, no recap. Break the remaining work into "
    "smaller pieces (e.g. write the file in several smaller edits/appends)."
)

def _truncation_continuation_wanted(error: ModelError) -> bool:
    return (error.code == "model_stream_truncated"
            and str(error.finish_reason or "").lower() in {"length", "max_tokens", ""})

def _with_truncation_continuation(chat, error) -> list[ChatMessage]:
    # 关键：不要带上那条被切的 assistant 消息（里面是半截 arguments），
    # 只带一条说明现状的 user 消息；仿 Claude Code 的 npt 与 opencode 的 Synthetic 事件。
    return [*chat, ChatMessage(role="user", content=TRUNCATED_TOOL_PROMPT)]
```
循环内分支要点：
- 命中且 `truncation_recoveries < MAX_TRUNCATION_CONTINUATIONS`：计数+1，`chat = _with_truncation_continuation(...)`，记一条 `model.truncation_continuation` 事件（字段照抄现有 `model.continuation`），`continue`。
- 用尽：不要裸 `raise` 冒成 HTTP 500。改成 `raise ModelError("model_truncated_exhausted", f"模型输出被 {ceiling} token 上限截断，已续写 2 次仍写不完 {tool_name}；请把文件拆成多次小写入或换更大输出上限的模型")`——仿 opencode 的 `StepFailedError` 文案和 DSH 的 max-tokens 行（明确告诉用户"内容已保留/被丢弃"），让顶层映射成 4xx/可读错误而不是 500。
- **不要复用** `_continuation_wanted`（`graph.py:2459`）：它第 2464 行注释明确写了"有工具调用：参数被切是另一条路（model_stream_truncated），接一句「继续」只会让模型重写一遍调用"——正文续写的 `RESUME_PROMPT`（`graph.py:2405`）不适用于工具参数，必须新写一条带"拆小"指令的提示。
- 可选（更稳）：在续写前调用现成的 `_grow_budget`（`graph.py:1389`，上限 `output_ceiling`，见 `services/api/src/blank_api/services/models/budget.py:57`），把 `finish_reason=length` 的证据也纳入 `_output_budget_exhausted`（`graph.py:2421` 目前只认 `model_stream_empty`）。这与 Claude Code 不同（它不放大），但能治"模型确实想写 10706 字"的根因；注意仍受 `MAX_BUDGET_RECOVERIES=2`（`graph.py:2392`）约束，放大到顶后要走上面的明确文案而不是 500。

### 对应基线 3：`_transient_may_retry`（`graph.py:2328`）与预算上限（`graph.py:2392/2397`、`budget.py:57`）

**改哪个函数**：`_transient_may_retry` 本身，以及预算判定 `_output_budget_exhausted`（`graph.py:2421`）。

**怎么改**：
1. 在 `_transient_may_retry` 的"是否可重试"判据里**显式排除** `code == "model_stream_truncated"`（连同现有 429/限流/5xx 判断并列）。理由：它现在因 `retryable=True` 原样重发同一句，10706 字还是会被切——这正是用户实测的现象。把它交给基线 2 的专门分支：
   - 要么追加"拆小"提示（改问法），
   - 要么先 `_grow_budget`（改预算），
   - 二者都改变请求，才可能跳出循环。
2. 复用而非新增上限：用 `MAX_BUDGET_RECOVERIES=2` 管"放大预算"次数，新增的 `MAX_TRUNCATION_CONTINUATIONS=2` 管"拆小续写"次数；两者都耗尽时按基线 2 的明确文案收尾。`MAX_CONTINUATIONS=3`（`graph.py:2397`）保持只服务正文续写，不要混用，否则工具调用场景会被错误的 `RESUME_PROMPT` 带偏。
3. 在工具执行层加一道**前置拦截**（当前完全缺失，四家里 Claude Code 的 edit 字节上限最接近）：在 `write_file` 处理器里，若 `content` 长度超过阈值（例如接近 `output_ceiling` 对应字符数），直接返回带 `is_error` 的 tool_result：
   ```
   "content too large for one tool call (N chars); split into multiple smaller writes/appends"
   ```
   仿 Claude Code 的 `...Use bash (sed/awk) to edit a large file.` 与 opencode 的 `toolInputError`：让模型下一次就分块，而不是先烧掉一整轮 max_tokens 再被发现。这是唯一能在"产生截断之前"止血的一层。

**一句话总结**：基线 1 负责把截断变成机器可读信号；基线 2 负责把 HTTP 500 变成"有界续写/放大预算 + 明确文案"；基线 3 负责堵住"原样重发"这条无效路径，并在工具层前置拦截超大写入。三处改完，10706 字场景会走"拆小提示（或放大预算）→ 成功"或"2 次后给出可读失败"，而不再是 HTTP 500。

---

## 7. 未找到清单（避免误判）

- Claude Code / Codex / opencode / DSH 中**均未找到**"把半截 arguments 在下一轮续写拼接成完整 JSON"的实现（选项 c）。
- Codex、DSH **未找到**"输出 token 上限导致工具参数截断"的专门用户文案；Codex 只有通用 `failed to parse function arguments`，DSH 只有通用 max-tokens turn 行。
- Claude Code、opencode、DSH **未找到**"检测单次 write 内容过大 → 提示 append"的专门分支（只有通用的"拆小"提示或 edit 字节上限）。
- DSH **未找到**自动续写/自动重试 max-tokens turn 的机制（需用户手动发 "continue"）。
