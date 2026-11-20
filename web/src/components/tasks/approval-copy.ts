/**
 * 审批卡顶上那句话问的是「要不要让它做这件事」，不是「这是一个 run_shell 审批」。
 *
 * 用户在这一格要做的唯一决定是同意还是不同意，标题就该是他心里那个问句——
 * 「需要你批准：run_shell」得先在脑子里翻译一遍，才知道自己在批准什么。
 *
 * 放在 .ts 而不是 .tsx：这是纯文案，能直接单测。渲染部分在 approval.tsx。
 */

/**
 * 计划模式的进出。这两张卡问的不是"执行一个叫 exit_plan_mode 的动作"——
 * 那是实现细节。用户要答的是「按这份计划开始改吗」，所以文案完全不同，
 * 卡片还得把计划本身摆出来（渲染在审批卡里，读 `planPreview`）。
 */
const PLAN_TOOLS: Record<string, true> = { exit_plan_mode: true, enter_plan_mode: true };

export function isPlanGate(tool: string): boolean {
  return PLAN_TOOLS[(tool ?? "").toLowerCase()] === true;
}

export function approvalQuestion(tool: string, args: Record<string, unknown>): string {
  const path = String(args.path ?? args.file_path ?? args.target ?? "");
  const lower = (tool ?? "").toLowerCase();
  // 计划门排在最前面：`exit_plan_mode` 里带 "plan"，但它是模式开关，不是写文件。
  if (lower === "exit_plan_mode") return "计划做完了，按这份开始改吗？";
  if (lower === "enter_plan_mode") return "要先进计划模式（只看不改）吗？";
  if (/(write|edit|patch|create|delete|move|rename|apply)/.test(lower)) {
    return path ? `要让它改 ${path} 吗？` : "要让它改这些文件吗？";
  }
  if (/(shell|bash|exec|run|command|terminal)/.test(lower)) return "要让它跑这条命令吗？";
  if (/(fetch|browse|search|http|web)/.test(lower)) return "要让它联网查东西吗？";
  return `要让它执行 ${tool} 吗？`;
}

/**
 * 计划门这张卡上，批准按钮该叫什么。
 *
 * 「允许这一次」在三档权限里是对的默认说法，但放在计划卡上说不通：用户批的不是
 * "允许它退出一个模式"，是"这份计划可以动手了"。按钮是这句确认的最后一个落点，
 * 说法对不上，他会在点之前犹豫一下——而这一步本来该是最果断的那一下。
 */
export function approvalApproveLabel(tool: string, scope: string, fallback: string): string {
  if (!isPlanGate(tool)) return fallback;
  if ((tool ?? "").toLowerCase() === "enter_plan_mode") return fallback;
  return scope === "once" ? "开始执行" : fallback;
}
