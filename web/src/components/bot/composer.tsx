"use client";

import { useEffect, useRef, useState } from "react";
import { fetchTree, type Project, type Provider, type TreeEntry } from "../tasks/api";

function AtPicker({
  projectId,
  onPick,
  onClose,
}: {
  projectId: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<TreeEntry[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchTree(projectId, "", 3)
      .then((tree) => {
        if (alive) setEntries(tree.entries);
      })
      .catch((reason: unknown) => {
        if (alive) setError(reason instanceof Error ? reason.message : "工作区读不到");
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const rows = (entries ?? [])
    .filter((entry) => !entry.is_dir)
    .filter((entry) => (query ? entry.path.toLowerCase().includes(query.toLowerCase()) : true))
    .slice(0, 40);

  return (
    <div className="absolute bottom-full left-0 z-20 mb-2 w-80 overflow-hidden rounded-xl border border-line bg-surface shadow-lg">
      <div className="border-b border-line p-2">
        <input
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") onClose();
            if (event.key === "Enter" && rows[0]) onPick(rows[0].path);
          }}
          placeholder="按路径过滤，回车选第一个"
          className="w-full rounded-md bg-surface-2 px-2 py-1.5 text-[12px] text-text placeholder:text-faint focus:outline-none"
        />
      </div>
      <div className="max-h-64 overflow-y-auto p-1">
        {entries === null && !error && <p className="px-2 py-3 text-center text-[11.5px] text-faint">读取工作区…</p>}
        {error && <p className="px-2 py-3 text-center text-[11.5px] text-danger">{error}</p>}
        {entries !== null && rows.length === 0 && (
          <p className="px-2 py-3 text-center text-[11.5px] text-faint">没有匹配的文件</p>
        )}
        {rows.map((entry) => (
          <button
            key={entry.path}
            type="button"
            onClick={() => onPick(entry.path)}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-3.5 shrink-0 text-faint" aria-hidden>
              <path d="M6 3h7l5 5v13H6z" />
            </svg>
            <span className="min-w-0 truncate font-mono text-[11.5px] text-text">{entry.path}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export type ComposerProps = {
  projects: Project[];
  providers: Provider[];
  projectId: string;
  onProjectChange: (id: string) => void;
  providerId: string;
  onProviderChange: (id: string) => void;
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  /** 运行中按回车走插话，而不是再开一轮。 */
  onSteer: () => void;
  onStop: () => void;
  running: boolean;
  /** 发送失败等就地提示。 */
  error: string | null;
  /** 中性提示，比如"已排队"。 */
  hint: string | null;
  disabled?: boolean;
};

export function Composer(props: ComposerProps) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const [picking, setPicking] = useState(false);

  // 自适应高度：起点就是三行，最多长到九行，再多就内部滚动。
  // 一行的输入框放在对话区底部，四周全是留白，看着像"没做完"；
  // 三行既能把常见的一句话摊开，也给了粘贴多行时的余地。
  //
  // 空内容时不写内联高度，交给 `rows={3}` 自己撑开：量 scrollHeight 会漏掉
  // 字体加载前的行高，写死一个偏小的高度会把占位文案截掉半行。
  useEffect(() => {
    const node = textarea.current;
    if (!node) return;
    if (!props.draft) {
      node.style.height = "";
      return;
    }
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 9 * 26 + 28)}px`;
  }, [props.draft]);

  const canSend = props.draft.trim().length > 0 && !props.disabled;

  return (
    <div className="border-t border-line bg-bg/95 px-4 pt-3 pb-3.5 backdrop-blur">
      <div className="mx-auto w-full max-w-3xl">
        {props.error && (
          <p className="mb-2 rounded-lg border border-danger/25 bg-danger/[0.04] px-2.5 py-1.5 text-[11.5px] text-danger">
            {props.error}
          </p>
        )}
        {props.hint && (
          <p className="mb-2 rounded-lg border border-line bg-surface-2/60 px-2.5 py-1.5 text-[11.5px] text-muted">
            {props.hint}
          </p>
        )}

        <div className="relative rounded-2xl border border-line-strong bg-surface shadow-sm transition-colors focus-within:border-brand/50">
          <textarea
            ref={textarea}
            value={props.draft}
            onChange={(event) => props.onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (!canSend) return;
                // 跑着的时候按回车是"插一句话"，不是"再开一轮"：同一轮里改方向，
                // 比等它跑完再纠正要省一次重复执行。
                if (props.running) props.onSteer();
                else props.onSend();
              }
            }}
            rows={3}
            placeholder={
              props.disabled
                ? "先选一个工作区"
                : props.running
                  ? "它正在做，可以插一句话调整方向…（Enter 发送）"
                  : "说点什么，或者直接让它去改代码…（Enter 发送，Shift+Enter 换行）"
            }
            className="block max-h-[262px] min-h-[88px] w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-[15px] leading-[1.75] text-text placeholder:text-faint focus:outline-none"
          />

          <div className="flex items-center gap-1.5 px-2.5 pt-1 pb-2.5">
            <button
              type="button"
              onClick={() => setPicking((value) => !value)}
              title="引用工作区里的文件"
              className="grid size-8 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-2 hover:text-text"
            >
              <span className="font-mono text-[14px]">@</span>
            </button>

            {props.projects.length > 1 && (
              <select
                value={props.projectId}
                onChange={(event) => props.onProjectChange(event.target.value)}
                title="工作区"
                className="max-w-[170px] truncate rounded-lg bg-transparent px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 focus:outline-none"
              >
                {props.projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            )}

            <select
              value={props.providerId}
              onChange={(event) => props.onProviderChange(event.target.value)}
              title="用哪个模型"
              className="max-w-[220px] truncate rounded-lg bg-transparent px-2 py-1.5 text-[12.5px] text-muted transition-colors hover:bg-surface-2 focus:outline-none"
            >
              <option value="">默认模型</option>
              {props.providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                  {provider.model ? ` · ${provider.model}` : ""}
                </option>
              ))}
            </select>

            <div className="ml-auto flex items-center gap-2">
              {props.running && (
                <button
                  type="button"
                  onClick={props.onStop}
                  className="grid size-9 place-items-center rounded-full bg-surface-3 text-text transition-colors hover:bg-line-strong"
                  title="停止这一轮"
                >
                  <span className="size-2.5 rounded-[2px] bg-current" />
                </button>
              )}
              <button
                type="button"
                onClick={props.running ? props.onSteer : props.onSend}
                disabled={!canSend}
                title={props.running ? "插一句话（从下一次模型调用开始生效）" : "发送（Enter）"}
                className="grid size-9 place-items-center rounded-full bg-text text-bg transition-opacity disabled:opacity-25"
              >
                {props.running ? (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="size-[18px]">
                    <path d="M4 12h16M14 6l6 6-6 6" />
                  </svg>
                ) : (
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.1} strokeLinecap="round" strokeLinejoin="round" className="size-[18px]">
                    <path d="M12 19V5M6 11l6-6 6 6" />
                  </svg>
                )}
              </button>
            </div>
          </div>

          {picking && props.projectId && (
            <AtPicker
              projectId={props.projectId}
              onClose={() => setPicking(false)}
              onPick={(path) => {
                props.onDraftChange(`${props.draft}${props.draft && !props.draft.endsWith(" ") ? " " : ""}@${path} `);
                setPicking(false);
                textarea.current?.focus();
              }}
            />
          )}
        </div>

        <p className="mt-2 text-center text-[11px] text-faint">
          它会读你的代码、改文件、跑命令；高风险操作会先问你。
        </p>
      </div>
    </div>
  );
}
