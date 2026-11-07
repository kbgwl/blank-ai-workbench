/**
 * v165 §9.1 顶部状态条的纯数据层。
 *
 * 与渲染分开是为了能被 node:test 直接钉住口径：状态条最怕「各处各拉一份」
 * 导致同一时刻显示互相矛盾的值，这里定义唯一的取值与文案规则。
 */

export type GoalStatusBarData = {
  scenario: string;
  model: string;
  automationMode: string;
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

const SCENARIO_LABELS: Record<string, string> = {
  dev: "开发",
  docs: "文档",
  daily: "日常",
  brainstorm: "头脑风暴",
  reverse: "逆向分析",
};

const AUTOMATION_LABELS: Record<string, string> = {
  manual: "手动",
  assist: "辅助",
  autonomous: "自动驾驶",
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

export function scenarioLabel(key: string): string {
  return SCENARIO_LABELS[key] || "开发";
}

export function automationModeLabel(mode: string): string {
  return AUTOMATION_LABELS[mode] || "辅助";
}

export function goalStatusLabel(status: string): string {
  return GOAL_STATUS_LABELS[status] || "";
}

/** 把状态条要显示的几格算成数组：纯函数，node:test 能直接钉住口径。 */
export function statusBarChips(data: GoalStatusBarData): StatusChip[] {
  const chips: StatusChip[] = [
    { key: "scenario", label: "场景", value: scenarioLabel(data.scenario), tone: "plain" },
    { key: "model", label: "模型", value: data.model || "未选择", tone: "plain" },
    {
      key: "automation",
      label: "模式",
      value: automationModeLabel(data.automationMode),
      tone: data.automationMode === "autonomous" ? "brand" : "plain",
    },
  ];
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
