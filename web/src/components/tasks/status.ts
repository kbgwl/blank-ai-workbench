import type { UsageBranch, UsageParallelGroup, UsageTurn } from "./api.ts";

export type StatusMeta = {
  label: string;
  dot: string;
  text: string;
  chip: string;
};

const STATUS_META: Record<string, StatusMeta> = {
  queued: {
    label: "排队中",
    dot: "bg-brand animate-pulse",
    text: "text-brand",
    chip: "border-brand/35 bg-brand/10 text-brand",
  },
  draft: {
    label: "草稿",
    dot: "bg-faint",
    text: "text-muted",
    chip: "border-line bg-surface-2 text-muted",
  },
  running: {
    label: "运行中",
    dot: "bg-brand animate-pulse",
    text: "text-brand",
    chip: "border-brand/35 bg-brand/10 text-brand",
  },
  waiting_approval: {
    label: "待审批",
    dot: "bg-warn",
    text: "text-warn",
    chip: "border-warn/35 bg-warn/[0.08] text-warn",
  },
  paused: {
    label: "已暂停",
    dot: "bg-faint",
    text: "text-muted",
    chip: "border-line bg-surface-2 text-muted",
  },
  succeeded: {
    label: "已完成",
    dot: "bg-accent",
    text: "text-accent",
    chip: "border-accent/35 bg-accent/10 text-accent",
  },
  failed: {
    label: "失败",
    dot: "bg-danger",
    text: "text-danger",
    chip: "border-danger/35 bg-danger/10 text-danger",
  },
  cancelled: {
    label: "已取消",
    dot: "bg-faint",
    text: "text-faint",
    chip: "border-line bg-surface-2 text-faint",
  },
};

const FALLBACK: StatusMeta = {
  label: "未知状态",
  dot: "bg-faint",
  text: "text-muted",
  chip: "border-line bg-surface-2 text-muted",
};

export function statusMeta(status: string | null | undefined): StatusMeta {
  const key = (status ?? "").toLowerCase();
  const known = STATUS_META[key];
  if (known) return known;
  if (!key) return FALLBACK;
  return { ...FALLBACK, label: key };
}

/** 工具调用状态（ok / error / denied / timeout / awaiting_approval）。 */
export function toolStatusClass(status: string): { label: string; className: string } {
  switch (status) {
    case "ok":
    case "succeeded":
      return { label: "成功", className: "text-accent" };
    case "running":
      return { label: "执行中", className: "text-brand" };
    case "error":
    case "failed":
      return { label: "失败", className: "text-danger" };
    case "timeout":
      return { label: "超时", className: "text-warn" };
    case "denied":
      return { label: "已拒绝", className: "text-danger" };
    case "awaiting_approval":
    case "waiting_approval":
      return { label: "待审批", className: "text-warn" };
    case "cancelled":
      return { label: "已取消", className: "text-faint" };
    default:
      return { label: status || "—", className: "text-muted" };
  }
}

export const RISK_LABELS: Record<string, string> = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
  critical: "严重风险",
};

export function riskLabel(risk: string): string {
  if (!risk) return "";
  return RISK_LABELS[risk.toLowerCase()] ?? risk;
}

/** 这一次调用跑在哪。"" = 事件没说这个事实（只读工具、老任务），不显示。
 * "host" = 进程真的落在宿主上跑（沙箱没开），这是最该露出来的一档。 */
/** 这一次工具调用的执行事实，来自 tool.completed 事件里的 `sandbox_enforcement`。
 * 全部可选：老任务没有这个键，读不到就不能编。 */
export type BoundaryFacts = {
  /** os_enforced / job_only / none / unknown */
  strength?: string;
  /** 关档（沙箱未启用）时是 false：连内存上限都没在执行 */
  limitsApplied?: boolean;
};

/**
 * 边界那一枚角标。
 * `local` 不是一种沙箱：它是"没有容器，进程就在这个宿主上跑"。把它写成「沙箱：local」
 * 就是本立项要消灭的那类谎。有 Job 的时候能说清强制了哪三项，没有的时候必须说不清。
 */
export function boundaryLabel(boundary: string, facts: BoundaryFacts = {}): string {
  if (!boundary) return "";
  if (boundary === "host") {
    return facts.limitsApplied === false ? "宿主执行（未隔离，限额未生效）" : "宿主执行（未隔离）";
  }
  if (boundary === "local") {
    if (facts.strength === "os_enforced") return "宿主执行 + 系统强制的文件/网络边界";
    if (facts.strength === "job_only") return "宿主执行 + 内核限额（内存/进程数/进程树）· 文件与网络未强制";
    if (facts.strength === "none") return "宿主执行（无任何内核强制）";
    return "宿主执行（未隔离）";
  }
  return `沙箱：${boundary}`;
}

