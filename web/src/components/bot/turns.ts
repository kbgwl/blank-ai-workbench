/**
 * 把一条任务的事件流折叠成「一轮对话里助手做过什么」。
 *
 * 后端的事件是给审计用的：一条工具调用会拆成 requested / completed 两条，
 * 中间还夹着 context.measured、guardian.reviewed、node.started 这类内部观测。
 * 直接铺到界面上，用户看到的是几十行机器话，看不出"它到底改了什么"。
 *
 * 所以这里做三件事，而且只做这三件：
 * 1. 只保留人看得懂的动作（计划、工具、文件变更、提问、审批）；
 * 2. 把同一次工具调用的开始与结束配成一条——配对按顺序 + 同名，
 *    因为 requested 里没有 tool_call_id，只有 completed 才有；
 * 3. 配不上的 completed 仍然保留。宁可多显示一条"执行完了"，也不能因为
 *    配对失败就把一次真实发生的操作从视野里抹掉。
 *
 * 纯函数，不 import React 也不 import 别名路径——这样 node --test 能直接跑。
 */

export type RawEvent = {
  seq: number;
  type: string;
  node?: string;
  payload: Record<string, unknown>;
};

export type ToolStep = {
  kind: "tool";
  key: string;
  name: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "failed";
  durationMs: number | null;
  output: string;
  risk: string;
};

export type BotStep =
  | { kind: "plan"; key: string; items: string[] }
  | { kind: "change"; key: string; path: string; changeType: string }
  | { kind: "note"; key: string; text: string; tone: "muted" | "warn" | "danger"; repeat?: number }
  | { kind: "question"; key: string; text: string; answered: string }
  | { kind: "approval"; key: string; text: string; granted: boolean | null }
  | ToolStep;

/** 只做展示，判断不了真假的观测类事件。留在这里是为了让"为什么这条不显示"有据可查。 */
const INTERNAL = /^(node\.|context\.|guardian\.|memory\.|shell\.|snapshot\.|model\.|intent\.|task\.(created|started|resumed)|delivery\.|validation\.)/;

function str(source: unknown, ...keys: string[]): string {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return "";
}

function num(source: unknown, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && !Number.isNaN(Number(value))) return Number(value);
  }
  return null;
}

function record(source: unknown, ...keys: string[]): Record<string, unknown> {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  }
  return {};
}

function list(source: unknown, ...keys: string[]): unknown[] {
  for (const key of keys) {
    const value = (source as Record<string, unknown> | null)?.[key];
    if (Array.isArray(value)) return value;
  }
  return [];
}

export function toolNameOf(event: RawEvent): string {
  return str(event.payload, "tool", "name", "tool_name", "action") || event.node || "tool";
}

function toolOutputOf(payload: Record<string, unknown>): string {
  const text = str(payload, "summary", "output", "result", "message", "detail", "stdout", "stderr");
  if (text) return text;
  const nested = record(payload, "result", "output");
  return nested ? JSON.stringify(nested).slice(0, 4000) : "";
}

function planItems(payload: Record<string, unknown>): string[] {
  const direct = list(payload, "plan", "steps")
    .map((item) => (typeof item === "string" ? item : str(item, "step", "title", "text")))
    .filter(Boolean);
  if (direct.length > 0) return direct;
  // planner 有时把整段 JSON 塞在 text 里，取不出来就当作没有计划——不要显示一段生 JSON。
  const raw = str(payload, "text");
  if (!raw.trim().startsWith("{")) return [];
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return list(parsed, "steps", "plan")
      .map((item) => (typeof item === "string" ? item : str(item, "step", "title")))
      .filter(Boolean);
  } catch {
    return [];
  }
}

export type PlanItem = { text: string; status: string };
/** failed 单独数出来：`3 / 5` 看不出"剩下的两步里有一歩已经炸了"。 */
export type PlanView = { items: PlanItem[]; completed: number; failed: number; total: number };

