"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import { fetchProjects, type Project } from "@/components/projects/api";
import { errorMessage, fetchProjectMemory, type ProjectMemory } from "./api";
import { groupMemory, memoryKindLabel, memoryUsage } from "./memory";

/**
 * 它到底记住了什么。
 *
 * 记忆一直是"只有写、没有看"的：会话里助手像是记得你，但用户查不到记的是哪一句、
 * 什么时候记的、是不是记错了。于是他对这东西只有两种态度——完全信任，或者
 * 干脆关掉。中间那条路（看一眼、发现记歪了、去改）原本不存在。
 *
 * 这一屏只做展示，不给编辑、也不给清空：记忆是助手按判断写进去的，让人在这里
 * 逐条改会变成另一种维护负担；而"一键清空"在没有撤销的情况下太容易误触，
 * 真要清就直说删那个文件，路径就在面板上。
 */
export function MemoryPanel() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState("");
  const [projectsError, setProjectsError] = useState("");
  // 结果与项目绑在一起存：换项目时旧数据立刻失效，不用在 effect 里手动清。
  const [result, setResult] = useState<{ projectId: string; data: ProjectMemory | null; error: string } | null>(
    null,
  );
  const [rawOpen, setRawOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((rows) => {
        if (!alive) return;
        setProjects(rows);
        // 只列有工作区的项目：没有工作区就没有记忆文件，选了也是空。
        const bound = rows.find((item) => item.workspaces.length > 0);
        setProjectId((current) => current || bound?.id || "");
      })
      .catch((err) => {
        if (alive) setProjectsError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    fetchProjectMemory(projectId)
      .then((data) => {
        if (alive) setResult({ projectId, data, error: "" });
      })
      .catch((err) => {
        if (alive) setResult({ projectId, data: null, error: errorMessage(err) });
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const loaded = result && result.projectId === projectId ? result : null;
  const memory = loaded?.data ?? null;
  const usage = useMemo(() => memoryUsage(memory), [memory]);
  const groups = useMemo(() => groupMemory(memory?.entries ?? []), [memory]);
  const bound = (projects ?? []).filter((item) => item.workspaces.length > 0);

  return (
    <div className="flex flex-col gap-4">
      <Panel
        title="长期记忆"
        hint="它记住的关于你、这个项目、以及做事方式的事"
        action={
          bound.length > 1 ? (
            <SelectPopover
              value={projectId}
              onChange={setProjectId}
              options={bound.map((item) => ({ value: item.id, label: item.name }))}
              ariaLabel="选择项目"
            />
          ) : undefined
        }
      >
        <p className={`text-[13px] leading-relaxed ${usage.tone === "warn" ? "text-warn" : "text-faint"}`}>
          {usage.text}
        </p>
        {memory?.path && (
          <p className="mt-1 break-all font-mono text-[11px] text-faint" title="记忆文件位置">
            {memory.path}
          </p>
        )}

        {projectsError && <p className="mt-2 text-[13px] text-danger">项目列表读取失败：{projectsError}</p>}
        {loaded?.error && <p className="mt-2 text-[13px] text-danger">记忆读取失败：{loaded.error}</p>}
        {!loaded && !projectsError && <p className="mt-2 font-mono text-[12px] text-faint">读取记忆…</p>}

        {memory && !memory.exists && (
          <p className="mt-2 text-[13px] leading-relaxed text-muted">
            还没有记过任何事。它在会话里判断出值得长期记住的东西时才会写进来，而且要先在输入框那边允许它记。
          </p>
        )}

        {groups.length > 0 && (
          <div className="mt-3 flex flex-col gap-3">
            {groups.map((group) => (
              <div key={group.section || "untitled"}>
                {/* 段落标题就是"什么时候、因为哪件事记的"。没有它，一堆条目
                    看起来同样重要，用户没法判断哪条已经过时。 */}
                <p className="font-mono text-[11px] text-faint">{group.section || "没写时间"}</p>
                <ul className="mt-1 flex flex-col gap-1">
                  {group.entries.map((entry, index) => (
                    <li key={`${group.section}-${index}`} className="flex items-baseline gap-2 text-[13px]">
                      <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted">
                        {memoryKindLabel(entry)}
                      </span>
                      <span className="min-w-0 flex-1 leading-relaxed text-text">{entry.text}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}

        {memory?.exists && groups.length === 0 && (
          <p className="mt-2 text-[13px] leading-relaxed text-muted">
            文件里没有能按条目读出来的内容（格式和预期不一样）。下面是原文。
          </p>
        )}

        {memory?.exists && (
          <div className="mt-3">
            <button
              type="button"
              onClick={() => setRawOpen((value) => !value)}
              className="font-mono text-[11px] text-faint transition-colors hover:text-text"
            >
              原文 {rawOpen ? "▴" : "▾"}
            </button>
            {rawOpen && (
              <pre className="mt-1.5 max-h-72 overflow-auto rounded-md border border-line bg-surface px-2.5 py-2 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-text/85">
                {memory.text}
              </pre>
            )}
          </div>
        )}
      </Panel>

      <Panel title="记忆怎么用" hint="写、读、以及什么时候会被裁掉">
        <ul className="flex flex-col gap-1.5 text-[13px] leading-relaxed text-muted">
          <li>
            · 只有你在输入框那边允许以后，它才会往这里写。默认关着。
          </li>
          <li>
            · 它记的是判断，不是流水账：用户偏好、做事方式的纠正、项目事实、外部资源指针。
          </li>
          <li>
            · 写满上限之后，最旧的段落会被丢掉。所以这里显示用了多少——快满的时候，
            重要的那几条要你重新说一遍，或者把过时的清掉。
          </li>
          <li>· 每一轮开始前它会先查一遍，跟这次要做的事相关的条目会带进上下文。</li>
        </ul>
      </Panel>
    </div>
  );
}
