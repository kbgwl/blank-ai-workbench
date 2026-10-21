"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { COMMANDS, PALETTE_PLACEHOLDER } from "@/lib/commands";
import { filterCommands, matchShortcut, PALETTE_SHORTCUT, shortcutHint, stepIndex } from "@/lib/shortcuts";

/**
 * 全局命令面板（Ctrl+K / ⌘K）。
 *
 * 对齐 Codex 的一点：任何页面都能用键盘跳到任何地方，不必先回首页。
 * 打开时把焦点放进输入框，关闭时还给原来的元素——这样在长面板里跳转不会丢焦点位置。
 * 键盘操作：方向键移动、回车打开、Esc 关闭；列表项不可聚焦，焦点始终在输入框里。
 *
 * 热键在输入态也生效（写代码时随手 Cmd/Ctrl+K 跳页面是主要用法），
 * 所以这里刻意不做 isEditableTarget 让路；isEditableTarget 只服务于「单键」类快捷键与 Esc。
 */
export function CommandPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  const results = useMemo(() => filterCommands(COMMANDS, query), [query]);
  // 高亮项夹回结果范围内：派生计算，不额外存一份状态，也就没有 effect 里的 setState。
  const highlighted = results.length === 0 ? -1 : Math.min(active, results.length - 1);

  // 全局热键：开关面板。首次打开时把搜索词与高亮项复位，动作放在事件回调里，
  // 而不是 effect body（后者会级联渲染，React 的 lint 规则直接禁止）。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!matchShortcut(event, PALETTE_SHORTCUT)) return;
      event.preventDefault();
      if (open) {
        setOpen(false);
        return;
      }
      setQuery("");
      setActive(0);
      setOpen(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    // 面板挂载后再聚焦，否则 Safari 会把焦点留在 body 上。
    const timer = setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      clearTimeout(timer);
      restoreRef.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  function run(index: number) {
    const command = results[index];
    if (!command) return;
    setOpen(false);
    // 先派发事件、再跳转：动作类命令（新对话）在同一个路由上也能生效。
    if (command.event) window.dispatchEvent(new CustomEvent(command.event));
    if (command.href) router.push(command.href);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActive(stepIndex(highlighted, event.key === "ArrowDown" ? 1 : -1, results.length));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      run(highlighted);
    }
  }

  return (
    <div
      data-overlay-open="command-palette"
      className="fixed inset-0 z-50 flex items-start justify-center bg-text/25 px-4 pt-[12vh] backdrop-blur-[2px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setOpen(false);
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        className="w-full max-w-lg overflow-hidden rounded-xl border border-line-strong bg-surface shadow-xl"
        onKeyDown={onKeyDown}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            // 改查询后结果集变了，高亮必须回到首项，否则回车会打开列表中间那一项。
            setActive(0);
          }}
          placeholder={PALETTE_PLACEHOLDER}
          aria-label={PALETTE_PLACEHOLDER}
          aria-controls="command-palette-list"
          aria-activedescendant={results[highlighted] ? `command-${results[highlighted].id}` : undefined}
          className="w-full border-b border-line bg-transparent px-4 py-3 text-[13px] outline-none placeholder:text-faint"
        />
        <ul id="command-palette-list" role="listbox" className="max-h-72 overflow-y-auto p-1.5">
          {results.length === 0 && <li className="px-3 py-6 text-center text-[12px] text-faint">没有匹配的命令</li>}
          {results.map((command, index) => (
            // option 内不放 button：焦点始终留在输入框（配合 aria-activedescendant），
            // 嵌可聚焦元素会让读屏器把同一项播报两次。点击用 onMouseDown 触发，
            // 避免 mousedown 让输入框失焦后 click 落空。
            <li
              key={command.id}
              id={`command-${command.id}`}
              role="option"
              aria-selected={index === highlighted}
              onMouseEnter={() => setActive(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                run(index);
              }}
              className={`flex cursor-pointer items-center justify-between rounded-md px-3 py-2 text-[12.5px] ${
                index === highlighted ? "bg-surface-2 text-text" : "text-muted"
              }`}
            >
              <span>{command.label}</span>
              <span className="font-mono text-[10px] text-faint">{command.group}</span>
            </li>
          ))}
        </ul>
        <div className="flex items-center justify-between border-t border-line px-3 py-2 text-[10px] text-faint">
          <span>↑↓ 选择 · Enter 打开 · Esc 关闭</span>
          <span className="font-mono">{shortcutHint(PALETTE_SHORTCUT)}</span>
        </div>
      </div>
    </div>
  );
}