/**
 * 这一轮的计划：初始计划 + 后续每次 `update_plan` 的修订。
 *
 * 计划不放进步骤流，因为它不是"过程"，而是"打算做什么 / 做到哪了"——那是用户在
 * 对话里最想先看到的东西。之前它被塞进默认折叠的过程明细里，等于没显示；
 * 而 `plan.updated` 更是完全没人消费，进度永远停在 0。
 */
export function planOf(events: readonly RawEvent[]): PlanView | null {
  let items: PlanItem[] = [];

  for (const event of events) {
    const type = event.type.toLowerCase();
    if (type === "plan.created") {
      const created = planItems(event.payload);
      if (created.length) items = created.map((text) => ({ text, status: "pending" }));
      continue;
    }
    if (type === "plan.updated") {
      const next = list(event.payload, "steps", "plan")
        .map((item) =>
          typeof item === "string"
            ? { text: item, status: "pending" }
            : { text: str(item, "step", "title", "text"), status: str(item, "status") || "pending" },
        )
        .filter((item) => item.text.trim());
      if (next.length) items = next;
    }
  }

  if (!items.length) return null;
  const completed = items.filter((item) => item.status === "completed").length;
  const failed = items.filter((item) => item.status === "failed").length;
  return { items, completed, failed, total: items.length };
}