/** 只有容器、或系统真的拒文件/网络时，这枚角标才不该是警告色。 */
export function boundaryNeedsWarn(boundary: string, facts: BoundaryFacts = {}): boolean {
  if (boundary === "host" || boundary === "local") return facts.strength !== "os_enforced";
  return false;
}

const NODE_LABELS: Record<string, string> = {
  receive_task: "接收任务",
  understand_task: "理解任务",
  inspect_workspace: "检查工作区",
  create_plan: "制定计划",
  approval_if_needed: "审批判断",
  execute_action: "执行动作",
  run_validation: "运行验证",
  summarize_result: "汇总结果",
  waiting_for_user: "等待用户",
  complete: "结束",
};

export function nodeLabel(node: string): string {
  if (!node) return "";
  return NODE_LABELS[node] ?? node;
}

const TRIGGER_LABELS: Record<string, string> = {
  tokens: "按 token 水位",
  chars: "按字符预算",
};

/** 事件里的真实取值带阶段后缀（`tokens:fold`、`chars:micro`），前缀才是判定依据。 */
const TRIGGER_STAGE_LABELS: Record<string, string> = {
  micro: "只做轻量裁剪",
  fold: "折叠了历史",
};

export function compactionTriggerLabel(triggeredBy: string): string {
  if (!triggeredBy) return "";
  const [base = "", stage = ""] = triggeredBy.toLowerCase().split(":");
  const head = TRIGGER_LABELS[base];
  // 认不出的前缀原样露出来，不编一个像模像样的中文标签。
  if (!head) return triggeredBy;
  if (!stage) return head;
  return `${head} · ${TRIGGER_STAGE_LABELS[stage] ?? stage}`;
}

/**
 * 压缩卡片正面的一句话。
 * `covered` 才是"边界"——它说"从这里往前 covered 条已经不是原文"；
 * `folded` 只是这一次折了几条。两个都必须报，缺了任何一个就会把
 * "水位推进但这次没折新内容"读成"什么都没发生"。
 */
export function compactionHeadline(covered: number, folded: number, tokensSaved: number): string {
  if (!covered && !folded) return "上下文已压缩，但事件没给出边界位置";
  const moved = folded ? `这次折入 ${folded} 条` : "这次没有新内容折入，只是水位推进";
  const saved = tokensSaved ? `，省 ${tokensSaved} tokens` : "";
  return `历史前 ${covered} 条已被摘要替换（${moved}${saved}）`;
}

/** 水位读数：窗口未知（老事件/未配窗口）时不给百分比，避免编一个分母。 */
export function compactionWaterLabel(waterRatio: number, window: number): string {
  if (!window || !waterRatio) return "";
  // 比率可以大于 1：真链路实测到「窗口 1600 · 压缩前 9264 tokens」= 5.79 倍。
  // 这不是量表算错，是压缩前真的塞不下，所以大于 1 改说倍数，不印 579% 那种看着像 bug 的数。
  if (waterRatio > 1) return `超出窗口 ${waterRatio.toFixed(1)} 倍 · 窗口 ${window}`;
  return `水位 ${Math.round(waterRatio * 100)}% · 窗口 ${window}`;
}

/**
 * 历史里的一条被换成「指认得出来、但不抄正文」的引用。
 * 对标 Codex `compacted.replacement_history`（§4.1：那边一条 30 万字节，正文原样写进本机日志），
 * 我们的事件会进证据包、被同项目其他人看到、还进全文索引，所以只搬结构与指纹。
 */
export type HistoryRef = {
  index: number;
  role: string;
  kind: string;
  chars: number;
  sha: string;
  /**
   * 这条历史的出生轮（#44，对标 Codex 每条消息都带 `..._passthrough.turn_id`）。
   * 空串 = 后端没记过出身（回合归属上线前写的，或从别的任务带进来的问答）。
   * 不许读成"第 0 轮"或"主线程"——那是把没记过的说成记过。
   */
  run: string;
  /** 第几次用户输入（对标 `guardian_metadata.user_order`）。null = 这条不是用户输入，与第 0 次不同。 */
  userOrder: number | null;
  /** 从第几窗起留下 / 已跨过几窗（对标 `retained_source`）。null = 没说，不是第 1 窗、不是 1 窗。 */
  retainedSince: number | null;
  retainedWindows: number | null;
};

export type CompactionWindow = {
  /** null = 这条事件早于窗口链（#42 之前的老任务），渲染时不许当成"第 0 窗"。 */
  windowNumber: number | null;
  windowId: string;
  previousWindowId: string;
  firstWindowId: string;
  /** 做这次压缩的那笔供应商调用的 id；只记指针，用量在它自己的账里。 */
  responseId: string;
};

