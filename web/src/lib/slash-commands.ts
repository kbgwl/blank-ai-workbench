/**
 * 斜杠命令清单。
 *
 * Claude Code 在输入框里打 `/` 会列出可用命令；我们之前完全没有这一层，
 * 用户只能记住"设置在哪、技能在哪"。这里只放**本地能立刻完成**的动作，
 * 不做需要后端配合的命令（比如压缩上下文），避免点了没反应。
 *
 * 纯数据 + 纯函数，方便单测，也方便技能页复用同一份清单。
 */

export type SlashAction = "new-chat" | "clear" | "plan-mode";

export type SlashCommand = {
  id: string;
  /** 显示成 `/新对话` 这种形式；也接受英文别名。 */
  label: string;
  hint: string;
  /** 二选一：本地动作，或者跳一个页面。 */
  action?: SlashAction;
  href?: string;
  keywords?: string[];
};

export const SLASH_COMMANDS: SlashCommand[] = [
  { id: "new", label: "/新对话", hint: "开一条新对话（当前这条会留在列表里）", action: "new-chat", keywords: ["new", "清空对话"] },
  { id: "plan", label: "/计划", hint: "切到计划模式：先出方案，你确认了再动手", action: "plan-mode", keywords: ["plan", "方案"] },
  { id: "clear", label: "/清空", hint: "只清掉输入框里的草稿，不动对话", action: "clear", keywords: ["clear", "清空输入"] },
  { id: "settings", label: "/设置", hint: "模型、权限、工具与用量", href: "/settings", keywords: ["settings", "模型"] },
  { id: "skills", label: "/技能", hint: "它从你的习惯里学到的做法", href: "/skills", keywords: ["skill", "记忆"] },
  { id: "evidence", label: "/证据", hint: "每次交付的验收条件与改动", href: "/evidence", keywords: ["evidence", "验收"] },
];

export type SlashMatch = { query: string; items: SlashCommand[] };

/**
 * 输入框内容是否处于"斜杠命令"状态。
 *
 * 只认**开头是 `/` 且还没有空格**的形式：`/设置` 是命令，`/usr/local` 不是，
 * `/设置 帮我改` 也不是——打空格就说明用户要写正文了，面板该让开。
 */
export function matchSlash(input: string): SlashMatch | null {
  const text = input ?? "";
  if (!text.startsWith("/")) return null;
  if (/\s/.test(text)) return null;
  const query = text.slice(1).toLowerCase();
  // `/usr/local`、`/api/v1` 是路径不是命令：命令词里不该再出现斜杠。
  if (query.includes("/")) return null;
  const items = SLASH_COMMANDS.filter((command) => {
    if (!query) return true;
    const label = command.label.slice(1).toLowerCase();
    if (label.startsWith(query)) return true;
    return (command.keywords ?? []).some((word) => word.toLowerCase().includes(query));
  });
  return { query, items };
}
