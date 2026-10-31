import type { Command } from "@/lib/shortcuts";

/** 「新对话」在任意页面都能用：派发这个事件，bot 页面收到后清空当前对话。 */
export const NEW_CHAT_EVENT = "blank:new-chat";

/**
 * 全局命令清单：命令面板与（未来的）快捷入口共用一份。
 *
 * 产品收敛成"一个 bot 页面 + 少数几个查看页"之后，入口只留这些：
 * 新对话、设置、Bot 团队，以及设计稿左栏那四项（工作区 / 证据库 / 技能 / 连接器）。
 * 旧页面（头脑风暴、日常、逆向实验室等）文件还在，但不再有入口——留一个进得去
 * 却没人维护的界面，比找不到更糟。
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
  {
    id: "go-projects",
    label: "工作区",
    group: "前往",
    href: "/projects",
    keywords: ["workspace", "project", "工作区", "项目", "目录"],
  },
  {
    id: "go-evidence",
    label: "证据库",
    group: "前往",
    href: "/evidence",
    keywords: ["evidence", "证据", "验收", "交付", "改动"],
  },
  {
    id: "go-skills",
    label: "技能",
    group: "前往",
    href: "/skills",
    keywords: ["skill", "技能", "偏好", "记忆"],
  },
  {
    id: "go-connectors",
    label: "连接器",
    group: "前往",
    href: "/settings#tools",
    keywords: ["connector", "mcp", "连接器", "外部工具", "插件"],
  },
];

/** 命令面板的标题文案，与快捷键提示绑定，避免两处写死不同的键名。 */
export const PALETTE_PLACEHOLDER = "搜索操作…";
