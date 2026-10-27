import {
  arrayOf,
  boolOf,
  eventToolArgs,
  eventToolName,
  eventToolOutput,
  guardianFromPayload,
  numOf,
  normalizeQuestions,
  normalizeStatus,
  pick,
  recordOf,
  textOf,
  type AgentEvent,
  type ApprovalGrant,
  type ApprovalScope,
  type GuardianVerdictInfo,
  type PendingQuestion,
} from "./api";
import { clip } from "./format";
import {
  type BoundaryFacts,
  type CompactionReplacement,
  type CompactionWindow,
  type HistoryRef,
} from "./status";
import { verdictOf } from "./verdict";

export type TermLine = { text: string; tone: "cmd" | "out" | "err" | "warn" };

export type Block =
  | {
      kind: "plan";
      key: string;
      seq: number;
      node: string;
      at: string;
      items: string[];
      text: string;
    }
  | {
      kind: "tool";
      key: string;
      seq: number;
      node: string;
      at: string;
      name: string;
      args: Record<string, unknown>;
      output: string;
      status: string;
      durationMs: number | null;
      risk: string;
      toolCallId: string;
      boundary: string;
      /** 后端探测出来的执行事实（强度、限额是否生效）。缺键 = 老任务，渲染时不许编。 */
      boundaryFacts?: BoundaryFacts;
    }
  | {
      kind: "terminal";
      key: string;
      seq: number;
      at: string;
      title: string;
      lines: TermLine[];
    }
  | {
      kind: "question";
      key: string;
      seq: number;
      at: string;
      questions: PendingQuestion[];
      state: "pending" | "answered";
      answers: string;
    }
  | {
      kind: "approval";
      key: string;
      seq: number;
      at: string;
      tool: string;
      toolCallId: string;
      args: Record<string, unknown>;
      risk: string;
      message: string;
      /** 命令前缀签名（"git push"）：不为空才能提供「本会话总是允许该前缀」。 */
      prefix: string;
      scopes: ApprovalGrant[];
      state: "pending" | "granted" | "denied";
      /** 独立审查的意见。undefined=这张卡早于审查进载荷，null=审查关着，对象=审查员说了话。 */
      guardian?: GuardianVerdictInfo | null;
    }
  | {
      kind: "result";
      key: string;
      seq: number;
      at: string;
      status: "succeeded" | "failed" | "cancelled" | "waiting";
      title: string;
      summary: string;
      reason: string;
    }
  | {
      kind: "change";
      key: string;
      seq: number;
      at: string;
      path: string;
      changeType: string;
      diff: string;
    }
  | {
      kind: "boundary";
      key: string;
      seq: number;
      node: string;
      at: string;
      /** 折进摘要的历史条数；0 表示这次只是水位推进（老事件没这个字段时也是 0）。 */
      folded: number;
      /** 累计已折叠到第几条历史消息——这就是"边界"本身：边界之前不再是原文。 */
      covered: number;
      tokensBefore: number;
      tokensAfter: number;
      tokensSaved: number;
      waterRatio: number;
      window: number;
      triggeredBy: string;
      microClipped: number;
      microCharsSaved: number;
      /**
       * 窗口链与替换记录（#42，对标 §4.1 的 compacted）。
       * 老任务的压缩事件没有这两组键，字段里以 null / 空数组如实表达"没说"。
       * `window` 已经被上面占走表示 context_window，所以这里叫 windowChain，不能混。
       */
      windowChain: CompactionWindow;
      replacement: CompactionReplacement;
    }
  | {
      kind: "system";
      key: string;
      seq: number;
      at: string;
      title: string;
      detail: string;
      tone: "muted" | "warn" | "danger" | "accent";
    };

const START_WORDS = ["started", "requested", "call", "invoked", "pending", "begin"];
const DONE_WORDS = ["completed", "result", "finished", "done", "returned", "ended"];

function matches(type: string, words: string[]): boolean {
  return words.some((word) => type.includes(word));
}

/**
 * 这次调用跑在哪："" = 事件没说这个事实（只读工具、被拒绝的调用、老任务）；
 * "host" = 进程真的落在宿主上跑；其余 = 沙箱后端名。
 * 后端刻意把 `sandbox_backend: null` 和"没有这个键"分开下发，所以这里必须按键的存在性判断，
 * 不能用取值判断——取成 undefined 就把"宿主执行"和"没跑命令"混成了同一档。
 */
