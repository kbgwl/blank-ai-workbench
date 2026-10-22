import type { Command } from "@/lib/shortcuts";

/** 「新对话」在任意页面都能用：派发这个事件，bot 页面收到后清空当前对话。 */
export const NEW_CHAT_EVENT = "blank:new-chat";

/**
 * 全局命令清单：命令面板与（未来的）快捷入口共用一份。
 *
 * 这里刻意只留两项。产品已经收敛成"一个 bot 页面 + 设置"，把旧页面继续挂在
 * 命令面板里，等于让用户还能进到一个已经不再维护的界面——那比找不到更糟。
 * 旧页面文件暂时保留（回退用），但不再有入口。
 */
export const COMMANDS: Command[] = [
  {
    id: "new-chat",
    label: "新对话",
    group: "操作",
    event: NEW_CHAT_EVENT,
    keywords: ["new", "chat", "新对话", "对话", "清空"],
  },
  {
    id: "go-settings",
    label: "设置",
    group: "前往",
    href: "/settings",
    keywords: ["settings", "模型", "provider", "供应商"],
  },
  {
    id: "go-team",
    label: "Bot 团队",
    group: "前往",
    href: "/team",
    keywords: ["team", "bot", "团队", "群聊", "小白管家"],
  },
];

/** 命令面板的标题文案，与快捷键提示绑定，避免两处写死不同的键名。 */
export const PALETTE_PLACEHOLDER = "搜索操作…";