export type CompactionReplacement = {
  beforeItems: number | null;
  afterItems: number | null;
  /** null = 事件里没有 replacement 记录（老任务）；0 是"真的一个都没保留"，两者不能混。 */
  retained: number | null;
  added: HistoryRef[];
  dropped: HistoryRef[];
  items: HistoryRef[];
};

export function compactionWindowLabel(window: CompactionWindow): string {
  if (window.windowNumber === null) return "";
  return `第 ${window.windowNumber} 窗`;
}

/** 链条那一行只在真的接得上时才给 id 前缀：链断了（老事件只有号没有 id）就不硬凑一句。 */
export function compactionChainLabel(window: CompactionWindow): string {
  const head = compactionWindowLabel(window);
  if (!head) return "";
  if (!window.previousWindowId) return head;
  return `${head} · 接续 ${window.previousWindowId.slice(0, 8)}`;
}

export function compactionReplacementLabel(replacement: CompactionReplacement): string {
  if (replacement.retained === null) return "";
  const parts = [`保留 ${replacement.retained} 条`, `丢弃 ${replacement.dropped.length} 条`];
  if (replacement.added.length) parts.push(`新进 ${replacement.added.length} 条`);
  return parts.join(" · ");
}

/** 一条引用的读法：位置 · 种类 · 字数 · 指纹前 8 位（+ 出身轮、第几次提问、跨窗留存）。正文一个字都不在这里。 */
export function historyRefLabel(ref: HistoryRef): string {
  const parts = [
    `#${ref.index}`,
    ref.kind || ref.role || "未知",
    `${ref.chars} 字`,
    ref.sha ? ref.sha.slice(0, 8) : "无指纹",
  ];
  // 出生轮：有就念得出来（attempt 键念"主线程#N"），没有就整段不说——不说 ≠ 说了"没有"。
  if (ref.run) parts.push(`出自 ${shortTurnId(ref.run)}`);
  if (ref.userOrder !== null) parts.push(`第 ${ref.userOrder + 1} 次提问`);
  if (ref.retainedSince !== null && ref.retainedWindows !== null && ref.retainedWindows > 1) {
    parts.push(`自第 ${ref.retainedSince} 窗留下 · 已跨 ${ref.retainedWindows} 窗`);
  }
  return parts.join(" · ");
}

/**
 * 缓存命中行。要把"打开了提示词缓存"和"缓存真的命中"区分开，只有把命中量
 * 跟输入量并排放才成立。两个数缺一个就不给百分比：没有分母的百分比是编的。
 */
export function usageCacheLabel(cached: number | null, prompt: number | null): string {
  if (cached === null || prompt === null || prompt <= 0) return "";
  const ratio = Math.round((cached / prompt) * 100);
  return `${ratio}% 走缓存（${cached.toLocaleString("zh-CN")} / ${prompt.toLocaleString("zh-CN")}）`;
}

/**
 * 三级口径：一次调用、本回合、整个任务各花了多少（对标 Codex token_usage_record）。
 * 只把三个数并排写出来才看得出"这一步便宜是因为命中缓存"还是"这回合根本没多跑"。
 * 缺项写破折号，不用 0 顶替 —— 0 会说"这一次没花钱"，而事实是"这一项没记到"。
 */
export function usageTrailLabel(
  call: number | null,
  turn: number | null,
  task: number | null
): string {
  if (call === null && turn === null && task === null) return "";
  const show = (value: number | null) => (value === null ? "—" : value.toLocaleString("zh-CN"));
  return `单次 ${show(call)} · 本回合 ${show(turn)} · 任务累计 ${show(task)}`;
}

/**
 * 有几轮就写几轮，外加"多少笔钱归属不明"。
 * `turns === null`（后端没给这个键）返回空串，让面板显示「暂无」；
 * `turns === []` 是后端说了"一笔归属得到的账都没有"，这时候要写出来而不是留空。
 */
export function usageTurnsLabel(turns: UsageTurn[] | null, unattributed: number | null): string {
  if (turns === null) return "";
  const parts = [`共 ${turns.length} 轮`];
  if (unattributed !== null && unattributed > 0) parts.push(`${unattributed} 笔归属未记`);
  return parts.join(" · ");
}

