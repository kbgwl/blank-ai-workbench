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

/** 打回时可选的目标步骤。 */
export type SendBackChoice = { id: string; label: string };

/** 标题截断上限：下拉项一行读完，不撑爆卡片。 */
const SEND_BACK_LABEL_CHARS = 16;

/**
 * 前端兜底出来的 id（`parsePlanSteps` 给没有 id 的老计划编的 `legacy-N` / `idx-N`）。
 *
 * 这类值不能进下拉：后端按 id 找不到那一步，结果是「驳回 + 打回第 N 步」被当成
 * 普通驳回——用户以为退回了，计划其实没动。今天后端会落一条 plan.rewind_missed
 * 说明这件事，但**不该让用户先选一个必然失败的项**。真实步骤 id 是内容寻址的
 * sha1 前缀，不会长成这个样子。
 */
const FABRICATED_STEP_ID = /^(?:legacy|idx)-\d+$/;

/**
 * 驳回这张卡能打回到哪几步。
 *
 * 只列**做过或正在做的**步骤：打回到一个还没开始的地方没有可重做的产物，
 * 列出来只会让"打回"看起来像"跳到未来"。`cancelled` 也不列——那一步是主动放弃的，
 * 退回去等于把它复活，语义上说不通。
 *
 * 文案用「第 N 步」而不是内部 id：id 是 `step-3` 这种，用户对着它没法判断
 * 自己要退回哪里；序号与计划面板上的编号是同一套。
 */
export function sendBackChoices(
  steps: ReadonlyArray<{ id?: string; text?: string; status?: string }> | null | undefined,
): SendBackChoice[] {
  const choices: SendBackChoice[] = [];
  (steps ?? []).forEach((step, index) => {
    const id = String(step?.id ?? "").trim();
    const text = String(step?.text ?? "").trim();
    if (!id || !text) return;
    // 前端自己编的兜底 id 后端匹配不上，列出来只会让用户点一个必然无效的动作。
    if (FABRICATED_STEP_ID.test(id)) return;
    // failed 也要能打回：那正是最该重做的一步。只列 completed/in_progress 的话，
    // 某步失败了反而只能退回上一个成功步骤，把已经做对的东西也推倒重来。
    const status = step?.status;
    if (status !== "completed" && status !== "in_progress" && status !== "failed") return;
    const title =
      text.length > SEND_BACK_LABEL_CHARS ? `${text.slice(0, SEND_BACK_LABEL_CHARS)}…` : text;
    choices.push({ id, label: `第 ${index + 1} 步 ${title}` });
  });
  return choices;
}
