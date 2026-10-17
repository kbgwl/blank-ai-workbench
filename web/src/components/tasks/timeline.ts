import {
  arrayOf,
  boolOf,
  eventToolArgs,
  eventToolName,
  eventToolOutput,
  numOf,
  normalizeQuestions,
  normalizeStatus,
  pick,
  textOf,
  type AgentEvent,
  type ApprovalScope,
  type PendingQuestion,
} from "./api";
import { clip } from "./format";

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
      scopes: { scope: ApprovalScope; label: string }[];
      state: "pending" | "granted" | "denied";
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
  const fallback = textOf(event.payload, "text", "summary", "message", "content")
    .split("\n")
    .map((line) => line.replace(/^\s*[-*\d.、)]+\s*/, "").trim())
    .filter((line) => line.length > 2);
  return { items: items.length ? items : fallback, text: textOf(event.payload, "text", "summary") };
}

function describeSystem(event: AgentEvent): { title: string; detail: string; tone: "muted" | "warn" | "danger" | "accent" } {
  const type = event.type.toLowerCase();
  const payload = event.payload;
  if (type === "node.started") {
    const step = numOf(payload, "step");
    // 默认预算 200 步，" / 共 N 步" 常驻显示只会变成噪音；只有人为把预算调小过才值得展示分母。
    const max = numOf(payload, "max_steps");
    const showMax = max !== null && max <= 50;
    return {
      title: event.node || "节点",
      detail: step !== null ? `第 ${step} 步${showMax ? ` / 共 ${max} 步` : ""}` : "",
      tone: "muted",
    };
  }
  if (type === "task.started") {
    const parts = [
      textOf(payload, "workspace") ? `工作区 ${textOf(payload, "workspace")}` : "",
      textOf(payload, "provider") ? `供应商 ${textOf(payload, "provider")}` : "",
      textOf(payload, "model") ? `模型 ${textOf(payload, "model")}` : "",
    ].filter(Boolean);
    return { title: "任务开始运行", detail: parts.join(" · "), tone: "muted" };
  }
  if (type === "validation.completed" || type.startsWith("validation")) {
    const command = textOf(payload, "command", "cmd");
    const exitCode = numOf(payload, "exit_code");
    const passed = boolOf(payload, "passed", "ok");
    return {
      title: passed === false ? "验证未通过" : "验证完成",
      detail: clip([command ? `命令 ${command}` : "", exitCode !== null ? `退出码 ${exitCode}` : passed === null ? "" : passed ? "结果 通过" : "结果 未通过"].filter(Boolean).join(" · "), 160),
      tone: passed === false ? "danger" : "accent",
    };
  }
  if (type === "budget.warning") {
    const remaining = numOf(payload, "remaining");
    return {
      title: "步数预算即将用完",
      detail: remaining !== null ? `只剩 ${remaining} 步，模型开始收尾` : "模型开始收尾",
      tone: "warn",
    };
  }
  if (type === "budget.exceeded") {
    return {
      title: "步数预算已用完",
      detail: textOf(payload, "steps") ? `已执行 ${textOf(payload, "steps")} 步，转为总结` : "转为总结",
      tone: "warn",
    };
  }
  if (type.startsWith("error")) {
    return { title: "运行出错", detail: clip(eventToolOutput(event), 200), tone: "danger" };
  }
  return { title: event.type, detail: clip(eventToolOutput(event), 200), tone: "muted" };
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

    if (rawType.startsWith("plan")) {
      const { items } = planItems(event);
      push({ kind: "plan", key, seq: event.seq, node: event.node, at: event.createdAt, items, text: textOf(event.payload, "text", "summary") });
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
          })),
        state: "pending",
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
        };
        openToolIndex.set(name, push(block));
        return;
      }
      const index = openToolIndex.get(name);
      const output = eventToolOutput(event);
      const duration = numOf(event.payload, "duration_ms", "latency_ms", "elapsed_ms");
      const risk = textOf(event.payload, "risk_level", "risk");
      const callId = textOf(event.payload, "tool_call_id", "id");
      if (index !== undefined && blocks[index]?.kind === "tool") {
        const target = blocks[index] as Extract<Block, { kind: "tool" }>;
        blocks[index] = {
          ...target,
          output: output || target.output,
          status: status && status !== "running" ? status : "ok",
          durationMs: duration ?? target.durationMs,
          risk: risk || target.risk,
          toolCallId: callId || target.toolCallId,
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
      };
      if (block.status === "running") openToolIndex.set(name, push(block));
      else push(block);
      return;
    }

    const described = describeSystem(event);
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