/** 折叠后的步骤序列。顺序就是发生顺序——不要按 kind 重排，那会让读者以为计划在最后才出现。 */
export function buildSteps(events: readonly RawEvent[]): BotStep[] {
  const steps: BotStep[] = [];
  const running = new Map<string, number[]>();
  // 已排队但还没生效的插话：`steer.queued` 先占一个位置，`steer.applied` 到了
  // 再把那条改写成"已生效"。按下标而不是按文本配对，同一句话连说两遍也不会串。
  const pendingSteer: { index: number; text: string }[] = [];

  for (const event of events) {
    const type = event.type.toLowerCase();
    if (INTERNAL.test(type)) continue;

    // 计划由 `planOf` 单独提到正文上方渲染，这里不再往步骤流里塞一份：
    // 两处都画，用户会以为计划执行了两次。
    if (type === "plan.created" || type === "plan.updated") continue;

    // 打回的结果必须在这一轮里当场有个交代：用户刚在审批卡上选了「退回第 N 步」。
    // 计划卡只会在成功时跟着变，而"没生效"那一档连计划都不动——不写一行说明的话，
    // 用户看到的就是「我点了退回，什么都没发生」。两档都进步骤流，口径与任务页一致。
    if (type === "plan.rewound") {
      const index = num(event.payload, "index");
      const stepId = str(event.payload, "step_id");
      steps.push({
        kind: "note",
        key: `rewound-${event.seq}`,
        text:
          index !== null
            ? `已退回第 ${index + 1} 步，从这里重做`
            : `已退回某一步（${stepId || "未记 id"}），从这里重做`,
        tone: "muted",
      });
      continue;
    }
    if (type === "plan.rewind_missed") {
      const requested = str(event.payload, "step_id");
      const knownIds = Array.isArray(event.payload?.known_step_ids) ? event.payload.known_step_ids : [];
      steps.push({
        kind: "note",
        key: `rewind-missed-${event.seq}`,
        text: `打回没生效：请求退回 ${requested || "（空）"}，当前计划 ${knownIds.length} 步，对不上任何一步。这次只按普通驳回处理，计划没有重开`,
        tone: "warn",
      });
      continue;
    }

    if (type === "tool.requested" || type === "tool.started") {
      const name = toolNameOf(event);
      steps.push({
        kind: "tool",
        key: `tool-${event.seq}`,
        name,
        args: record(event.payload, "arguments", "args", "input", "params"),
        status: "running",
        durationMs: null,
        output: "",
        risk: str(event.payload, "risk_level", "risk"),
      });
      running.set(name, [...(running.get(name) ?? []), steps.length - 1]);
      continue;
    }

    if (type === "tool.completed" || type === "tool.failed" || type === "tool.result") {
      const name = toolNameOf(event);
      const queue = running.get(name);
      // 先配最早那条未结束的。这个顺序是有保证的：后端 `graph.py` 的收尾段
      // "全部按模型给出的顺序写事件，前端看到的次序与串行时一致"——即使只读工具
      // 是并发执行的，落盘顺序仍是调用顺序。所以同名调用按下标一一对应，
      // 不按结束先后配（那会把并发的两次读文件张冠李戴）。
      const index = queue?.length ? queue[0] : undefined;
      const ok = event.payload.ok === true || str(event.payload, "status").toLowerCase() === "ok";
      const failed = type === "tool.failed" || event.payload.ok === false;
      const patch = {
        status: (failed ? "failed" : ok ? "ok" : "ok") as ToolStep["status"],
        durationMs: num(event.payload, "duration_ms", "latency_ms", "elapsed_ms"),
        output: toolOutputOf(event.payload),
        risk: str(event.payload, "risk_level", "risk"),
      };
      if (index !== undefined && steps[index]?.kind === "tool") {
        queue!.shift();
        const existing = steps[index] as ToolStep;
        // requested 没带参数时，completed 里的也不一定有；两边都空就保持空，
        // 不要用一个空对象把"参数未知"伪装成"没有参数"。
        steps[index] = { ...existing, ...patch, args: Object.keys(existing.args).length ? existing.args : record(event.payload, "arguments", "args") };
      } else {
        steps.push({
          kind: "tool",
          key: `tool-${event.seq}`,
          name,
          args: record(event.payload, "arguments", "args", "input"),
          ...patch,
        });
      }
      continue;
    }

    if (type === "file.changed" || type === "file.created" || type === "file.deleted") {
      const path = str(event.payload, "path", "file", "file_path");
      if (!path) continue;
      steps.push({
        kind: "change",
        key: `change-${event.seq}`,
        path,
        changeType: str(event.payload, "change_type", "type") || (type === "file.created" ? "created" : type === "file.deleted" ? "deleted" : "modified"),
      });
      continue;
    }

    if (type === "question.asked" || type === "question.required") {
      const text = str(event.payload, "question", "text", "prompt");
      if (!text) continue;
      steps.push({ kind: "question", key: `q-${event.seq}`, text, answered: "" });
      continue;
    }

    if (type === "question.answered") {
      const answer = str(event.payload, "answer", "answers", "text");
      const pending = [...steps].reverse().find((step) => step.kind === "question" && !step.answered);
      if (pending?.kind === "question") pending.answered = answer || "已回答";
      continue;
    }

    if (type === "approval.decided") {
      const granted = event.payload.approved === true || str(event.payload, "decision").toLowerCase().startsWith("approve");
      steps.push({
        kind: "approval",
        key: `ap-${event.seq}`,
        text: str(event.payload, "message", "reason", "tool") || (granted ? "已批准一次操作" : "驳回过一次操作"),
        granted,
      });
      continue;
    }

    if (type === "steer.queued") {
      const text = str(event.payload, "message", "text");
      if (text) {
        // 先按"已排队"记一条，等 `steer.applied` 到了再把同一条改写成"已生效"。
        // 只显示排队不显示生效，用户没法判断"我说的话到底进没进上下文"——
        // 那他就只能再发一遍，于是同一句话在下一轮又生效一次。
        pendingSteer.push({ index: steps.length, text });
        steps.push({
          kind: "note",
          key: `note-${event.seq}`,
          text: `插话已排队，等它下一次想事情时生效：${text}`,
          tone: "muted",
        });
      }
      continue;
    }

    if (type === "steer.applied") {
      // 后端在真正注入上下文时写这条，`step` 是注入发生在第几步、`delay_ms` 是
      // 从用户敲下到生效等了多久。等待时长对长任务是关键信息：等 3 秒是正常节奏，
      // 等 4 分钟说明它正卡在一条命令里，用户据此才知道下次该不该等。
      const step = num(event.payload, "step");
      const delay = num(event.payload, "delay_ms");
      const where = step !== null ? `第 ${step} 步` : "下一步";
      const waited = delay !== null && delay >= 1500 ? `，等了 ${Math.round(delay / 1000)} 秒` : "";
      const label = `插话已生效（从${where}起${waited}）`;
      const pending = pendingSteer.shift();
      const slot = pending?.index ?? -1;
      const existing = slot >= 0 ? steps[slot] : undefined;
      if (pending && existing?.kind === "note") {
        steps[slot] = { ...existing, text: `${label}：${pending.text}` };
      } else {
        // 找不到对应的排队记录（事件被裁剪过、或这句来自更早的一轮）也不能丢：
        // 悄悄不显示等于让用户以为插话没生效。
        steps.push({ kind: "note", key: `note-${event.seq}`, text: label, tone: "muted" });
      }
      continue;
    }
  }

  return steps;
}

