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
  /**
   * 排在最前的那个后台任务的标题与状态。
   *
   * 只说「后台 4 个任务」，用户不知道是哪四个、更不知道有没有卡在他身上。
   * 原来这句话下面还挂着一条橙色横幅专门讲同一个后台任务——同一件事在一屏里
   * 说了两遍，两条都是横幅，看起来就是"两条打架的告示"。现在收进这一格。
   */
  backgroundLabel?: string;
  backgroundStatus?: string;
  serviceOnline: boolean;
};

export type StatusChip = {
  key: string;
  label: string;
  value: string;
  tone: "plain" | "brand" | "warn" | "danger" | "muted";
  /**
   * 只用一个点表达状态，不把整格染成警示色。
   *
   * 目标那一格已经把「等你批准」四个字写出来了，后台那一格如果也整格变橙，
   * 一行里就是两个一模一样的橙药丸——用户第一反应是"这两条是不是同一件事"。
   * 后台是另一件事，用灰底 + 一个橙点指出来就够了。
   */
  dot?: "warn" | "danger" | "brand";
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
    // 状态只用一个小圆点表达，不把整格染色：目标那一格已经整格橙了，
    // 后台再整格橙，一行里就是两个一模一样的橙药丸，像在说同一件事。
    const status = data.backgroundStatus ?? "";
    const dot: StatusChip["dot"] =
      status === "failed"
        ? "danger"
        : status === "waiting_approval" || status === "paused"
          ? "warn"
          : "brand";
    // 这里不再写「等你批准」四个字：目标那一格已经在说状态了，同一行两个
    // 「等你批准」用户只会问该点哪个。点说"要你看"，标题说"哪一条"。
    const title = (data.backgroundLabel ?? "").trim();
    // 「4 个 · 冒烟：审批」：数量在前、标题在后，截断时先丢标题尾巴而不是数量。
    const countPart = data.backgroundCount > 1 ? `${data.backgroundCount} 个` : "";
    chips.push({
      key: "background",
      label: "后台",
      value: title ? (countPart ? `${countPart} · ${title}` : title) : `${data.backgroundCount} 个任务`,
      tone: "plain",
      dot,
    });
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
