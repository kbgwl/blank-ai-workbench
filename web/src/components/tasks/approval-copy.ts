/**
 * 审批卡顶上那句话问的是「要不要让它做这件事」，不是「这是一个 run_shell 审批」。
 *
 * 用户在这一格要做的唯一决定是同意还是不同意，标题就该是他心里那个问句——
 * 「需要你批准：run_shell」得先在脑子里翻译一遍，才知道自己在批准什么。
 *
 * 放在 .ts 而不是 .tsx：这是纯文案，能直接单测。渲染部分在 approval.tsx。
 */
export function approvalQuestion(tool: string, args: Record<string, unknown>): string {
  const path = String(args.path ?? args.file_path ?? args.target ?? "");
  const lower = (tool ?? "").toLowerCase();
  if (/(write|edit|patch|create|delete|move|rename|apply)/.test(lower)) {
    return path ? `要让它改 ${path} 吗？` : "要让它改这些文件吗？";
  }
  if (/(shell|bash|exec|run|command|terminal)/.test(lower)) return "要让它跑这条命令吗？";
  if (/(fetch|browse|search|http|web)/.test(lower)) return "要让它联网查东西吗？";
  return `要让它执行 ${tool} 吗？`;
}