/**
 * 事件里的 reason 有时间是机器码（后端要按它做判断），直接端给用户就是一句
 * 英文标识符。实测页面上出现过一行红字就写着 `approval_expired`——用户读到的
 * 是一个变量名，不是"发生了什么、我该怎么办"。
 *
 * 只翻译确定会进界面的码；其余保持原样，但套一句人话的壳，别让标识符裸奔。
 */
const REASON_TEXT: Record<string, string> = {
  approval_expired: "审批卡超过有效期没人处理，这一轮已经结束。点下面「用同样的话再试一次」重跑一轮。",
};

export function reasonText(raw: string): string {
  const value = raw.trim();
  if (!value) return "";
  const known = REASON_TEXT[value.toLowerCase()];
  if (known) return known;
  // 长文本是后端抄回来的真实错误，原样显示才有排查价值；短的全小写标识符才是码。
  if (/^[a-z][a-z0-9_]{2,39}$/.test(value)) return `这一轮失败了（${value}）`;
  return value;
}

/**
 * 把回复里选中的一段做成引用块，塞进输入框。
 *
 * 为什么要做这件事：用户想追问某一句的时候，只能自己把那段话抄一遍，或者写
 * 「上面说的第二点」——后者在长回复里根本指不准，模型只能猜。选中就能引用，
 * 是把"我在说哪一句"变成确定的输入，而不是一次猜测。
 *
 * 三条约束：
 * 1. 按 markdown 引用（`>` 前缀）而不是引号包裹。引用块在模型那边是明确的结构，
 *    引号只是一对字符，长段落里哪句到哪句结束会糊掉。
 * 2. 超长截断到句边界。选了三屏文字，原样塞进去会把它自己的上下文挤满，
 *    用户真正想问的那句话反而被淹没。截断后标一个省略号，让它知道还有下文。
 * 3. 已经引过的不再引第二遍。连点两次「引用」应该是没反应，不是同一段话出现两次。
 */
export const QUOTE_LIMIT = 600;

/** 选中文本 → 引用块。空白/重复的返回空串，调用方据此不插入。 */
export function quoteBlock(text: string, limit = QUOTE_LIMIT): string {
  const raw = String(text ?? "").replace(/\r\n/g, "\n").trim();
  if (!raw) return "";

  let body = raw;
  if ([...body].length > limit) {
    const cut = [...body].slice(0, limit).join("");
    // 尽量断在句号/问号/换行后面：截在半句话上，模型会去补全那半句。
    const boundary = Math.max(
      cut.lastIndexOf("。"),
      cut.lastIndexOf("？"),
      cut.lastIndexOf("！"),
      cut.lastIndexOf("\n"),
      cut.lastIndexOf(". "),
    );
    body = boundary > limit * 0.5 ? [...cut].slice(0, boundary + 1).join("") : cut;
    body = body.trimEnd() + "……";
  }

  // 每行都加 `>`；空行写成单个 `>`，否则引用块在渲染时会被断成两段。
  const quoted = body
    .split("\n")
    .map((line) => (line.trim() ? `> ${line}` : ">"))
    .join("\n");
  return `${quoted}\n\n`;
}

