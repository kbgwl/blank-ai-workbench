import type {
  Boundaries,
  BoundaryEntry,
  GuardianDecision,
  GuardianHistory,
  GuardianVerdictInfo,
  GuardianWindow,
  PermissionProfile,
  UsageBranch,
  UsageParallelGroup,
  UsageTurn,
} from "./api.ts";

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
  /**
   * 这一次是**经人工批准的单次越界**（`sandbox_bypass=true`），不是"本来就没开沙箱"。
   * 缺键 = 老任务没记过这一格，不许读成 false；false 才是"记了、没越界"。
   */
  bypassed?: boolean;
};

/**
 * 边界那一枚角标。
 * `local` 不是一种沙箱：它是"没有容器，进程就在这个宿主上跑"。把它写成「沙箱：local」
 * 就是本立项要消灭的那类谎。有 Job 的时候能说清强制了哪三项，没有的时候必须说不清。
 */
export function boundaryLabel(boundary: string, facts: BoundaryFacts = {}): string {
  if (!boundary) return "";
  if (boundary === "host") {
    // 越界与关档都是宿主执行，但前者是"人工批准后绕过沙箱"，必须单独说：
    // 只写「宿主执行」会把一次显式授权读成"这台机器本来就没隔离"。
    if (facts.bypassed === true) return "宿主执行（单次越界：已绕过沙箱）";
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
  // 越界永远警告：这次命令确实没被沙箱约束，与它当时是什么档位无关。
  if (facts.bypassed === true) return true;
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

// ---------------------------------------------------------------------------
// 边界报告与审阅轨迹（#77 的 permission_profile + #79 的 guardian_history）
//
// 这一族的文案全部是判定，所以写在模块里而不是 JSX 里：它们必须能被断言，
// 也要能在被改坏时打红。共同的纪律是「没记」与「记了是零」不许共用一个读法。
// ---------------------------------------------------------------------------

/** 缺键念「未记」，真 0 念 0 —— 后者是事实，前者是这份报告没覆盖到。 */
const count = (value: number | null): string => (value === null ? "未记" : value.toLocaleString("zh-CN"));

const STRENGTH_TEXT: Record<string, string> = {
  os_enforced: "文件与网络由系统强制",
  job_only: "仅内存/进程数/进程树由内核强制",
  none: "无内核强制",
  unknown: "边界探测不可信，不承诺任何边界",
};

/** 档位那一行：要求的、实际的、降级没有、谁在强制，四件事一次说完。 */
export function sandboxTierLabel(boundaries: Boundaries): string {
  if (!boundaries.recorded) return "";
  const tier = boundaries.tier || "未记档位";
  const wanted = boundaries.requested && boundaries.requested !== tier ? `（要求 ${boundaries.requested}）` : "";
  const degraded = boundaries.degraded === true ? " · 已降级" : "";
  const strength = boundaries.strength
    ? STRENGTH_TEXT[boundaries.strength] ?? `边界强度 ${boundaries.strength}`
    : "边界强度未记";
  return `档位 ${tier}${wanted}${degraded} · ${strength}`;
}

/** 该亮警告的三种情形：档位被部署策略降级、内核层没管到文件与网络、或压根没人强制写入。 */
export function boundariesNeedWarn(boundaries: Boundaries): boolean {
  if (!boundaries.recorded) return false;
  // 有 MCP 服务端没进 Job：它按宿主权限跑，限额与退出清理都不受内核管，必须提示。
  if (boundaries.mcp.unboundedServers.length > 0) return true;
  if (boundaries.degraded === true) return true;
  if (boundaries.strength && boundaries.strength !== "os_enforced") return true;
  return boundaries.profile.entries.some((entry) => entry.enforcedBy === "none" && entry.access === "write");
}

const ENFORCED_BY_TEXT: Record<string, string> = {
  os: "系统强制",
  policy: "策略闸门（执行前判定，不是系统拒）",
  none: "无人强制",
};

const ACCESS_TEXT: Record<string, string> = {
  write: "可写",
  read: "只读",
  deny: "拒绝",
  none: "不进挂载表",
};

/** 一条逐条授权。尾巴上"谁在保证"是这一行存在的理由——只有路径和读写，就会被读成系统会拒。 */
export function permissionEntryLabel(entry: BoundaryEntry): string {
  const path = entry.path || "未命名路径";
  const access = ACCESS_TEXT[entry.access] ?? (entry.access || "未记");
  const enforced = ENFORCED_BY_TEXT[entry.enforcedBy] ?? (entry.enforcedBy || "未记");
  return `${path} · ${access} · ${enforced}`;
}

/** 「不存在的目标怎么算」：这是闸门的真实语义，认不出来就原样露出来，不套中文。 */
export function missingPathLabel(behavior: string): string {
  if (!behavior) return "";
  if (behavior === "skip") return "不存在的目标不参与越界判定（skip）";
  if (behavior === "fail") return "不存在的目标按拒绝处理（fail）";
  if (behavior === "not_applicable") return "未开「仅限工作区」，这条不适用";
  return behavior;
}

export function networkGuardLabel(network: Boundaries["profile"]["network"]): string {
  if (!network) return "";
  const wanted = network.requested === "open" ? "要求可通网" : network.requested === "none" ? "要求不通网" : "网络要求未记";
  const by = ENFORCED_BY_TEXT[network.enforcedBy] ?? (network.enforcedBy || "未记");
  const gate = network.commandGate === "deny" ? "命令闸门拒绝" : network.commandGate === "allow" ? "命令闸门放行" : "命令闸门未记";
  return `${wanted} · ${by} · ${gate}`;
}

/** 每一笔调用落在哪一层。五类分开数，是因为「MCP 宿主进程」根本不进任务沙箱。 */
export function toolCallBoundaryLabel(boundaries: Boundaries): string {
  const { blocked, mcpHostProcess, process: spawned, inProcess, unattributed } = boundaries.toolCalls;
  const parts = [
    `进程内 ${count(inProcess)}`,
    `起进程 ${count(spawned)}`,
    `MCP 宿主 ${count(mcpHostProcess)}`,
    `被档位拦 ${count(blocked)}`,
    `未归属 ${count(unattributed)}`,
  ];
  if (unattributed !== null && unattributed > 0) parts.push("（未归属=当时没记边界，不猜成进程内）");
  return parts.join(" · ");
}

const VERDICT_TEXT: Record<string, string> = { allow: "放行", deny: "否决", abstain: "未表态" };

const OUTCOME_TEXT: Record<string, string> = {
  asked: "交给人批准",
  blocked: "审查直接拒",
  auto_approved: "审查代批",
};

/** 一条判定的读法。审批指纹与命令原文都不在这行里：那是内部标识与用户数据，不上汇总行。 */
export function guardianDecisionLabel(decision: GuardianDecision): string {
  const parts = [
    `第 ${decision.seq ?? "?"} 条`,
    decision.tool || "未知工具",
    VERDICT_TEXT[decision.verdict] ?? (decision.verdict || "判定未记"),
    `出自 ${decision.runId ? shortTurnId(decision.runId) : "无名轮"}`,
  ];
  // 去向缺键就是"记这条的时候还没有这一格"，不补成"没去向"。
  if (decision.outcome !== null) parts.push(`去向 ${OUTCOME_TEXT[decision.outcome] ?? decision.outcome}`);
  if (decision.model) {
    parts.push(decision.latencyMs === null ? decision.model : `${decision.model} ${decision.latencyMs.toLocaleString("zh-CN")}ms`);
  }
  if (decision.timedOut) parts.push("超时");
  if (decision.error) parts.push("审查出错");
  return parts.join(" · ");
}

/** 一次压缩：审阅侧看得见几条、模型侧只剩几条——Codex 实物是 96 vs 18，丢的是模型的历史。 */
export function guardianWindowLabel(window: GuardianWindow): string {
  const parts = [
    `第 ${window.windowNumber ?? "?"} 窗`,
    `审阅侧 ${count(window.itemsReviewed)} 条 · 模型侧 ${count(window.itemsInModelContext)} 条`,
    `判定 ${count(window.reviews)} 条随账`,
    `${count(window.reviewedChars)} 字`,
  ];
  const effect = window.inEffect;
  const effectParts = [
    effect.guardian_mode ? `审查=${String(effect.guardian_mode)}` : "",
    effect.approval_mode ? `审批=${String(effect.approval_mode)}` : "",
    effect.window_tokens ? `窗口 ${Number(effect.window_tokens).toLocaleString("zh-CN")}` : "",
  ].filter(Boolean);
  if (effectParts.length) parts.push(`生效 ${effectParts.join(" ")}`);
  return parts.join(" · ");
}

/** 轨迹汇总行。「带上判定」数的是窗，不是判定条数：跨运行续跑时判定只有几条，窗却有七面。 */
export function guardianHeadline(trail: GuardianHistory): string {
  if (!trail.recorded) return "";
  const carried = trail.windows.filter((window) => (window.reviews ?? 0) > 0).length;
  const parts = [
    `判定 ${trail.decisions.length} 条`,
    `代批 ${trail.autoApprovals.length} 次`,
    `压缩 ${trail.windows.length} 窗（其中 ${carried} 窗带上判定）`,
  ];
  if (trail.windowsUnrecorded !== null && trail.windowsUnrecorded > 0) parts.push(`另有 ${trail.windowsUnrecorded} 次压缩没记轨迹`);
  return parts.join(" · ");
}

/**
 * 审批卡上的审查意见。三种状态必须长得不一样：
 * `undefined`=这张卡是"审查进载荷之前"的老任务（整段不显示）；
 * `null`=记过了、当时审查关着（要说，否则批准的人以为没人看过）；
 * 对象=审查员真说了话。
 */
export function guardianReviewLabel(guardian: GuardianVerdictInfo | null | undefined): string {
  if (guardian === undefined) return "";
  if (guardian === null) return "独立审查未开启：这次没有第二个模型看过这条操作";
  const verdict = VERDICT_TEXT[guardian.verdict] ?? (guardian.verdict || "未表态");
  const head = `审查员：${verdict}`;
  const note = guardian.timedOut ? "（审查超时，按未表态处理）" : guardian.error ? `（审查出错：${guardian.error}）` : "";
  const reasons = guardian.reasons.filter(Boolean).join("；");
  return reasons ? `${head}${note} · ${reasons}` : `${head}${note}`;
}

/** 后端有没有真的强制这一层：null（没这一格）与 false（记过了、没强制）不是一回事。 */
export function enforcedFlagLabel(value: boolean | null): string {
  if (value === null) return "未记";
  return value ? "系统强制" : "未强制";
}

/** 权限快照的来历。unrestricted 是「按宿主权限执行」这个事实，不是「没配」。 */
export function permissionProfileTypeLabel(profile: PermissionProfile): string {
  if (!profile.type) return "权限快照未记（后端早于 M4）";
  if (profile.type === "managed") return "受管权限（逐条授权见下）";
  if (profile.type === "unrestricted") return "不受限：按宿主权限直接执行";
  if (profile.type === "unrecorded") return "无权限快照：该任务当时没记";
  return profile.type;
}

/** MCP 那一路的边界事实。挂进 Job 的 stdio、远端的 http、没挂进去的进程必须分开说。 */
export function mcpBoundaryLabel(boundaries: Boundaries): string {
  const mcp = boundaries.mcp;
  if (!mcp.boundary && mcp.calls === null && mcp.servers.length === 0 && !mcp.note) return "";
  const parts = [
    `MCP 边界 ${mcp.boundary || "未记"}`,
    `服务端 ${mcp.servers.length} 个${mcp.servers.length > 0 ? `（${mcp.servers.join("、")}）` : ""}`,
    `调用 ${count(mcp.calls)} 次`,
  ];
  if (mcp.unboundedServers.length > 0) {
    parts.push(`⚠ 未进 Job：${mcp.unboundedServers.join("、")}（按宿主权限运行，未由内核强制）`);
  }
  if (mcp.note) parts.push(mcp.note);
  return parts.join(" · ");
}

/** 这次任务里出现过的档位序列。空数组=后端没记过，不能念成「一路 off」。 */
export function sandboxModesLabel(modes: string[]): string {
  return modes.length === 0 ? "" : modes.join(" → ");
}

/** shell 那一格：载荷没给时整行不显示，显示出来就是替后端编了一句。 */
export function shellGrantLabel(enabled: boolean | null): string {
  if (enabled === null) return "";
  return enabled ? "shell 允许" : "shell 禁止";
}
