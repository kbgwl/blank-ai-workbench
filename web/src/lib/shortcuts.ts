/**
 * 命令面板与快捷键的纯逻辑。
 *
 * 放在 lib 里而不是组件里：这几条规则最容易出错（macOS 的 ⌘ 与 Windows 的 Ctrl、
 * 输入框里该不该让路、模糊匹配的排序、首尾环绕），必须能被 `node --test` 直接覆盖，
 * 不依赖 React 渲染环境。
 */

export type Shortcut = {
  /** 小写按键名，例如 "k"、"escape" */
  key: string;
  /** 跨平台的 Ctrl / ⌘ */
  mod?: boolean;
  shift?: boolean;
  alt?: boolean;
};

/** 只依赖 KeyboardEvent 的这几个字段，测试里可以传纯对象。 */
export type KeyLike = {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
};

export type EditableLike = {
  tagName?: string;
  isContentEditable?: boolean;
};

// 显式声明 shift/alt 为 false：Ctrl+Alt+K、Ctrl+Shift+K 这类组合不该被面板吞掉，
// 它们常被输入法或浏览器占用。默认 undefined 会让 matchShortcut 忽略这两个修饰键。
export const PALETTE_SHORTCUT: Shortcut = { key: "k", mod: true, shift: false, alt: false };

export function isMacPlatform(platform?: string): boolean {
  const value = platform ?? (typeof navigator === "undefined" ? "" : navigator.platform);
  return /mac|iphone|ipad|ipod/i.test(value);
}

/** 快捷键提示文案，UI 与按钮 title 共用一套，避免各页面各写一遍。 */
export function shortcutHint(shortcut: Shortcut, mac = isMacPlatform()): string {
  const parts: string[] = [];
  if (shortcut.mod) parts.push(mac ? "⌘" : "Ctrl");
  if (shortcut.shift) parts.push(mac ? "⇧" : "Shift");
  if (shortcut.alt) parts.push(mac ? "⌥" : "Alt");
  parts.push(shortcut.key.toUpperCase());
  return parts.join(mac ? "" : "+");
}

/**
 * 事件是否命中快捷键。
 *
 * mod 只要求 ctrl/meta 至少一个为真，同一份配置在 macOS 与 Windows 都成立；
 * shift / alt 只有显式声明才参与比较，否则 Ctrl+K 会顺带吞掉 Ctrl+Shift+K。
 */
export function matchShortcut(event: KeyLike, shortcut: Shortcut): boolean {
  if (event.key.toLowerCase() !== shortcut.key.toLowerCase()) return false;
  const modPressed = Boolean(event.ctrlKey || event.metaKey);
  if (Boolean(shortcut.mod) !== modPressed) return false;
  if (shortcut.shift !== undefined && Boolean(event.shiftKey) !== shortcut.shift) return false;
  if (shortcut.alt !== undefined && Boolean(event.altKey) !== shortcut.alt) return false;
  return true;
}

/** 焦点在输入框/文本域/下拉/可编辑区时，全局的单键快捷键要让路。 */
export function isEditableTarget(target: EditableLike | null | undefined): boolean {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = (target.tagName ?? "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select";
}

export type Command = {
  id: string;
  label: string;
  /** 分组，面板里按它分组展示 */
  group: string;
  /** 跳转目标；纯动作命令可以不填 */
  href?: string;
  /**
   * 纯动作命令：选中后往 window 上派发这个事件名。
   *
   * 同一路由上的动作（比如"新对话"就是回到首页重新开始）不能靠 href 表达 ——
   * `router.push("/")` 在首页时什么都不会发生，用户会以为命令坏了。
   */
  event?: string;
  /** 额外关键词，用于英文别名命中，例如「任务 tasks」 */
  keywords?: string[];
};

/**
 * 命令匹配打分。
 *
 * 规则刻意做得很小：大小写不敏感的子串命中，label 命中排 2，分组与别名命中排 1，
 * 不命中返回 -1。空查询全部命中，这样「打开面板先看到全量」是预期行为。
 */
export function scoreCommand(command: Command, query: string): number {
  const needle = query.trim().toLowerCase();
  if (!needle) return 0;
  if (command.label.toLowerCase().includes(needle)) return 2;
  if (command.group.toLowerCase().includes(needle)) return 1;
  if ((command.keywords ?? []).some((word) => word.toLowerCase().includes(needle))) return 1;
  return -1;
}

export function filterCommands(commands: Command[], query: string): Command[] {
  return commands
    .map((command, index) => ({ command, index, score: scoreCommand(command, query) }))
    .filter((item) => item.score >= 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((item) => item.command);
}

/** 上下键在列表里环绕移动；空列表恒为 -1，避免面板打开时把索引弄成 0。 */
export function stepIndex(current: number, delta: number, total: number): number {
  if (total <= 0) return -1;
  if (current < 0) return delta > 0 ? 0 : total - 1;
  return (((current + delta) % total) + total) % total;
}

/**
 * Esc 该不该由全局处理（返回工作台）。
 *
 * 三条否决：不是 Esc、已被更内层消费（defaultPrevented）、焦点在输入态或
 * 有浮层打开。抽成纯函数是为了让「Esc 冲突」这条最容易回归的规则有测试兜着，
 * 而不是散在组件 effect 里靠肉眼保证。
 */
export function shouldHandleEscape(options: {
  key: string;
  defaultPrevented: boolean;
  inEditable: boolean;
  overlayOpen: boolean;
}): boolean {
  if (options.key !== "Escape" || options.defaultPrevented) return false;
  return !options.inEditable && !options.overlayOpen;
}