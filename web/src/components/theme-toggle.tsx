"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * 主题开关：DOM 的 data-theme 是唯一真源，localStorage 只负责跨启动记忆。
 *
 * 用 useSyncExternalStore 而不是 useState + useEffect：启动前 layout.tsx 里的
 * 引导脚本已经把 data-theme 写上了，effect 里再 setState 既会闪一下，也会被
 * react-hooks/set-state-in-effect 拦。读 DOM 则天然与引导脚本一致。
 */
export const THEME_STORAGE_KEY = "blank.theme";
const THEME_EVENT = "blank:theme-change";

export type ThemeMode = "light" | "dark";

function currentMode(): ThemeMode {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  window.addEventListener(THEME_EVENT, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(THEME_EVENT, onChange);
  };
}

export function applyTheme(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch {
    // 隐私模式下写不了 localStorage，皮肤仍应在本次会话生效。
  }
  window.dispatchEvent(new Event(THEME_EVENT));
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const mode = useSyncExternalStore(subscribe, currentMode, () => "light" as ThemeMode);
  const next: ThemeMode = mode === "dark" ? "light" : "dark";
  const onClick = useCallback(() => applyTheme(next), [next]);

  const icon = mode === "dark" ? (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" className="size-3.5" aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="size-3.5" aria-hidden>
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />
    </svg>
  );

  if (compact) {
    return (
      <button
        type="button"
        onClick={onClick}
        title={mode === "dark" ? "切换到雾白皮肤" : "切换到深渊蓝皮肤"}
        aria-label={mode === "dark" ? "切换到雾白皮肤" : "切换到深渊蓝皮肤"}
        className="grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-text"
      >
        {icon}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[12.5px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
    >
      <span className="size-3.5 shrink-0 text-faint">{icon}</span>
      {mode === "dark" ? "雾白皮肤" : "深渊蓝皮肤"}
    </button>
  );
}