/**
 * 新草稿 = 引用块 + 原有内容。
 *
 * 引用放最前面：它是这句话的上下文，问句跟在后面读起来才是"针对这段，我想问…"。
 * 反过来写（先问句后引用）在人读来是两段无关的话。
 */
export function quoteDraft(current: string, text: string, limit = QUOTE_LIMIT): string {
  const block = quoteBlock(text, limit);
  if (!block) return current;
  const rest = String(current ?? "");
  // 同一段话连点两次：第二次什么都不做。
  if (rest.includes(block.trim())) return current;
  return rest.trim() ? `${block}${rest}` : block;
}

/**
 * 这一轮助手最后说的话。取最后一条 task.completed 的 summary；失败时取原因。
 *
 * 除了给人看的那句 `reason`，还交出 `rawReason` 与 `code`：错误分层（error-copy.ts）
 * 需要**原始**字样才能认出成因——`reason` 已经被翻译成一句完整的建议，
 * 再拿去匹配关键词，"模型请求超时"里的"超时"是它自己写进去的，等于自证。
 *
 * `code` 来自后端 `_finish(extra=...)`，`ModelError` 一路带下来；旧数据没有这个字段。
 */
export function turnReply(events: readonly RawEvent[]): {
  text: string;
  status: string;
  reason: string;
  rawReason: string;
  code: string;
} {
  let text = "";
  let status = "";
  let reason = "";
  let code = "";
  for (const event of events) {
    const type = event.type.toLowerCase();
    if (type === "task.completed" || type === "task.finished") {
      text = str(event.payload, "summary", "result", "message") || text;
      status = str(event.payload, "status") || status;
    }
    if (type === "task.failed" || type === "task.cancelled" || type === "task.canceled") {
      status = type.replace("task.", "");
      reason = str(event.payload, "reason", "error", "message") || reason;
      code = str(event.payload, "code", "error_code") || code;
    }
  }
  return { text, status, reason: reasonText(reason), rawReason: reason, code };
}

/**
 * 相邻两条一模一样的备注合成一条，后面标次数。
 *
 * 连按两次回车曾真的会发两次插话（draft 是在 then 里才清空的），后端各记一条，
 * 对话里于是出现两段逐字相同的话——看起来像它在复读。发送侧已经挡住了重复提交，
 * 这里兜住历史数据和任何别的重复来源：同一句话只在屏幕上出现一次。
 *
 * 只合并相邻的：两句相同的话中间夹着一步工具调用，那是"又说了一遍"，不是重复。
 */
export function collapseRepeatedNotes(steps: readonly BotStep[]): BotStep[] {
  const out: BotStep[] = [];
  for (const step of steps) {
    const prev = out[out.length - 1];
    if (step.kind === "note" && prev?.kind === "note" && prev.text === step.text) {
      out[out.length - 1] = { ...prev, repeat: (prev.repeat ?? 1) + 1 };
      continue;
    }
    out.push(step);
  }
  return out;
}

/**
 * 一轮是否还在跑：有终态事件或任务状态是终态都算结束。
 *
 * `task.cancel_requested` 也算结束：它是"已经在取消"的确证，此时再把这句输入
 * 当插话发过去，后端只会回 409「任务已结束」。用户想说的是下一句，不是往
 * 一个已经停下的轮次里塞话。
 */
