"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { errorMessage } from "@/lib/http";
import { fetchDirectories, type DirectoryListing } from "./fs-api";

type Props = {
  initialPath?: string;
  onClose: () => void;
  onPick: (path: string) => void;
};

const ABSOLUTE = /^(?:[A-Za-z]:[\\/]|\/)/;

function FolderIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4 shrink-0" aria-hidden>
      <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l1.5 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-10Z" />
    </svg>
  );
}

/**
 * 本机目录选择框。
 *
 * 浏览器拿不到本机绝对路径，原生目录框只在桌面壳里有；这里走后端列目录，
 * 让网页环境也能选择/更换目录。只列目录，不列文件。
 */
export function DirectoryPicker({ initialPath = "", onClose, onPick }: Props) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const alive = useRef(true);
  // 连点目录时只认最后一次请求：慢请求晚回来不能把新目录的清单顶掉。
  const seq = useRef(0);

  const load = useCallback(async (path: string, fallback: boolean) => {
    const id = ++seq.current;
    setLoading(true);
    setError("");
    try {
      const data = await fetchDirectories(path);
      if (alive.current && id === seq.current) setListing(data);
    } catch (err) {
      if (!alive.current || id !== seq.current) return;
      if (!fallback) {
        setError(errorMessage(err));
        return;
      }
      // 保存的目录可能已被删：退回根目录，让用户重新选，而不是卡在错误上。
      try {
        const data = await fetchDirectories("");
        if (alive.current && id === seq.current) setListing(data);
      } catch (rootError) {
        if (alive.current && id === seq.current) setError(errorMessage(rootError));
      }
    } finally {
      if (alive.current && id === seq.current) setLoading(false);
    }
  }, []);

  const go = useCallback(
    (path: string) => {
      void load(path, false);
    },
    [load],
  );

  useEffect(() => {
    alive.current = true;
    const id = ++seq.current;
    const start = ABSOLUTE.test(initialPath) ? initialPath : "";
    const settle = () => {
      if (alive.current && id === seq.current) setLoading(false);
    };
    fetchDirectories(start)
      .then((data) => {
        if (alive.current && id === seq.current) setListing(data);
      })
      .catch((err: unknown) => {
        if (!alive.current || id !== seq.current) return;
        if (!start) {
          setError(errorMessage(err));
          return;
        }
        // 保存的目录可能已被删：退回根目录，让用户重新选，而不是卡在错误上。
        return fetchDirectories("")
          .then((data) => {
            if (alive.current && id === seq.current) setListing(data);
          })
          .catch((rootError: unknown) => {
            if (alive.current && id === seq.current) setError(errorMessage(rootError));
          });
      })
      .finally(settle);
    return () => {
      alive.current = false;
    };
  }, [initialPath]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const path = listing?.path ?? "";
  const atRoot = Boolean(listing && !listing.path);
  const entries = listing?.entries ?? [];

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="选择目录"
        className="flex max-h-[80vh] w-full max-w-xl flex-col overflow-hidden rounded-xl border border-line bg-surface shadow-[var(--shadow-lg)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-line px-4 py-3">
          <span className="grid size-8 place-items-center rounded-lg bg-surface-2 text-muted">
            <FolderIcon />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-[14px] font-medium">选择目录</h3>
            <p className="mt-0.5 truncate font-mono text-[11px] text-faint" title={path || "根目录"}>
              {path || "根目录"}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="grid size-7 place-items-center rounded-md text-faint hover:bg-surface-2 hover:text-text"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="size-4" aria-hidden>
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center gap-2 border-b border-line px-4 py-2">
            <button
              type="button"
              disabled={!listing?.parent}
              onClick={() => listing?.parent && void go(listing.parent)}
              className="border border-line px-2 py-1 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text disabled:opacity-40"
            >
              上一级
            </button>
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint">
              {loading ? "读取中…" : atRoot ? "选一个盘符或目录" : "点目录进入，或直接选当前目录"}
            </span>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {error && (
              <p className="mx-1 mb-2 border border-danger/25 bg-danger/[0.04] px-2.5 py-2 text-[12px] text-danger">
                {error}
              </p>
            )}
            {atRoot ? (
              <div className="flex flex-col gap-0.5">
                {listing?.roots.map((root) => (
                  <button
                    key={root.path}
                    type="button"
                    onClick={() => void go(root.path)}
                    className="flex items-center gap-2 rounded-md px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
                  >
                    <FolderIcon />
                    <span className="truncate font-mono">{root.name}</span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="flex flex-col gap-0.5">
                {!loading && entries.length === 0 && !error && (
                  <p className="px-2.5 py-3 text-[12px] text-faint">这个目录下没有子目录。</p>
                )}
                {entries.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    data-dir-entry
                    onClick={() => void go(entry.path)}
                    title={entry.path}
                    className="flex items-center gap-2 rounded-md px-2.5 py-2 text-left text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-text"
                  >
                    <FolderIcon />
                    <span className="truncate">{entry.name}</span>
                  </button>
                ))}
                {listing?.truncated && (
                  <p className="px-2.5 py-2 text-[11px] text-faint">目录过多，只显示前 500 个。</p>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-line px-4 py-3">
          <p className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint" title={path}>
            {path ? `将绑定：${path}` : "还没有进入任何目录"}
          </p>
          <button
            type="button"
            disabled={!path || loading}
            onClick={() => path && onPick(path)}
            className="shrink-0 rounded-md bg-brand px-3 py-1.5 text-[12px] font-medium text-on-brand transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            选择这个目录
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