function eventBoundary(event: AgentEvent): string {
  const payload = event.payload || {};
  if (!Object.prototype.hasOwnProperty.call(payload, "sandbox_backend")) return "";
  const value = payload.sandbox_backend;
  return typeof value === "string" && value.trim() ? value.trim() : "host";
}

/**
 * 这一次调用**实际被什么强制**。后端只在起了进程的调用上下发 `sandbox_enforcement`，
 * 键不存在就返回 undefined —— 拿默认值填一遍，"未知"就会在界面上长成"没强制"或"已强制"的样子。
 */
function eventBoundaryFacts(event: AgentEvent): BoundaryFacts | undefined {
  const payload = event.payload || {};
  // 单次越界（#115）是事件顶层的独立事实，不在 enforcement 里：越界后 enforcement
  // 只剩「限额全没生效」，光看它会把这笔执行读成关档。
  const bypassed = payload.sandbox_bypass === true;
  const raw = payload.sandbox_enforcement;
  if (!raw || typeof raw !== "object") return bypassed ? { bypassed: true } : undefined;
  const value = raw as Record<string, unknown>;
  return {
    strength: typeof value.boundary_strength === "string" ? value.boundary_strength : undefined,
    limitsApplied: typeof value.limits_applied === "boolean" ? value.limits_applied : undefined,
    ...(bypassed ? { bypassed: true } : {}),
  };
}

/**
 * 引用数组的搬运：只认 index / role / kind / chars / sha 这五个字段，别的一个键都不抄。
 * 后端给的是指纹不是正文（§4.1），这里再抄一层就成了第二处会写歪的账。
 */
function refsOf(source: Record<string, unknown>, key: string): HistoryRef[] {
  return arrayOf(source, key)
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
    .map((item) => ({
      index: numOf(item, "index") ?? 0,
      role: textOf(item, "role"),
      kind: textOf(item, "kind"),
      chars: numOf(item, "chars") ?? 0,
      sha: textOf(item, "sha"),
      // 出身轮与跨窗留存（#44）。缺键一律读成"没说"：run 空串、其余 null，
      // 补 0 会凭空造出"第 0 轮 / 第 1 次提问 / 从第 0 窗留下"。
      run: textOf(item, "run"),
      userOrder: numOf(item, "user_order"),
      retainedSince: numOf(item, "retained_since"),
      retainedWindows: numOf(item, "retained_windows"),
    }));
}

/** 窗口链：`window_number` 缺失（#42 之前的老事件）必须是 null，不是 0——0 会说"这是出生窗"。 */
function windowChainOf(payload: Record<string, unknown>): CompactionWindow {
  return {
    windowNumber: numOf(payload, "window_number"),
    windowId: textOf(payload, "window_id"),
    previousWindowId: textOf(payload, "previous_window_id"),
    firstWindowId: textOf(payload, "first_window_id"),
    responseId: textOf(payload, "compaction_response_id"),
  };
}

function replacementOf(payload: Record<string, unknown>): CompactionReplacement {
  const source = recordOf(payload, "replacement");
  return {
    beforeItems: numOf(source, "before_items"),
    afterItems: numOf(source, "after_items"),
    retained: numOf(source, "retained"),
    added: refsOf(source, "added"),
    dropped: refsOf(source, "dropped"),
    items: refsOf(source, "items"),
  };
}

function shellLine(event: AgentEvent): TermLine | null {
  const payload = event.payload;
  const command = textOf(payload, "command", "cmd");
  const body = textOf(payload, "chunk", "line", "lines", "text", "output", "stdout", "stderr", "data", "content", "message");
  if (!command && !body) return null;
  const stream = textOf(payload, "stream", "source").toLowerCase();
  const exitCode = numOf(payload, "exit_code", "exitCode", "returncode");
  const failed = event.type.toLowerCase().includes("error") || stream.includes("err") || (exitCode !== null && exitCode !== 0);
  const looksError = /(\berror\b|错误|failed|FAILED|traceback|exception)/i.test(body);
  const looksWarn = /(\bwarning\b|警告)/i.test(body);
  if (command) return { text: `$ ${command}`, tone: "cmd" };
  if (failed || looksError) return { text: body, tone: "err" };
  if (looksWarn) return { text: body, tone: "warn" };
  return { text: body, tone: "out" };
}