export function isTurnRunning(status: string, events: readonly RawEvent[]): boolean {
  const normalized = status.toLowerCase();
  if (["succeeded", "failed", "cancelled", "canceled", "error", "completed"].includes(normalized)) return false;
  return !events.some((event) => {
    const type = event.type.toLowerCase();
    return (
      type === "task.completed" ||
      type === "task.failed" ||
      type === "task.cancelled" ||
      type === "task.canceled" ||
      // paused 不在这里：驳回高风险操作后这一轮还能接着聊（后端 steer 也收它），
      // 当成结束会让"接着说一句"变成"另开一轮"。
      type === "task.cancel_requested"
    );
  });
}

/**
 * 只有新建的一轮才需要立刻 `/start`。
 *
 * Chat Supervisor（v165 §2）把「继续 / 暂停 / 取消 / 同意」作用在**已有**任务上，
 * 后端返回的是那条任务而不是新草稿。此时再调一次 `/start`，等于把刚暂停的任务
 * 重新拉起，或者对一条已经排队/在跑的任务撞 409——用户看到的就是「我点了暂停，
 * 它又自己跑起来了」。
 */
/**
 * 后端下发的 auto_start（v165 §5.1）：七条前置条件齐备才为 true。
 * 老任务 / 控制词返回的旧任务没有这个字段，返回 null 表示「按 status 自己判」。
 */
export function dispatchAutoStart(goalSpec: Record<string, unknown> | undefined): boolean | null {
  const dispatch = goalSpec?.dispatch;
  if (!dispatch || typeof dispatch !== "object") return null;
  const value = (dispatch as Record<string, unknown>).auto_start;
  return typeof value === "boolean" ? value : null;
}

/**
 * 只有新建的 draft 轮次才自动 /start。
 * 后端 Dispatcher 明确判了「手动模式 / 等待审批 / 等待补充」时，即使状态是 draft
 * 也不能替用户点运行——那是 §8 三种自动化模式的边界，不是界面能绕过的。
 */
export function shouldAutoRunTurn(status: string, autoStart?: boolean | null): boolean {
  if (autoStart === false) return false;
  return status.toLowerCase() === "draft";
}

/**
 * 输入框右边那个圆钮这一刻是干什么的。
 *
 * 只有一个按钮：闲着是发送，跑着是停止。「停止」和「发送」共用同一个位置，
 * 是因为它们本来就是同一个问题的两个答案——"现在要它继续，还是要它别继续"。
 * 拆成两个按钮之后，停止在对话区还有一个文字按钮，三处都能停，谁也不敢点。
 */
export function composerAction(running: boolean, canSend: boolean): "stop" | "send" | "disabled" {
  if (running) return "stop";
  return canSend ? "send" : "disabled";
}

/** 一眼就能看出"它在读还是在写"的工具名归类。与 steps.tsx 的图标归类同源，但语义不同：
 *  那边是为了挑图标，这边是为了统计"先看后改"的比例，别把两个正则合并到一起。 */
const LOOK_LIKE = /^(read|list|open|view|cat|search|grep|glob|find|query|inspect)/i;

export type TurnEvidence = {
  /** 工具调用总次数（含失败）。这是"花了多少动作"的直接度量。 */
  calls: number;
  /** 其中读/查类调用。为 0 而 files > 0，说明它是直接改的——没看就改。 */
  looked: number;
  /** 实际跑过的命令（按出现顺序去重，最多留 8 条给悬浮提示）。 */
  commands: string[];
  /** 去重后的命令总条数。commands 会被截断，所以计数单独存——否则跑了 20 条会显示成 8 条。 */
  commandCount: number;
  /** 改动过的文件数。 */
  files: number;
  /** 失败次数。步骤区留着失败记录，所以这里只做计数，不隐藏。 */
  failed: number;
  /** 工具耗时合计（毫秒）。没有一条带耗时则为 0。 */
  durationMs: number;
};

