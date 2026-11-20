/**
 * 工具调用的「人话」翻译层。
 *
 * 卡片上原来直接写 `list_files` + `{"path":"web/src",...}`，对非工程用户
 * 等于噪音。这里统一转成三样东西：
 *   label  —— 这一步在干什么（列出文件 / 读取文件 / 修改文件）
 *   target —— 对谁做的（哪个目录、哪个文件、哪条命令）
 *   result —— 做出来什么（几项、几行、几处命中）
 * 原始参数与原始输出只在展开后才出现。
 */

export type ToolFact = { label: string; value: string };

export type ToolView = {
  label: string;
  target: string;
  result: string;
  facts: ToolFact[];
  /** 可读的条目清单（列出来的文件、命中的代码行），展开时直接看，不用读原始 JSON。 */
  items: string[];
};

const TOOL_LABELS: Record<string, string> = {
  list_files: "列出文件",
  read_file: "读取文件",
  search_code: "搜索代码",
  glob: "按名字找文件",
  apply_patch: "修改文件",
  write_file: "写入文件",
  edit_file: "修改文件",
  notebook_edit: "修改笔记本",
  run_shell: "执行命令",
  repl: "运行代码",
  run_tests: "运行测试",
  git: "查看改动",
  lsp: "查代码索引",
  browser_check: "打开页面查看",
  web_search: "联网搜索",
  web_fetch: "读取网页",
  update_plan: "更新步骤",
  set_goal: "整理目标",
  propose_goal: "整理目标",
  enter_plan_mode: "进入计划模式",
  exit_plan_mode: "退出计划模式",
  enter_worktree: "开出隔离工作区",
  exit_worktree: "收尾隔离工作区",
  memory_search: "查找记忆",
  memory_write: "记住这件事",
  read_notes: "读取笔记",
  write_notes: "记录笔记",
  spawn_agent: "分派子任务",
  spawn_agents: "分派多路子任务",
  request_user_input: "向你确认",
  report_findings: "提交结论",
  tool_search: "查找工具",
  find_skills: "查找技能",
  list_curated_skills: "查看技能库",
  propose_skills: "提议技能",
  install_market_skill: "安装技能",
  skill: "调用技能",
  monitor: "等待运行结果",
  schedule_wakeup: "安排稍后再来",
  send_message: "发送消息",
  workflow: "编排流程",
  cron_create: "设置定时任务",
  cron_list: "查看定时任务",
  cron_delete: "删除定时任务",
  list_agents: "查看可用助手",
  list_mcp_resources: "列出外部资源",
  read_mcp_resource: "读取外部资源",
  read_mcp_resource_dir: "浏览外部资源目录",
  refresh_mcp_tools: "刷新外部工具",
  task_create: "新建子任务",
  task_list: "查看子任务",
  task_get: "查看子任务详情",
  task_update: "更新子任务",
  task_output: "读子任务输出",
  task_stop: "停止子任务",
  team_status: "查看团队状态",
  team_create_bot: "新建团队成员",
  team_update_bot: "调整团队成员",
  team_add_member: "加入团队",
  team_remove_member: "移出团队",
  team_assign_task: "给成员派活",
};

/**
 * 一步在跑的时候，这一行该写什么。
 *
 * 原来对话里的步骤行直接显示工具名：`read_file` / `apply_patch` / `run_shell`。
 * 那是接口字段名，不是"它在干什么"——非工程用户看到的就是一串带下划线的英文。
 * 任务详情页早就用 TOOL_LABELS 翻译过了，对话页没有，同一件事两个界面两种说法。
 *
 * **阶段只有三种，所以不建 61 × 3 的表**：中文的进行体是加前缀（正在…），
 * 失败是加后缀（…失败）。61 条名词短语 + 两条规则，比 183 条文案更好维护，
 * 也不会出现"新加了一个工具、忘了补它的三种时态"那种半截翻译。
 *
 * 没登记的工具返回空串，调用方继续显示原始名字——`正在read_weird_tool` 比
 * `read_weird_tool` 更难懂。
 */
export function toolPhaseLabel(name: string, status: "running" | "ok" | "failed"): string {
  const label = TOOL_LABELS[(name || "").toLowerCase()];
  if (!label) return "";
  if (status === "running") return `正在${label}`;
  if (status === "failed") return `${label}失败`;
  return label;
}