function answersSummary(source: Record<string, unknown>): string {
  const rows = source.answers;
  if (!rows || typeof rows !== "object") return "";
  return Object.values(rows as Record<string, unknown>)
    .map((value) => String(value ?? "").trim())
    .filter(Boolean)
    .join("；");
}

function planItems(event: AgentEvent): { items: string[]; text: string } {
  const raw = arrayOf(event.payload, "plan", "steps", "items", "tasks");
  const items = raw
    .map((item) => {
      if (typeof item === "string") return item;
      const nested = arrayOf(item, "items", "steps").map((leaf) => textOf(leaf, "text", "title", "name", "step") || String(leaf));
      const own = textOf(item, "text", "title", "name", "step", "description", "action");
      return own || nested.join("；");
    })
    .filter((item) => item.trim())
    .map((item) => item.replace(/^\d+[.、)]\s*/, "").trim());
  // 只有字段整个缺失（老事件）才拿正文兜底。
  // 后端明确给了空计划就代表「这次不需要计划」——再拿正文去拆，
  // 一句「你好！我可以帮你…」会被渲染成一张假的执行计划。
  const declared = ["plan", "plan_steps", "steps", "items", "tasks"].some((key) => key in (event.payload ?? {}));
  const fallback = declared
    ? []
    : textOf(event.payload, "text", "summary", "message", "content")
        .split("\n")
        .map((line) => line.replace(/^\s*[-*\d.、)]+\s*/, "").trim())
        .filter((line) => line.length > 2);
  return { items: items.length ? items : fallback, text: textOf(event.payload, "text", "summary") };
}

