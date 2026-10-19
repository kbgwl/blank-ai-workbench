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
  apply_patch: "修改文件",
  write_file: "写入文件",
  edit_file: "修改文件",
  run_shell: "执行命令",
  run_tests: "运行测试",
  git: "查看改动",
  update_plan: "更新步骤",
  memory_search: "查找记忆",
  read_notes: "读取笔记",
  write_notes: "记录笔记",
  spawn_agent: "分派子任务",
  spawn_agents: "分派子任务",
  request_user_input: "向你确认",
  web_search: "联网搜索",
  web_fetch: "读取网页",
  tool_search: "查找工具",
};

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