/** 这个工具登记过中文说法没有。展开详情里据此决定要不要补一行原始工具名。 */
export function hasToolLabel(name: string): boolean {
  return Boolean(TOOL_LABELS[(name || "").toLowerCase()]);
}

function str(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "";
}

function firstLine(text: string): string {
  return text.split(/\r?\n/).find((line) => line.trim())?.trim() ?? "";
}

function clip(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

function lineCount(text: string): number {
  if (!text.trim()) return 0;
  return text.split(/\r?\n/).filter((line) => line.trim().length > 0).length;
}

/** list_files 的输出是缩进树，每行 `name` 或 `name  (123 字节)`。 */
function treeLines(output: string): string[] {
  return output
    .split(/\r?\n/)
    .map((line) => line.replace(/\s*\(\d+\s*字节\)\s*$/, "").replace(/\s+$/, ""))
    .filter((line) => line.trim());
}

function treeNames(output: string): string[] {
  return treeLines(output)
    .map((line) => line.trim())
    .map((line) => line.split("/").filter(Boolean).pop() ?? line);
}

function patchStat(patch: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of patch.split(/\r?\n/)) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

function testStat(output: string): string {
  const passed = /(\d+)\s+passed/i.exec(output)?.[1];
  const failed = /(\d+)\s+failed/i.exec(output)?.[1];
  if (passed || failed) {
    return [passed ? `通过 ${passed}` : "", failed ? `失败 ${failed}` : ""].filter(Boolean).join(" · ");
  }
  return "";
}

function failureText(output: string): string {
  const line =
    output
      .split(/\r?\n/)
      .map((item) => item.trim())
      .find((item) => /(error|failed|错误|失败|无法|denied)/i.test(item)) ?? firstLine(output);
  return line ? `未完成：${clip(line, 80)}` : "";
}

/** 参数里第一个有意义的字符串，作为兜底目标。 */
function anyText(args: Record<string, unknown>): string {
  for (const value of Object.values(args ?? {})) {
    const text = str(value);
    if (text) return clip(text, 120);
  }
  return "";
}

export function describeTool(
  name: string,
  args: Record<string, unknown>,
  output: string,
  failed: boolean,
): ToolView {
  const key = (name || "").toLowerCase();
  const label = TOOL_LABELS[key] ?? name;
  const facts: ToolFact[] = [];
  const add = (factLabel: string, value: string) => {
    if (value) facts.push({ label: factLabel, value });
  };
  const bad = failed ? failureText(output) : "";

  let target = "";
  let result = "";
  let items: string[] = [];
  const LIST_LIMIT = 80;

  const listItems = (text: string, limit = LIST_LIMIT): string[] => {
    const lines = text.split(/\r?\n/).map((line) => line.replace(/\s+$/, "")).filter((line) => line.trim());
    if (lines.length <= limit) return lines;
    return [...lines.slice(0, limit), `… 还有 ${lines.length - limit} 项`];
  };

  switch (key) {
    case "list_files": {
      const path = str(args.path);
      const depth = str(args.depth);
      target = path ? `目录 ${path}` : "工作区根目录";
      add("目录", path || "工作区根目录");
      add("展开层级", depth);
      const names = treeNames(output);
      result = names.length
        ? `${names.length} 项 · ${names.slice(0, 4).join("、")}${names.length > 4 ? " 等" : ""}`
        : "目录为空";
      const rawTree = output
        .split(/\r?\n/)
        .map((line) => line.replace(/\s+$/, ""))
        .filter((line) => line.trim());
      // 目录后面补一个 /，否则「chat」和「chat-home.tsx」在同一列里分不清谁是谁。
      items = rawTree.slice(0, LIST_LIMIT).map((line) =>
        /\(\d+\s*字节\)\s*$/.test(line)
          ? line.replace(/\s*\(\d+\s*字节\)\s*$/, "")
          : `${line}/`,
      );
      if (rawTree.length > LIST_LIMIT) items.push(`… 还有 ${rawTree.length - LIST_LIMIT} 项`);
      break;
    }
    case "read_file": {
      const path = str(args.path);
      target = path ? `文件 ${path}` : "读取文件";
      add("文件", path);
      const lines = lineCount(output);
      result = lines ? `共 ${lines} 行` : "文件为空";
      break;
    }
    case "search_code": {
      const pattern = str(args.pattern);
      const path = str(args.path);
      target = pattern ? `搜索「${clip(pattern, 60)}」${path ? ` · ${path}` : ""}` : "搜索代码";
      add("搜索内容", pattern);
      add("范围", path || "整个工作区");
      add("匹配方式", str(args.mode) === "regex" ? "正则" : "普通文本");
      const hits = lineCount(output);
      result = hits ? `命中 ${hits} 处` : "没有找到匹配";
      items = listItems(output, 40);
      break;
    }
    case "apply_patch":
    case "write_file":
    case "edit_file": {
      const path = str(args.path);
      target = path ? `文件 ${path}` : "修改文件";
      add("文件", path);
      if (typeof args.patch === "string") {
        const stat = patchStat(args.patch);
        result = `新增 ${stat.added} 行 · 删除 ${stat.removed} 行`;
        add("改动量", `新增 ${stat.added} 行，删除 ${stat.removed} 行`);
      } else if (typeof args.content === "string") {
        result = `写入 ${lineCount(args.content)} 行`;
      }
      break;
    }
    case "run_shell": {
      const command = str(args.command);
      target = command ? `命令 ${clip(command, 90)}` : "执行命令";
      add("命令", command);
      add("超时", str(args.timeout_seconds) ? `${str(args.timeout_seconds)} 秒` : "");
      const lines = lineCount(output);
      result = lines ? `输出 ${lines} 行` : "执行完成，没有输出";
      break;
    }
    case "run_tests": {
      const command = str(args.command);
      target = command ? `命令 ${clip(command, 90)}` : "运行项目测试";
      add("命令", command || "自动识别测试方式");
      result = testStat(output) || (lineCount(output) ? `输出 ${lineCount(output)} 行` : "测试执行完成");
      break;
    }
    case "git": {
      const sub = str(args.subcommand);
      target = sub ? `git ${sub}` : "查看仓库状态";
      add("子命令", sub);
      add("限定文件", str(args.path));
      result = lineCount(output) ? `输出 ${lineCount(output)} 行` : "没有改动";
      break;
    }
    case "web_search": {
      const query = str(args.query);
      target = query ? `搜索「${clip(query, 60)}」` : "联网搜索";
      add("搜索词", query);
      const hits = lineCount(output);
      result = hits ? `返回 ${hits} 行结果` : "没有结果";
      items = listItems(output, 24);
      break;
    }
    case "web_fetch": {
      const url = str(args.url);
      target = url ? `网页 ${clip(url, 90)}` : "读取网页";
      add("网址", url);
      result = output ? `正文 ${output.length} 字` : "网页没有正文";
      break;
    }
    case "update_plan": {
      const steps = Array.isArray(args.steps) ? args.steps : Array.isArray(args.plan) ? args.plan : [];
      target = "更新执行步骤";
      result = steps.length ? `共 ${steps.length} 步` : "已更新";
      break;
    }
    case "memory_search": {
      const query = str(args.query);
      target = query ? `搜索「${clip(query, 60)}」` : "查找记忆";
      add("搜索词", query);
      result = lineCount(output) ? `找到 ${lineCount(output)} 条` : "没有找到";
      break;
    }
    case "spawn_agent":
    case "spawn_agents": {
      const goal = str(args.goal) || str(args.task);
      target = goal ? `任务 ${clip(goal, 90)}` : "分派子任务";
      add("目标", goal);
      add("隔离方式", args.isolate === true ? "独立副本" : "");
      result = "已分派";
      break;
    }
    case "request_user_input": {
      const count = Array.isArray(args.questions) ? args.questions.length : 0;
      target = count > 1 ? `需要你确认 ${count} 件事` : "需要你确认";
      result = "";
      break;
    }
    default: {
      target = anyText(args);
      const lines = lineCount(output);
      result = lines ? `输出 ${lines} 行` : "";
    }
  }

  // 长路径在窄卡片里会撑破布局，统一截断，完整值在展开里看。
  target = clip(target, 110);
  if (bad) result = bad;
  if (!failed && !result) result = "完成";

  return { label, target, result, facts, items };
}