function describeSystem(event: AgentEvent): { title: string; detail: string; tone: "muted" | "warn" | "danger" | "accent" } | null {
  const type = event.type.toLowerCase();
  const payload = event.payload;
  // 内部节点与埋点事件不进对话流。用户要看的是"做到哪了"，不是
  // "create_plan 节点第 1 步"——这些行曾经占掉事件列表的一大半。
  if (
    type.startsWith("node.") ||
    type.startsWith("context.") ||
    type.startsWith("hook.") ||
    type.startsWith("intent.") ||
    type.startsWith("answer.") ||
    type === "result.delivered" ||
    type === "delivery.recorded" ||
    type === "task.created" ||
    type === "snapshot.created"
  ) {
    return null;
  }
  if (type === "task.started") {
    const parts = [
      textOf(payload, "workspace") ? `工作区 ${textOf(payload, "workspace")}` : "",
      textOf(payload, "provider") ? `供应商 ${textOf(payload, "provider")}` : "",
      textOf(payload, "model") ? `模型 ${textOf(payload, "model")}` : "",
    ].filter(Boolean);
    return { title: "开始执行", detail: parts.join(" · "), tone: "muted" };
  }
  // 权限模式切换：用户必须看见"从现在起只读/恢复写"，这比某一次工具结果重要。
  if (type === "permission.mode_changed") {
    const to = textOf(payload, "to");
    const plan = textOf(payload, "plan");
    if (!to) {
      // 缺方向的老事件不能当作"退出"：那会把只读状态说成已恢复写。
      return {
        title: "权限模式已变化",
        detail: clip(plan ? `计划：${plan}` : "", 180),
        tone: "muted",
      };
    }
    const entering = to === "plan";
    return {
      title: entering ? "已进入计划模式" : "已退出计划模式",
      detail: clip(
        [
          entering ? "只读取证，不写文件、不执行命令" : "恢复默认权限，写操作按规则逐条确认",
          plan ? `计划：${plan}` : "",
        ]
          .filter(Boolean)
          .join(" · "),
        180,
      ),
      tone: entering ? "muted" : "accent",
    };
  }
  if (type === "validation.completed" || type.startsWith("validation")) {
    const command = textOf(payload, "command", "cmd");
    const exitCode = numOf(payload, "exit_code");
    const verdict = verdictOf(boolOf(payload, "passed", "ok"), textOf(payload, "status"));
    if (verdict.tone === "warn") {
      // 没做成自动化校验时，command 和退出码都没有意义（根本没跑），
      // 有意义的是后端给的原因：没授权、等审批、沙箱起不来。
      const reason = textOf(payload, "skipped_reason");
      return {
        title: verdict.title,
        detail: clip([reason, command ? `命令 ${command}` : ""].filter(Boolean).join(" · "), 160),
        tone: verdict.tone,
      };
    }
    return {
      title: verdict.title,
      detail: clip(
        [command ? `命令 ${command}` : "", exitCode !== null ? `退出码 ${exitCode}` : "", verdict.label === "通过" ? "结果 通过" : "结果 未通过"]
          .filter(Boolean)
          .join(" · "),
        160,
      ),
      tone: verdict.tone,
    };
  }
  if (type === "budget.warning" || type === "budget.exceeded") {
    // 预算有两种：步数与 token，token 又分「自己花的」和「派给子代理的」。
    // 原来这一条只报步数，token 用完的那一次也写成「步数预算」——用户看到的解释是错的，
    // 而真正的原因（派出去的那批）一个字都没提。
    const remaining = numOf(payload, "remaining");
    const reason = textOf(payload, "reason");
    const tokens = numOf(payload, "tokens");
    const budget = numOf(payload, "token_budget");
    const delegated = numOf(payload, "tokens_delegated");
    const show = (value: number) => value.toLocaleString("zh-CN");
    const done = type === "budget.exceeded";
    const tokenDriven = tokens !== null && delegated !== null && delegated > 0;
    return {
      title: done ? (tokenDriven ? "预算已用完（含派出的子代理）" : "步数预算已用完")
        : tokenDriven ? "预算即将用完（含派出的子代理）" : "步数预算即将用完",
      detail: clip(
        [
          done ? (textOf(payload, "steps") ? `已执行 ${textOf(payload, "steps")} 步` : "")
            : remaining !== null ? `只剩 ${remaining} 步` : "",
          tokens !== null && budget !== null ? `token ${show(tokens)}/${show(budget)}` : "",
          delegated ? `其中派出 ${show(delegated)}` : "",
          reason && reason !== "步数用完" ? reason : "",
          done ? "转为总结" : "模型开始收尾",
        ]
          .filter(Boolean)
          .join(" · "),
        180,
      ),
      tone: "warn",
    };
  }
  if (type === "attachment.trimmed") {
    // 少给了几个附件必须让用户看见：否则他会以为模型看了全部，然后怀疑模型"没长眼睛"。
    const attached = numOf(payload, "attached");
    const used = numOf(payload, "used");
    const dropped = arrayOf(payload, "dropped");
    const reasons = dropped
      .map((item) => textOf(item as Record<string, unknown>, "reason"))
      .filter(Boolean);
    const why = reasons.includes("over_limit")
      ? "超过单轮容量上限"
      : reasons.includes("no_text")
        ? "文件里没有可读文字"
        : reasons.includes("missing")
          ? "文件已被清理"
          : "无法读取";
    return {
      title: "部分附件没有交给模型",
      detail: clip(
        [
          attached !== null && used !== null ? `带上 ${used}/${attached} 个` : "",
          why,
          dropped.length > 0
            ? dropped
                .map((item) => textOf(item as Record<string, unknown>, "name"))
                .filter(Boolean)
                .slice(0, 3)
                .join("、")
            : "",
        ]
          .filter(Boolean)
          .join(" · "),
        180,
      ),
      tone: "warn",
    };
  }
  if (type === "subagents.planned") {
    const count = numOf(payload, "count");
    const concurrency = numOf(payload, "concurrency");
    return {
      title: `并行分派 ${count ?? "多个"} 个子代理`,
      detail: [
        concurrency !== null ? `并发 ${concurrency}` : "",
        boolOf(payload, "merge") ? "跑完自动合并不冲突的改动" : "只交回 diff，不改主工作区",
      ]
        .filter(Boolean)
        .join(" · "),
      tone: "accent",
    };
  }
  if (type === "subagents.completed") {
    const count = numOf(payload, "count");
    const succeeded = numOf(payload, "succeeded");
    const batch = recordOf(payload, "usage");
    const total = numOf(batch, "total_tokens", "totalTokens");
    const calls = numOf(batch, "calls");
    return {
      title: "并行批次收口",
      detail: clip(
        [
          textOf(payload, "group") ? `批次 ${textOf(payload, "group")}` : "",
          count !== null ? `${succeeded ?? "?"}/${count} 完成` : "",
          total !== null ? `合计 ${total.toLocaleString("zh-CN")} token` : "",
          calls !== null ? `${calls} 笔` : "",
        ]
          .filter(Boolean)
          .join(" · "),
        180,
      ),
      tone: succeeded !== null && count !== null && succeeded < count ? "warn" : "accent",
    };
  }
  if (type === "subagent.started") {
    return {
      title: "子代理开始工作",
      detail: clip(
        [
          boolOf(payload, "isolate") ? "隔离副本" : "只读",
          textOf(payload, "goal"),
        ]
          .filter(Boolean)
          .join(" · "),
        160,
      ),
      tone: "muted",
    };
  }
  if (type === "subagent.completed") {
    const ok = boolOf(payload, "ok");
    const steps = numOf(payload, "steps");
    return {
      title: ok === false ? "子代理未完成" : "子代理完成",
      detail: clip(
        [steps !== null ? `${steps} 步` : "", textOf(payload, "error") || textOf(payload, "summary")]
          .filter(Boolean)
          .join(" · "),
        180,
      ),
      tone: ok === false ? "warn" : "muted",
    };
  }
  if (type === "worktree.merged") {
    const applied = arrayOf(payload, "applied").length;
    const conflicts = arrayOf(payload, "conflicts").length;
    return {
      title: conflicts > 0 ? "副本合并有冲突" : "副本改动已合并",
      detail: `写入 ${applied} 个文件${conflicts > 0 ? ` · 冲突 ${conflicts} 个未写入` : ""}`,
      tone: conflicts > 0 ? "warn" : "accent",
    };
  }
  if (type.startsWith("review")) {
    const approved = boolOf(payload, "approved");
    if (type.includes("unavailable")) return null;
    return {
      title: approved === false ? "自检不通过" : "自检通过",
      detail: clip(textOf(payload, "summary"), 140),
      tone: approved === false ? "warn" : "muted",
    };
  }
  if (type.startsWith("error")) {
    return { title: "运行出错", detail: clip(eventToolOutput(event), 200), tone: "danger" };
  }
  // 没显式识别的内部事件一律不显示。对话流里出现 "memory.consolidated"
  // 这种原始事件名，对用户没有任何信息量。
  return null;
}

