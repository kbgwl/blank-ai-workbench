"use client";

import { referencesIn, removeReference, shortPath } from "./references";

/**
 * 草稿里已引用的文件，显示成可删的小标签。
 *
 * 存在的理由：引用是写进正文的纯文本，草稿一长就看不出到底引了哪几个文件，
 * 想删还得回到正文里去找那一串 `@路径`。标签把「引了什么」摊在输入框上方，
 * 点叉就直接从正文里摘掉。
 */
export function ReferenceChips({
  draft,
  onChange,
  disabled,
}: {
  draft: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const paths = referencesIn(draft);
  if (paths.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
      {paths.map((path) => (
        <span
          key={path}
          title={path}
          className="inline-flex max-w-[240px] items-center gap-1 rounded-md border border-line bg-surface-2 py-0.5 pl-1.5 pr-1 text-[11px] text-muted"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-3 shrink-0 text-faint" aria-hidden>
            <path d="M6 3h7l5 5v13H6z" />
          </svg>
          <span className="min-w-0 truncate font-mono">{shortPath(path)}</span>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange(removeReference(draft, path))}
            title="从正文里删掉这处引用"
            className="grid size-4 shrink-0 place-items-center rounded text-faint transition-colors hover:bg-surface-3 hover:text-text disabled:opacity-40"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" className="size-2.5" aria-hidden>
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </span>
      ))}
    </div>
  );
}