"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchTree, type TreeEntry } from "../tasks/api";
import { childrenOf, moveIndex, parentPath, rankEntries, type PickEntry } from "./picker";

/**
 * `@` 引用面板。
 *
 * 浏览与搜索刻意分开：
 * - 没有查询词时是浏览，一次只读一层目录，上下选、回车进目录、← 回上一层；
 * - 有查询词时才把整棵树拉下来排序匹配。
 *
 * 之前是一次取三层再拍平、只留 40 条：深目录里的文件进不去，浅层常用文件又被长路径挤掉。
 * 分开之后浏览省请求，搜索也够全。
 */
export function AtPicker(props: {
  projectId: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const { projectId, onPick, onClose } = props;
  const [dir, setDir] = useState("");
  const [query, setQuery] = useState("");
  // 目录结果按"是哪个目录"存：目录一变，旧结果自然失效、界面回到"读取工作区…"，
  // 不需要在 effect 里先 setState(null) 清一次（那会多渲染一轮）。
  const [dirLoad, setDirLoad] = useState<{ dir: string; entries: TreeEntry[] } | null>(null);
  const [tree, setTree] = useState<TreeEntry[] | null>(null);
  // 光标跟着"列表键"存：换目录或改查询词，键一变就回到第一行。
  // 以前是在 effect 里 setIndex(0)，同样是渲染后再改状态，多一轮也更容易残留旧下标。
  const listKey = `${dir}\u0000${query}`;
  const [cursor, setCursor] = useState({ key: listKey, index: 0 });
  const [error, setError] = useState<string | null>(null);
  const list = useRef<HTMLDivElement>(null);

  const searching = query.trim().length > 0;
  const dirEntries = dirLoad && dirLoad.dir === dir ? dirLoad.entries : null;
  const index = cursor.key === listKey ? cursor.index : 0;
  const setIndex = (next: number | ((current: number) => number)) =>
    setCursor((current) => {
      const base = current.key === listKey ? current.index : 0;
      return { key: listKey, index: typeof next === "function" ? next(base) : next };
    });

  // 用户主动换目录/改词时把上一次的错误清掉。清在这里而不是 effect 里：
  // 那是用户动作的直接结果，同步改状态是正常的。
  const goTo = (next: string) => {
    setError(null);
    setQuery("");
    setDir(next);
  };
  const typeQuery = (next: string) => {
    setError(null);
    setQuery(next);
  };

  // 浏览模式只读当前这一层：depth=1 在深目录里也是一次小请求。
  useEffect(() => {
    if (searching) return;
    let alive = true;
    fetchTree(projectId, dir, 1)
      .then((response) => {
        if (alive) setDirLoad({ dir, entries: response.entries });
      })
      .catch((reason: unknown) => {
        if (alive) setError(reason instanceof Error ? reason.message : "工作区读不到");
      });
    return () => {
      alive = false;
    };
  }, [projectId, dir, searching]);

  // 搜索模式第一次输入时才拉一次四层的树，之后换词不再请求。
  useEffect(() => {
    if (!searching || tree !== null) return;
    let alive = true;
    fetchTree(projectId, "", 4)
      .then((response) => {
        if (alive) setTree(response.entries);
      })
      .catch((reason: unknown) => {
        if (alive) setError(reason instanceof Error ? reason.message : "工作区读不到");
      });
    return () => {
      alive = false;
    };
  }, [projectId, searching, tree]);

  const rows = useMemo<PickEntry[]>(() => {
    if (searching) return tree === null ? [] : rankEntries(tree, query);
    return dirEntries === null ? [] : childrenOf(dirEntries, dir);
  }, [dir, dirEntries, query, searching, tree]);

  // 键盘移动后保持选中行可见；block:"nearest" 在已经可见时不滚动。
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-row="${index}"]`)?.scrollIntoView({ block: "nearest" });
  }, [index, rows.length]);

  function activate(row: PickEntry | undefined) {
    if (!row) return;
    if (row.is_dir) {
      goTo(row.path);
      return;
    }
    onPick(row.path);
  }

  return (
    <div className="absolute bottom-full left-0 z-20 mb-2 w-96 overflow-hidden rounded-xl border border-line bg-surface shadow-lg">
      <div className="flex items-center gap-2 border-b border-line px-2 py-1.5">
        <button
          type="button"
          onClick={() => goTo(parentPath(dir))}
          disabled={!dir && !query}
          title="上一层（←）"
          className="grid size-6 shrink-0 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 hover:text-text disabled:opacity-30"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
            className="size-3.5"
            aria-hidden
          >
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-faint">{dir ? `/${dir}` : "/"}</span>
      </div>

      <div className="border-b border-line p-2">
        <input
          autoFocus
          value={query}
          onChange={(event) => typeQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              onClose();
              return;
            }
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setIndex((current) => moveIndex(current, 1, rows.length));
              return;
            }
            if (event.key === "ArrowUp") {
              event.preventDefault();
              setIndex((current) => moveIndex(current, -1, rows.length));
              return;
            }
            if (event.key === "ArrowLeft" && !query) {
              event.preventDefault();
              goTo(parentPath(dir));
              return;
            }
            if (event.key === "Enter") {
              event.preventDefault();
              activate(rows[index]);
            }
          }}
          placeholder="按名字搜索，或直接浏览目录"
          className="w-full rounded-md bg-surface-2 px-2 py-1.5 text-[12px] text-text placeholder:text-faint focus:outline-none"
        />
      </div>

      <div ref={list} className="max-h-72 overflow-y-auto p-1">
        {error && <p className="px-2 py-3 text-center text-[11.5px] text-danger">{error}</p>}
        {!error && rows.length === 0 && (
          <p className="px-2 py-3 text-center text-[11.5px] text-faint">
            {searching
              ? tree === null
                ? "读取工作区…"
                : "没有匹配的文件"
              : dirEntries === null
                ? "读取工作区…"
                : "这个目录是空的"}
          </p>
        )}
        {rows.map((entry, row) => (
          <button
            key={entry.path}
            type="button"
            data-row={row}
            onMouseEnter={() => setIndex(row)}
            onClick={() => activate(entry)}
            className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
              row === index ? "bg-surface-2" : ""
            }`}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-3.5 shrink-0 text-faint" aria-hidden>
              {entry.is_dir ? <path d="M3 7h6l2 2h10v10H3z" /> : <path d="M6 3h7l5 5v13H6z" />}
            </svg>
            <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-text">{entry.name || entry.path}</span>
          </button>
        ))}
      </div>

      <p className="border-t border-line px-2.5 py-1.5 text-[10.5px] text-faint">
        回车选目录 / 引用文件，← 回上一层，Esc 关闭
      </p>
    </div>
  );
}