/**
 * 把原始事件流折叠成 6 类可读区块：计划 / 工具调用 / 终端 / 审批 / 结果 / 变更。
 * 相邻的 shell 事件合并进同一个终端块，tool.requested 与 tool.completed 按工具名配对成一张卡片。
 */
export function buildBlocks(events: AgentEvent[]): Block[] {
  const blocks: Block[] = [];
  const openToolIndex = new Map<string, number>();
  const openApprovalIndex = new Map<string, number>();
  let lastTerminalIndex: number | null = null;

  const push = (block: Block): number => {
    blocks.push(block);
    return blocks.length - 1;
  };

  events.forEach((event, position) => {
    const rawType = event.type.toLowerCase();
    const key = `${event.seq ?? position}:${event.type}`;
    const isShell = rawType.startsWith("shell") || rawType.startsWith("terminal") || rawType.includes("stdout");
    const isTool = rawType.startsWith("tool") || rawType.startsWith("action");
    const isApproval = rawType.startsWith("approval") || rawType.includes("waiting_approval");
    const isDiff = rawType.startsWith("diff") || rawType.includes("file.changed") || rawType.includes("file_change");
    const isResult =
      rawType.startsWith("result") || rawType.startsWith("task.completed") || rawType.startsWith("task.failed") || rawType.startsWith("task.cancelled") || rawType.startsWith("task.canceled") || rawType.startsWith("error");

    // 压缩边界是一张卡：`context.compacted` 一次任务只发生几回，
    // 但它标记的正是"从这里往前看，历史已经不是原文"。
    // 同族的 context.measured 每步都发（几百条），继续留在对话流之外。
    if (rawType === "context.compacted") {
      const micro = recordOf(event.payload, "micro");
      push({
        kind: "boundary",
        key,
        seq: event.seq,
        node: event.node,
        at: event.createdAt,
        folded: numOf(event.payload, "folded") ?? 0,
        covered: numOf(event.payload, "covered") ?? 0,
        tokensBefore: numOf(event.payload, "active_context_tokens_before") ?? 0,
        tokensAfter: numOf(event.payload, "active_context_tokens_after") ?? 0,
        tokensSaved: numOf(event.payload, "tokens_saved") ?? 0,
        waterRatio: numOf(event.payload, "water_ratio") ?? 0,
        window: numOf(event.payload, "context_window") ?? 0,
        triggeredBy: textOf(event.payload, "triggered_by"),
        microClipped: numOf(micro, "clipped") ?? 0,
        microCharsSaved: numOf(micro, "chars_saved") ?? 0,
        windowChain: windowChainOf(event.payload),
        replacement: replacementOf(event.payload),
      });
      return;
    }

    if (rawType.startsWith("plan")) {
      const { items, text } = planItems(event);
      // 这次没有计划（直接回答）：不建卡，答案由最终结果块呈现。
      // 空卡片会渲染成「暂无计划条目」，纯噪音。
      if (items.length === 0) return;
      push({ kind: "plan", key, seq: event.seq, node: event.node, at: event.createdAt, items, text });
      return;
    }

    if (rawType.startsWith("question")) {
      const interrupt = (event.payload as { interrupt?: Record<string, unknown> } | null)?.interrupt;
      const source = interrupt ? { ...event.payload, ...interrupt } : event.payload;
      if (rawType.includes("answered") || rawType.includes("decided")) {
        const last = blocks[blocks.length - 1];
        if (last?.kind === "question") {
          blocks[blocks.length - 1] = {
            ...last,
            state: "answered",
            answers: textOf(source, "answer", "text") || answersSummary(source),
          };
        }
        return;
      }
      // question.asked 与 question.required 是同一次中断的两条记录，只留一张卡
      const last = blocks[blocks.length - 1];
      if (last?.kind === "question" && last.state === "pending") return;
      const questions = normalizeQuestions({ questions: source.questions ?? source.items ?? [] });
      if (questions.length === 0) return;
      push({
        kind: "question",
        key,
        seq: event.seq,
        at: event.createdAt,
        questions,
        state: "pending",
        answers: "",
      });
      return;
    }

    if (isApproval) {
      const interrupt = (event.payload as { interrupt?: Record<string, unknown> } | null)?.interrupt;
      const source = interrupt ? { ...event.payload, ...interrupt } : event.payload;
      const tool = textOf(source, "tool", "name", "action") || "";
      if (rawType.includes("grant") || rawType.includes("approve")) {
        const index = openApprovalIndex.get(tool);
        if (index !== undefined) {
          const target = blocks[index];
          if (target?.kind === "approval") blocks[index] = { ...target, state: "granted" };
          openApprovalIndex.delete(tool);
          return;
        }
        push({
          kind: "system",
          key,
          seq: event.seq,
          at: event.createdAt,
          title: `已批准：${tool || "高风险操作"}`,
          detail: "",
          tone: "accent",
        });
        return;
      }
      if (rawType.includes("deny") || rawType.includes("reject")) {
        const index = openApprovalIndex.get(tool);
        if (index !== undefined) {
          const target = blocks[index];
          if (target?.kind === "approval") blocks[index] = { ...target, state: "denied" };
          openApprovalIndex.delete(tool);
          return;
        }
        push({
          kind: "system",
          key,
          seq: event.seq,
          at: event.createdAt,
          title: `已拒绝：${tool || "高风险操作"}`,
          detail: "",
          tone: "danger",
        });
        return;
      }
      // 只有真正的审批请求才建卡；task.waiting_approval 与 approval.required 指向同一次请求，保留一张。
      const isRequest =
        rawType.includes("required") || rawType.includes("requested") || rawType.includes("waiting_approval");
      if (!isRequest) return;
      if (tool && openApprovalIndex.has(tool)) return;
      const block: Block = {
        kind: "approval",
        key,
        seq: event.seq,
        at: event.createdAt,
        tool: tool || "高风险操作",
        toolCallId: textOf(source, "tool_call_id", "id", "approval_id"),
        args: eventToolArgs({ ...event, payload: source }),
        risk: textOf(source, "risk_level", "risk", "level"),
        message: textOf(source, "message", "reason", "approval_state"),
        prefix: textOf(source, "suggested_prefix", "prefix"),
        scopes: arrayOf(source, "approval_options", "scopes")
          .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
          .map((item) => ({
            scope: (textOf(item, "scope", "value") || "once") as ApprovalScope,
            label: textOf(item, "label", "title"),
            warning: textOf(item, "warning", "hint"),
          })),
        state: "pending",
        guardian: guardianFromPayload(source),
      };
      openApprovalIndex.set(tool, push(block));
      return;
    }

    if (isResult) {
      const cancelled = rawType.includes("cancel");
      const failed = rawType.includes("fail") || rawType.startsWith("error") || normalizeStatus(pick(event.payload, "status")) === "failed";
      const status: "succeeded" | "failed" | "cancelled" = cancelled ? "cancelled" : failed ? "failed" : "succeeded";
      push({
        kind: "result",
        key,
        seq: event.seq,
        at: event.createdAt,
        status,
        title: textOf(event.payload, "title", "status") || (status === "succeeded" ? "任务完成" : status === "failed" ? "任务失败" : "任务已取消"),
        summary: textOf(event.payload, "summary", "result", "text", "message"),
        reason: textOf(event.payload, "reason", "failure_reason", "error"),
      });
      return;
    }

    if (isShell) {
      const line = shellLine(event);
      if (!line) return;
      const title = textOf(event.payload, "command", "title") || "终端输出";
      if (lastTerminalIndex !== null && blocks[lastTerminalIndex] && blocks[lastTerminalIndex].kind === "terminal" && event.seq - (blocks[lastTerminalIndex] as { seq: number }).seq <= 1) {
        const target = blocks[lastTerminalIndex] as Extract<Block, { kind: "terminal" }>;
        blocks[lastTerminalIndex] = { ...target, lines: [...target.lines, line] };
        return;
      }
      lastTerminalIndex = push({ kind: "terminal", key, seq: event.seq, at: event.createdAt, title, lines: [line] });
      return;
    }

    if (isDiff) {
      push({
        kind: "change",
        key,
        seq: event.seq,
        at: event.createdAt,
        path: textOf(event.payload, "path", "file", "file_path") || "未知文件",
        changeType: textOf(event.payload, "change_type", "type", "kind"),
        diff: textOf(event.payload, "diff", "patch"),
      });
      return;
    }

    if (isTool) {
      const name = eventToolName(event);
      const isStart = matches(rawType, START_WORDS) && !matches(rawType, DONE_WORDS);
      const status = normalizeStatus(pick(event.payload, "status", "state"));
      if (isStart) {
        const block: Block = {
          kind: "tool",
          key,
          seq: event.seq,
          node: event.node,
          at: event.createdAt,
          name,
          args: eventToolArgs(event),
          output: "",
          status: "running",
          durationMs: null,
          risk: textOf(event.payload, "risk_level", "risk"),
          toolCallId: textOf(event.payload, "tool_call_id", "id"),
          boundary: "",
        };
        openToolIndex.set(name, push(block));
        return;
      }
      const index = openToolIndex.get(name);
      const output = eventToolOutput(event);
      const duration = numOf(event.payload, "duration_ms", "latency_ms", "elapsed_ms");
      const risk = textOf(event.payload, "risk_level", "risk");
      const callId = textOf(event.payload, "tool_call_id", "id");
      const boundary = eventBoundary(event);
      const boundaryFacts = eventBoundaryFacts(event);
      if (index !== undefined && blocks[index]?.kind === "tool") {
        const target = blocks[index] as Extract<Block, { kind: "tool" }>;
        blocks[index] = {
          ...target,
          output: output || target.output,
          status: status && status !== "running" ? status : "ok",
          durationMs: duration ?? target.durationMs,
          risk: risk || target.risk,
          toolCallId: callId || target.toolCallId,
          boundary: boundary || target.boundary,
          boundaryFacts: boundaryFacts ?? target.boundaryFacts,
        };
        openToolIndex.delete(name);
        return;
      }
      // 没有配对到发起事件时也要完整呈现这一次调用。
      const block: Block = {
        kind: "tool",
        key,
        seq: event.seq,
        node: event.node,
        at: event.createdAt,
        name,
        args: eventToolArgs(event),
        output,
        status: status || "ok",
        durationMs: duration,
        risk,
        toolCallId: callId,
        boundary,
        boundaryFacts,
      };
      if (block.status === "running") openToolIndex.set(name, push(block));
      else push(block);
      return;
    }

    const described = describeSystem(event);
    if (!described) return;
    push({
      kind: "system",
      key,
      seq: event.seq,
      at: event.createdAt,
      title: described.title,
      detail: described.detail,
      tone: described.tone,
    });
  });

  return blocks;
}
