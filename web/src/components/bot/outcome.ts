export type OutcomeTone = "accent" | "danger" | "warn" | "muted";

export type TurnOutcome = {
  label: string;
  tone: OutcomeTone;
};

/**
 * 这一轮结束时该说哪一个词。
 *
 * 认不出的状态不写成「完成」：旧后端、配置错误、未知终态都会走到兜底，
 * 把它说成完成等于替后端编一个结论。宁可写「已结束」。
 */
export function turnOutcome(status: string, running: boolean): TurnOutcome {
  if (running) return { label: "正在做", tone: "accent" };
  if (status === "failed") return { label: "失败", tone: "danger" };
  if (status === "cancelled") return { label: "已停止", tone: "danger" };
  if (status === "waiting_approval" || status === "approval_expired") {
    return { label: "等你批准", tone: "warn" };
  }
  if (status === "paused") return { label: "已暂停", tone: "warn" };
  if (status === "succeeded") return { label: "完成", tone: "accent" };
  return { label: "已结束", tone: "muted" };
}

export type OutcomePart = {
  text: string;
  tone: "muted" | "danger";
};

export type OutcomeEvidence = {
  looked: number;
  files: number;
  commandCount: number;
  failed: number;
  calls: number;
};

/**
 * 摘要只保留有用的量：看了几处、改了几个文件、跑了几条命令、失败几次。
 * 空项不出现；一次调用都没有时退回「N 步」，避免摘要行变成空白。
 */
export function outcomeParts(evidence: OutcomeEvidence, stepCount: number): OutcomePart[] {
  const parts: OutcomePart[] = [];
  if (evidence.looked > 0) parts.push({ text: `看了 ${evidence.looked}`, tone: "muted" });
  if (evidence.files > 0) parts.push({ text: `改了 ${evidence.files} 个文件`, tone: "muted" });
  if (evidence.commandCount > 0) {
    parts.push({ text: `跑了 ${evidence.commandCount} 条命令`, tone: "muted" });
  }
  if (evidence.calls === 0 && stepCount > 0) {
    parts.push({ text: `${stepCount} 步`, tone: "muted" });
  }
  if (evidence.failed > 0) parts.push({ text: `${evidence.failed} 次失败`, tone: "danger" });
  return parts;
}
