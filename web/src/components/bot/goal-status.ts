/**
 * 对话顶部状态条的纯数据层。
 *
 * 这里只回答一个问题：**这一轮现在在做什么**（目标进度、后台还有几个任务、
 * 后端是不是断了）。场景、模型、权限档不在这里出现——它们本来就在输入框
 * 工具条上各有一个可点的选择器，再在顶部重复一遍，用户只会问
 * 「模式 辅助 是什么东西」。
 *
 * 与渲染分开是为了能被 node:test 直接钉住口径：状态条最怕「各处各拉一份」
 * 导致同一时刻显示互相矛盾的值，这里定义唯一的取值与文案规则。
 */

export type GoalStatusBarData = {
  goal: string;
  goalStatus: string;
  backgroundCount: number;
  serviceOnline: boolean;
};

export type StatusChip = {
  key: string;
  label: string;
  value: string;
  tone: "plain" | "brand" | "warn" | "danger" | "muted";
};

const GOAL_STATUS_LABELS: Record<string, string> = {
  captured: "已记录",
  clarifying: "待澄清",
  planned: "已规划",
  executing: "执行中",
  waiting_approval: "等你批准",
  waiting_user: "等你补充",
  verifying: "验证中",
  repairing: "修复中",
  succeeded: "已完成",
  failed: "失败",
  paused: "已暂停",
  cancelled: "已取消",
  archived: "已归档",
};

export function goalStatusLabel(status: string): string {
  return GOAL_STATUS_LABELS[status] || "";
}

/** 把状态条要显示的几格算成数组：纯函数，node:test 能直接钉住口径。 */
export function statusBarChips(data: GoalStatusBarData): StatusChip[] {
  const chips: StatusChip[] = [];
  if (data.goal) {
    const status = goalStatusLabel(data.goalStatus);
    chips.push({
      key: "goal",
      label: "目标",
      value: status ? `${data.goal} · ${status}` : data.goal,
      tone:
        data.goalStatus === "waiting_approval" || data.goalStatus === "waiting_user"
          ? "warn"
          : data.goalStatus === "failed"
            ? "danger"
            : data.goalStatus === "executing" || data.goalStatus === "repairing"
              ? "brand"
              : "muted",
    });
  }
  if (data.backgroundCount > 0) {
    chips.push({ key: "background", label: "后台", value: `${data.backgroundCount} 个任务`, tone: "plain" });
  }
  return chips;
}

/**
 * 整条要不要渲染。
 *
 * 新对话、没有目标、后端也正常时，顶部那条除了占一行高度什么也不说。
 * 主流 agent 的新会话页都是干净的，所以这里宁可整条不出现。
 */
export function statusBarVisible(data: GoalStatusBarData): boolean {
  return Boolean(data.goal) || data.backgroundCount > 0 || !data.serviceOnline;
}
