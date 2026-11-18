"use client";

import type { SlashCommand } from "@/lib/slash-commands";

/**
 * 斜杠命令面板。浮在输入框上方，靠输入框的键盘事件驱动（焦点始终留在 textarea，
 * 不抢焦点——否则用户打一半字再按 ↑↓ 会跳到面板里，回不到正文）。
 */
export function SlashMenu({
  items,
  index,
  onPick,
}: {
  items: SlashCommand[];
  index: number;
  onPick: (command: SlashCommand) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="absolute bottom-full left-2 right-2 z-30 mb-1 rounded-xl border border-line bg-surface px-3 py-2 text-[12px] text-faint shadow-[var(--shadow-lg)]">
        没有匹配的命令。输入 / 看全部。
      </div>
    );
  }

  return (
    <div
      role="listbox"
      aria-label="命令"
      className="absolute bottom-full left-2 right-2 z-30 mb-1 overflow-hidden rounded-xl border border-line bg-surface p-1 shadow-[var(--shadow-lg)]"
    >
      {items.map((command, position) => {
        const active = position === index;
        return (
          <button
            key={command.id}
            type="button"
            role="option"
            aria-selected={active}
            // 用 mouseDown 而不是 click：textarea 失焦会先触发 blur，面板可能已经关掉。
            onMouseDown={(event) => {
              event.preventDefault();
              onPick(command);
            }}
            className={`flex w-full items-baseline gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors ${
              active ? "bg-surface-3" : "hover:bg-surface-2"
            }`}
          >
            <span className={`shrink-0 font-mono text-[12px] ${active ? "text-text" : "text-muted"}`}>{command.label}</span>
            <span className="min-w-0 flex-1 truncate text-[12px] text-faint">{command.hint}</span>
          </button>
        );
      })}
    </div>
  );
}
