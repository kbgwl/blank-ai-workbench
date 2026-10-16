"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { chatProvider } from "@/components/settings/api";
import { createTask, fetchProjects, fetchProviders, fetchTasks, runTask, type Project, type Provider, type Task } from "@/components/tasks/api";
import { QUIET_RULE, useModel, useScene } from "@/components/scenes/scenes";

type Message = { id: string; role: "user" | "agent"; text: string; task?: Task };

export function ChatHome() {
  const { scene } = useScene();
  const { modelId } = useModel();
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [provider, setProvider] = useState<Provider | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [dailyMode, setDailyMode] = useState<"temporary" | "remember">("temporary");

  useEffect(() => {
    void Promise.all([fetchTasks(), fetchProjects(), fetchProviders()]).then(([nextTasks, projects, providers]) => {
      setTasks(nextTasks);
      setProject(projects.find((item) => item.workspaces.length > 0) ?? projects[0] ?? null);
      setProvider(providers.find((item) => item.id === modelId && item.enabled) ?? providers.find((item) => item.enabled) ?? null);
    }).catch(() => setNotice("运行时尚未连接；可以先配置模型和工作目录。"));
  }, [modelId]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setNotice("");
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: "user", text }]);
    setBusy(true);
    try {
      if (project) {
        const task = await createTask(project.id, {
          projectId: project.id,
          providerId: provider?.id,
          title: text.split("\n")[0].slice(0, 80),
          prompt: `${QUIET_RULE}\n${scene.instruction}${scene.id === "daily" ? `\n日常记忆模式：${dailyMode === "remember" ? "用户确认后可记住这个习惯" : "仅用于本次，不写入长期习惯"}` : ""}\n\n用户任务：${text}`,
          acceptanceCriteria: scene.defaultCriteria,
        });
        setTasks((current) => [task, ...current.filter((item) => item.id !== task.id)]);
        setMessages((current) => [...current, { id: crypto.randomUUID(), role: "agent", text: "已开始执行。完成后我会只汇报结果、变更和未验证项。", task }]);
        await runTask(task.id);
      } else if (provider) {
        const response = await chatProvider(provider.id, `${QUIET_RULE}\n${scene.instruction}${scene.id === "daily" ? `\n日常记忆模式：${dailyMode === "remember" ? "用户确认后可记住这个习惯" : "仅用于本次，不写入长期习惯"}` : ""}\n\n用户内容：${text}`);
        setMessages((current) => [...current, { id: crypto.randomUUID(), role: "agent", text: response.text }]);
      } else {
        setNotice("请先配置一个模型；代码任务会自动使用内部工作目录，不需要手动选择项目。 ");
      }
    } catch {
      setNotice("执行失败，请检查模型、后端和内部工作目录配置。 ");
    } finally {
      setBusy(false);
    }
  }

  return <div className="flex h-full min-h-0 bg-bg">
    <aside className="hidden w-64 shrink-0 flex-col border-r border-line bg-surface/15 p-3 md:flex"><button type="button" onClick={() => setMessages([])} className="rounded-md border border-line px-3 py-2 text-left text-[12px] text-muted hover:bg-surface-2 hover:text-text">+ 新对话</button><div className="mt-4 px-2 text-[10px] font-medium text-faint">最近对话</div><div className="mt-2 flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto">{tasks.slice(0, 20).map((task) => <Link key={task.id} href={`/tasks/${task.id}`} className="truncate rounded-md px-2 py-2 text-[12px] text-muted hover:bg-surface-2 hover:text-text">{task.title}</Link>)}{tasks.length === 0 && <p className="px-2 py-3 text-[11px] text-faint">还没有对话</p>}</div><div className="mt-3 border-t border-line pt-3"><div className="flex flex-col gap-0.5"><Link href="/reverse-lab" className="rounded-md px-3 py-2 text-[12px] text-muted hover:bg-surface-2 hover:text-text">逆向实验室</Link><Link href="/settings" className="rounded-md px-3 py-2 text-[12px] text-muted hover:bg-surface-2 hover:text-text">设置</Link></div><div className="mt-2 px-2 text-[10px] text-faint">{scene.name}场景 · {provider?.model ?? "未配置模型"}</div></div></aside>
    <main className="flex min-w-0 flex-1 flex-col"><div className="flex-1 overflow-y-auto"><div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-5 py-10">{messages.length === 0 ? <div className="flex min-h-[48vh] flex-col items-center justify-center text-center"><div className="grid size-10 place-items-center rounded-xl bg-brand/12 ring-1 ring-brand/25"><span className="size-3 rounded-[3px] bg-brand" /></div><h1 className="mt-5 text-lg font-medium">今天要做什么？</h1><p className="mt-2 text-[12px] text-muted">直接说目标，Agent 会在当前场景下完成工作。</p><div className="mt-6 flex flex-wrap justify-center gap-2 text-[11px] text-faint">{scene.id === "daily" ? <><button type="button" onClick={() => setDailyMode("remember")} className={`rounded-full border px-3 py-1.5 ${dailyMode === "remember" ? "border-brand text-text" : "border-line"}`}>记住这个习惯</button><button type="button" onClick={() => setDailyMode("temporary")} className={`rounded-full border px-3 py-1.5 ${dailyMode === "temporary" ? "border-brand text-text" : "border-line"}`}>仅用于本次</button></> : <><span className="rounded-full border border-line px-3 py-1.5">修复一个问题</span><span className="rounded-full border border-line px-3 py-1.5">写一份文档</span><span className="rounded-full border border-line px-3 py-1.5">分析一个目标</span></>}</div></div> : messages.map((message) => <div key={message.id} className={message.role === "user" ? "ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-surface-2 px-4 py-3 text-[13px] leading-relaxed" : "max-w-[90%] px-1 text-[13px] leading-relaxed text-text"}><div className="whitespace-pre-wrap">{message.text}</div>{message.task && <Link href={`/tasks/${message.task.id}`} className="mt-3 inline-block text-[11px] text-brand hover:underline">打开执行详情 →</Link>}</div>)}</div></div><div className="mx-auto w-full max-w-3xl px-5 pb-5"><div className="rounded-xl border border-line bg-surface/60 p-2 shadow-sm"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} rows={3} placeholder={`在${scene.name}场景下告诉 Agent 你要做什么…`} className="w-full resize-none bg-transparent px-2 py-1 text-[13px] leading-relaxed outline-none placeholder:text-faint" /><div className="flex items-center justify-between px-2 pt-2"><span className="text-[10px] text-faint">Enter 发送 · Shift+Enter 换行</span><button type="button" onClick={() => void send()} disabled={busy || !input.trim()} className="rounded-md bg-brand px-3 py-1.5 text-[11px] font-medium text-bg hover:bg-brand/85 disabled:opacity-40">{busy ? "执行中…" : "发送"}</button></div></div>{notice && <p className="mt-2 text-center text-[11px] text-muted">{notice}</p>}</div></main>
  </div>;
}
