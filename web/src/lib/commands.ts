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
const CORE_COMMANDS: Command[] = [
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
    keywords: ["connector", "mcp", "连接器", "外部工具", "插件", "settings", "设置项"],
  },
];

/**
 * 设置里的各个分类，也能从 Ctrl+K 直接跳到。
 * 「外部工具与插件」沿用上面的「连接器」命令（同一个 #tools 分类，不能有两个 href）。
 *
 * 设置页有八个分类、几十个面板。原来全局搜索只能到「设置」这一层，
 * 用户想改「审批要不要逐个问」还得自己在一排 tab 里找——搜得到入口、
 * 搜不到东西，等于没搜。keywords 写用户会打的词（键名、中文说法、
 * 他记得的那个报错），不是界面标题的复述。
 *
 * href 带 hash：设置页监听 hashchange，从别的页面跳过来也会落到对应分类。
 * 分类键必须与 settings-view 的 SECTIONS 对齐；那边改名时这里要一起改，
 * 否则命令面板给的是一个静默无效的地址（比没有更糟）。
 */
const SETTING_COMMANDS: Command[] = [
  { id: "settings-models", label: "设置 / 模型与供应商", group: "设置项", href: "/settings#models", keywords: ["模型", "密钥", "api key", "provider", "供应商", "上下文", "推理", "备用模型"] },
  { id: "settings-safety", label: "设置 / 安全与权限", group: "设置项", href: "/settings#safety", keywords: ["审批", "权限", "风险", "白名单", "沙箱", "允许", "拒绝", "高风险", "模式"] },
  { id: "settings-runtime", label: "设置 / 执行与智能体", group: "设置项", href: "/settings#runtime", keywords: ["子代理", "后台", "超时", "并发", "agent", "执行", "工作树", "隔离"] },
  { id: "settings-memory", label: "设置 / 记忆", group: "设置项", href: "/settings#memory", keywords: ["记忆", "记住", "memory", "忘掉", "偏好", "长期"] },
  { id: "settings-channels", label: "设置 / 消息通道", group: "设置项", href: "/settings#channels", keywords: ["通知", "机器人", "企业微信", "webhook", "通道", "推送"] },
  { id: "settings-utilities", label: "设置 / 查看与效率", group: "设置项", href: "/settings#utilities", keywords: ["监控", "证据", "技能", "连接器", "查看", "效率"] },
  { id: "settings-advanced", label: "设置 / 高级与实验", group: "设置项", href: "/settings#advanced", keywords: ["开关", "feature flag", "路由", "实验", "灰度"] },
];

export const COMMANDS: Command[] = [...CORE_COMMANDS, ...SETTING_COMMANDS];

/** 命令面板的标题文案，与快捷键提示绑定，避免两处写死不同的键名。 */
export const PALETTE_PLACEHOLDER = "搜索操作…";