/**
 * 一轮的「依据」：用来替代正文里的过程复述。
 *
 * 正文为什么能变短，靠的不是让模型少写，而是**分工**：事实（改了哪个文件、
 * 跑了什么命令、输出什么、花了多久）全部由这里按事件流算出来，正文只留判断
 * （为什么这么改、哪条路被否了、还剩什么风险）。同一份事实说两遍不会让人更
 * 放心，只会让人多读一遍。
 *
 * 所以这里的每个数字都要能落到具体的行——它是"溯源"的入口，不是装饰。
 * 数错了比不显示更糟：用户会据此以为它没验证、或者以为它没读就改。
 */
export function turnEvidence(steps: readonly BotStep[]): TurnEvidence {
  let calls = 0;
  let looked = 0;
  let files = 0;
  let failed = 0;
  let durationMs = 0;
  const commands: string[] = [];
  const seenCommands = new Set<string>();
  const seenFiles = new Set<string>();

  for (const step of steps) {
    if (step.kind === "change") {
      // 同一个文件改三次算一个文件：用户关心的是"动了哪些文件"，不是写了几次。
      if (!seenFiles.has(step.path)) {
        seenFiles.add(step.path);
        files += 1;
      }
      continue;
    }
    if (step.kind !== "tool") continue;

    calls += 1;
    if (LOOK_LIKE.test(step.name)) looked += 1;
    if (step.status === "failed") failed += 1;
    if (step.durationMs !== null) durationMs += step.durationMs;

    const command = str(step.args, "command", "cmd");
    // 空命令不入表：run_tests 这类工具本来就没有 command 参数，硬塞一条空串
    // 会让"跑了 3 条命令"变成"跑了 3 条命令（其中 1 条是空的）"。
    if (command && !seenCommands.has(command)) {
      seenCommands.add(command);
      if (commands.length < 8) commands.push(command);
    }
  }

  return { calls, looked, commands, commandCount: seenCommands.size, files, failed, durationMs };
}

/** 步骤区的三种看法。默认全看，"改动"和"失败"是查到具体某一处时最快的两条路。 */
export type StepFilter = "all" | "changes" | "failures";

export function filterSteps(steps: readonly BotStep[], filter: StepFilter): BotStep[] {
  if (filter === "all") return [...steps];
  if (filter === "changes") return steps.filter((step) => step.kind === "change");
  return steps.filter((step) => step.kind === "tool" && step.status === "failed");
}

/**
 * 正文里提一句文件名，能不能精确落到本轮改过的某个文件上。
 *
 * 命中返回完整路径，否则返回空串。**有歧义时一律返回空**——两个同名文件都改过，
 * 点哪个都可能是错的；给一个点不动的灰字，好过给一个点错地方的链接。
 *
 * 按 basename 与尾部路径两档匹配，是因为人话里写的是 `composer.tsx` 或
 * `bot/composer.tsx`，几乎不会把 `web/src/components/bot/composer.tsx` 写全。
 * 只做这两档而不做"包含"匹配：包含匹配会让 `test.py` 命中 `contest.py`。
 */
export function resolveTouchedPath(text: string, files: readonly string[]): string {
  const needle = String(text ?? "").trim().replace(/\\/g, "/");
  if (!needle || needle.includes(" ")) return "";
  const pool = files.map((path) => String(path).replace(/\\/g, "/"));
  const exact = pool.find((path) => path === needle);
  if (exact) return exact;

  const tail = (path: string) => (path.endsWith(`/${needle}`) ? true : path === needle);
  const byTail = pool.filter(tail);
  if (byTail.length === 1) return byTail[0];
  if (byTail.length > 1) return "";

  const name = needle.split("/").pop() ?? needle;
  const byBase = pool.filter((path) => (path.split("/").pop() ?? path) === name);
  return byBase.length === 1 ? byBase[0] : "";
}

/** 一轮里改过哪些文件（用于对话里的"改了 N 个文件"摘要，去重保序）。 */
export function touchedFiles(steps: readonly BotStep[]): { path: string; changeType: string }[] {
  const seen = new Map<string, string>();
  for (const step of steps) {
    if (step.kind === "change" && !seen.has(step.path)) seen.set(step.path, step.changeType);
  }
  return [...seen].map(([path, changeType]) => ({ path, changeType }));
}