/**
 * 线程/轮 id 的读法。主线程的 id 形状是 `task:<任务 id>:attempt:<N>`，砍前 8 位只剩
 * "task:fd2" —— 同一任务里每一轮、每一条分支的父引用会长成一个样，等于没指认。
 * 这种形状念成「主线程#N」（attempt 键只由主会话产生），其余（run 台账的 uuid、
 * 子代理的随机 trace id）仍取前 8 位。
 *
 * 团队链是第三种形状（:8062 真跑一轮群聊量出来的）：`services/team.py` 给每位发言成员
 * 现建一个任务，轮 id 写成 `team-<群轮 id>-<发言 id>`，放行续跑再补 `-resume`。
 * 它同样禁不起砍前八位——同一轮的三条发言 `…-38 / …-39 / …-40` 会全念成 "team-016"，
 * 而发言 id 在群内自增、跨轮也不重复，所以直接念成「群聊第 N 条发言」就够指认了。
 */
const TEAM_RUN_ID = /^team-[0-9a-f-]+-(\d+)(-resume)?$/i;

function shortTurnId(id: string): string {
  const attempt = /^task:[0-9a-f-]+:attempt:(\d+)$/i.exec(id);
  if (attempt) return `主线程#${attempt[1]}`;
  const team = TEAM_RUN_ID.exec(id);
  if (team) return `群聊第 ${team[1]} 条发言${team[2] ? "（放行后续跑）" : ""}`;
  return id.slice(0, 8);
}

/**
 * 一轮的读法：轮 id（能念出来的那种）· 合计 · 笔数（派过子代理才点出来）。
 * 缺的数用破折号，不用 0 —— 0 会说"这一轮没花钱"，事实是"这一轮的合计没算出来"。
 */
export function usageTurnLabel(turn: UsageTurn): string {
  const show = (value: number | null) => (value === null ? "—" : value.toLocaleString("zh-CN"));
  const parts = [turn.runId ? shortTurnId(turn.runId) : "无名轮"];
  parts.push(`合计 ${show(turn.totalTokens)}`);
  parts.push(`${show(turn.calls)} 笔`);
  if (turn.subagentCalls !== null && turn.subagentCalls > 0) parts.push(`含子代理 ${turn.subagentCalls} 笔`);
  return parts.join(" · ");
}

/**
 * 一条分支的读法：路径 · 合计 · 谁引起的。
 * 父线程为空有两种截然不同的意思，必须分开说：有轮 id 说明这笔账是归属上线后记的，
 * 空就是"没人派它"（主代理本身）；连轮 id 都没有就是老账，那时候连路径都是空的，
 * 把它读成主代理等于替它编一个出身。
 */
export function usageBranchLabel(branch: UsageBranch): string {
  const show = (value: number | null) => (value === null ? "—" : value.toLocaleString("zh-CN"));
  const parts = [branch.agentPath || "无路径", `合计 ${show(branch.totalTokens)}`, `${show(branch.calls)} 笔`];
  if (branch.parentTraceId) parts.push(`派自 ${shortTurnId(branch.parentTraceId)}`);
  else if (branch.runId) parts.push("主代理本身");
  else parts.push("归属未记");
  return parts.join(" · ");
}

/**
 * 一次并行编排（fan_out）的读法：批次号 · 合计 · 笔数 · 几条线程真花了钱。
 * `threads` 是账本上出现过的线程数，不是"派了几个"——跑挂在前任何一笔的子代理一行账都没有，
 * 把它算进线程数等于替它记了一笔没发生的开销；「派了几个」由时间线那条 `subagents.planned` 说。
 */
export function usageParallelGroupLabel(batch: UsageParallelGroup): string {
  const show = (value: number | null) => (value === null ? "—" : value.toLocaleString("zh-CN"));
  const parts = [`批次 ${batch.group || "无名"}`, `合计 ${show(batch.totalTokens)}`, `${show(batch.calls)} 笔`];
  if (batch.threads !== null) parts.push(`${batch.threads} 条线程`);
  if (batch.runId) parts.push(`出自 ${shortTurnId(batch.runId)}`);
  return parts.join(" · ");
}

/**
 * 「派给子代理多少」这一行。两个数都缺就不写（返回空串，由面板显示「暂无」）。
 * 关键在不能反推：任务累计里没这个拆分时，不许拿"总 - 分支合计"凑一个数出来，
 * 那种减法会把摘要、审查员这些非子代理的开销一起算成派出去的。
 */
export function delegatedSpendLabel(tokens: number | null, calls: number | null): string {
  if (tokens === null && calls === null) return "";
  const show = (value: number | null) => (value === null ? "—" : value.toLocaleString("zh-CN"));
  const parts: string[] = [];
  if (tokens !== null) parts.push(`派给子代理 ${show(tokens)}`);
  if (calls !== null) parts.push(`${show(calls)} 笔`);
  return parts.join(" · ");
}
