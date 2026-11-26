/**
 * 「节点之间为什么跳」该怎么说。
 *
 * 后端把每次跳转记成 ``agent.transition``（``rule`` 机读码 + ``why`` 一句话 +
 * 支撑它的数字），但一直只有事件流一个消费方：任务页看得到工具卡片一串，
 * 看不到"为什么又跑一轮"。用户要分清"还有待办"、"校验没过在修"、"审查驳回退回"，
 * 只能靠翻证据时间线猜。
 *
 * 这里只做一件事：把 ``rule`` 翻成一句短话。口径与 ``error-copy.ts`` 一致——
 *
 * 1. **码优先，文本兜底**。``rule`` 是后端写死的字面量，比 ``why`` 那句中文稳定；
 *    句子里的数字（"还有 3 项待办"）仍用后端那句，前端不重算。
 * 2. **认不出就说没登记**。返回 ``registered: false``，界面标黄并把原始 ``rule``
 *    一起放出来。绝不按前缀猜一个"看起来像"的理由——时间线上出现一行错的解释，
 *    比没有解释更糟：排查的人会照着它走错方向。
 * 3. 纯函数，不 import React 也不 import 别名路径——``node --test`` 能直接跑。
 */

/** 一行跳转的分量：muted = 常规回路，warn = 需要人看一眼（没登记），accent = 收尾。 */
export type HopTone = "muted" | "warn" | "accent";

export type HopCopy = {
  /** 一句短话，进时间线那一行。认不出时是"跳转原因没登记"。 */
  label: string;
  tone: HopTone;
  /** false = 后端出现了本文件没登记的 rule。界面要把它显式露出来。 */
  registered: boolean;
};

/**
 * rule → 短话。键与后端 ``services/api/src/blank_api/services/agent/transition.py``
 * 的 ``_result(..., "<rule>", ...)`` 一一对应，判据是一条读那份源码的测试
 * （``transition-copy.test.ts``）：后端加了分支而这里没跟上，测试会红。
 */
const BY_RULE: Record<string, { label: string; tone: HopTone }> = {
  // ── create_plan ────────────────────────────────────────────────────────
  direct_answer: { label: "这次不用动工作区，直接作答", tone: "accent" },
  plan_ready: { label: "计划已拆好，进入执行", tone: "accent" },

  // ── execute_action ─────────────────────────────────────────────────────
  cancelled: { label: "被取消，直接收尾", tone: "warn" },
  paused: { label: "被暂停，先收尾", tone: "warn" },
  errored: { label: "执行出错，不再进修复回路", tone: "warn" },
  environment_gap: { label: "环境缺口还没补，继续装/找", tone: "muted" },
  plan_incomplete: { label: "计划里有事没做，推回执行", tone: "muted" },
  steps_remaining: { label: "还有待办，继续执行", tone: "muted" },
  steps_done: { label: "待办清空，进入校验", tone: "muted" },
  list_only: { label: "只要清单，直接按工具回执出列表", tone: "accent" },

  // ── run_validation ─────────────────────────────────────────────────────
  readonly_no_artifact: { label: "只读任务，没有交付物可校验", tone: "accent" },
  validation_skipped: { label: "没有可跑的校验命令，直接总结", tone: "muted" },
  ui_repair_reserve: { label: "预算已尽，保留一次界面修复", tone: "warn" },
  validation_failed_retry: { label: "校验没过，回执行修", tone: "warn" },
  validation_passed: { label: "校验通过，进入审查", tone: "accent" },
  validation_not_observed: { label: "没有校验结果，没有失败证据，进入审查", tone: "muted" },
  repair_budget_spent: { label: "修复轮次用尽，不再修", tone: "warn" },
  repair_exhausted: { label: "同一处反复失败，不再修", tone: "warn" },
  budget_exhausted: { label: "步数预算用完，不再修", tone: "warn" },

  // ── review_result ──────────────────────────────────────────────────────
  review_rejected_retry: { label: "审查驳回，退回执行", tone: "warn" },
  review_approved: { label: "审查通过，进入收尾", tone: "accent" },
  review_round_limit: { label: "审查轮次用尽，按现状收尾", tone: "warn" },

  // ── summarize_result ───────────────────────────────────────────────────
  stop_blocked: { label: "交付前被 Stop 钩子拦下，继续干", tone: "warn" },
  delivered: { label: "收尾完成，本轮结束", tone: "accent" },

  // 后端刻意用的"没登记"码：它不是错误，是记账没跟上代码。照样显示，
  // 让人知道这句解释缺席，而不是以为这一跳没有理由。
  unexplained: { label: "跳转原因没登记", tone: "warn" },
};

/**
 * 一次跳转该显示什么。
 *
 * ``rule`` 为空（升级前的旧事件、或后端换了字段名）同样按"没登记"处理：
 * 不能因为"没有码"就当作正常跳转静默吞掉。
 */
export function hopCopy(rule: string): HopCopy {
  const key = String(rule ?? "").trim();
  const hit = BY_RULE[key];
  if (hit) return { ...hit, registered: true };
  return { label: "跳转原因没登记", tone: "warn", registered: false };
}

/** 这一行要不要把原始 rule 露出来：认不出时必须露，否则用户无从查起。 */
export function hopCode(rule: string): string {
  return hopCopy(rule).registered ? "" : String(rule ?? "").trim() || "（空）";
}

/**
 * 这一行到底印哪句话。
 *
 * 后端那句 `why` 更精确（带"还有 1 项待办""已用 2/3 轮修复"这类数字），
 * 标签只是它的摘要——两句一起印就成了同一件事说两遍。所以：
 *
 * - 有 `why` → 印 `why`；标签退到悬停提示里（分类仍看得见）；
 * - 没有 `why`（老事件、或后端没给话）→ 印标签，别留一行空白；
 * - 认不出的 `rule` → 印"没登记"+原始码，`why` 一律不猜。
 */
export function hopLine(
  rule: string,
  why: string,
): { text: string; tone: HopTone; code: string } {
  const copy = hopCopy(rule);
  if (!copy.registered) return { text: copy.label, tone: copy.tone, code: hopCode(rule) };
  const detail = String(why ?? "").trim();
  return { text: detail || copy.label, tone: copy.tone, code: "" };
}

/**
 * 同一段回路重复了几次，怎么写在标签后面。
 *
 * 两种重复要分开说：``repeat`` 是连着的（中间没有任何动作块），
 * ``occurrence`` 是这一轮里第几次出现同一个跳转（中间隔着工具调用）。
 * 后者正是"为什么又跑一轮"的答案，所以两者都写，但措辞不同。
 */
export function hopRepeat(block: { repeat: number; occurrence: number }): string {
  if (block.repeat > 1) return `连续 ${block.repeat} 次`;
  if (block.occurrence > 1) return `第 ${block.occurrence} 次`;
  return "";
}