/**
 * 拿首条消息当会话标题：取第一行、去掉 markdown 前缀、按**字符**截断。
 *
 * 按字符而不是按字节：中文一个字三字节，按字节切会把最后一个字砍成半个乱码。
 * 去掉前缀是因为用户常先写 `# 修一下登录` 或 `- 帮我看看`，标题里不该出现这些符号。
 * 但只削真正的 markdown 前缀："2026 年那次重构"不能因为以数字开头就被削成"年那次重构"。
 */
export function threadTitleFrom(text: string, limit = 28): string {
  const first = String(text ?? "")
    .split("\n")
    .map((line) => line.replace(/^(?:#{1,6}\s*|[-*+]\s+|\d+[.)、]\s*|>\s*)/, "").trim())
    .find((line) => line.length > 0);
  if (!first) return "新对话";
  const chars = [...first];
  return chars.length <= limit ? first : `${chars.slice(0, limit).join("")}…`;
}

/**
 * 会话列表按「今天 / 昨天 / 本周 / 更早」分组。
 *
 * 用本地日历日切分，不是"距今 24 小时"：早上九点看昨天傍晚的会话，
 * 按小时算会归到"今天"，而用户心里那是昨天。
 * 时间戳解析不出来（空、坏值）时归到"更早"，不抛错——列表少一个分组标题，
 * 好过整块列表渲染不出来。
 */
export function dayBucket(value: string, now: number = Date.now()): "today" | "yesterday" | "week" | "earlier" {
  const parsed = Date.parse(String(value ?? "").includes("T") ? String(value) : String(value ?? "").replace(" ", "T"));
  if (!Number.isFinite(parsed)) return "earlier";
  const startOfDay = (ms: number) => {
    const date = new Date(ms);
    date.setHours(0, 0, 0, 0);
    return date.getTime();
  };
  const today = startOfDay(now);
  const day = startOfDay(parsed);
  if (day >= today) return "today";
  if (day >= today - 86_400_000) return "yesterday";
  if (day >= today - 7 * 86_400_000) return "week";
  return "earlier";
}


/**
 * 把会话标题末尾的时间拆出来。
 *
 * 自动生成的会话标题形如「雷罡 21:51:06」——一整列 18 行都是这个形状，时间
 * 和标题同字号同颜色，读起来是一堵墙。拆开之后时间靠右排成等宽小字：
 * 眼睛可以竖着扫时间、横着读标题，两条信息各自成列。
 *
 * 拆不出来就原样返回（标题里本来就没有时间是很常见的情况），不改写标题。
 */
export function splitTitleStamp(title: string): { base: string; stamp: string } {
  const raw = String(title ?? "");
  const match = /^(.+?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$/.exec(raw.trim());
  if (!match) return { base: raw, stamp: "" };
  return { base: match[1], stamp: match[2] };
}

/**
 * 「在想…」那句话用的动词。
 *
 * Claude Code 终端有 188 个动词，我们不需要那么多：前 6 分钟换个词就够让
 * 人感觉到"它在动"，再长用户也不看词，只看计时。所以这里真正能止住疑虑的
 * 不是词，是旁边那个一直在走的秒数。
 */
const THINK_VERBS = ["在想", "在核对", "在翻代码", "在理顺", "在拆解", "在比对"];

/**
 * 由开始时间挑一个词：同一轮无论重渲染多少次都是同一个词。
 * 用随机数会让它在打字时不停跳，反而更像卡住了。
 */
export function thinkVerb(startedAtMs: number | null): string {
  if (startedAtMs === null || !Number.isFinite(startedAtMs)) return THINK_VERBS[0];
  return THINK_VERBS[Math.floor(startedAtMs / 1000) % THINK_VERBS.length];
}
