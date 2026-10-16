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
