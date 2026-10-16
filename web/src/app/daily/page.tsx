"use client";

import { useState } from "react";
import { HabitList, submitMemoryCandidate, useHabits } from "@/components/scenes/memory";
import { useScene } from "@/components/scenes/scenes";

export default function DailyPage() {
  const { scene } = useScene();
  const { habits, remember, forget } = useHabits();
  const [message, setMessage] = useState("");
  const [notice, setNotice] = useState("");
  async function saveHabit() {
    if (!message.trim()) return;
    remember(message, "用户确认 · 日常场景");
    try {
      await submitMemoryCandidate(message);
      setNotice("已加入本地习惯档案，并提交为待审核 Skill 候选；不会自动启用。");
    } catch {
      setNotice("已加入本地习惯档案；后端不可用，Skill 候选将在连接恢复后再提交。");
    }
  }
  return <div className="mx-auto grid w-full max-w-6xl gap-4 p-6 xl:grid-cols-[1fr_22rem]">
    <main className="rounded-lg border border-line bg-surface/20 p-5"><div className="font-mono text-[10px] text-faint">DAILY / {scene.name.toUpperCase()}</div><h1 className="mt-2 text-xl font-semibold tracking-tight">随手告诉 Agent</h1><p className="mt-2 max-w-2xl text-[13px] leading-relaxed text-muted">想法、待办、偏好、工作上下文都可以先放在这里。临时内容不会自动变成长期记忆。</p><textarea value={message} onChange={(event) => setMessage(event.target.value)} rows={9} placeholder="例如：我喜欢先看结论，再看细节；每次改代码都要先跑最相关的测试。" className="mt-6 w-full resize-y rounded-md border border-line bg-transparent p-3 text-[13px] leading-relaxed outline-none placeholder:text-faint focus:border-brand" /><div className="mt-4 flex flex-wrap gap-2"><button type="button" onClick={() => setNotice("已记录为本次日常上下文；当前不会写入长期记忆。 ")} className="rounded-md border border-line px-3 py-2 text-[11px] text-muted hover:border-brand hover:text-text">仅用于本次</button><button type="button" onClick={saveHabit} className="rounded-md bg-brand px-3 py-2 text-[11px] font-medium text-bg hover:bg-brand/85">记住这个习惯</button></div>{notice && <p className="mt-4 text-[11px] text-muted">{notice}</p>}<div className="mt-8 border-t border-line pt-4"><div className="font-mono text-[10px] text-faint">学习规则</div><p className="mt-2 text-[11px] leading-relaxed text-muted">Agent 只学习你明确确认的内容；学习记录保存在当前浏览器本地，不上传服务器。未来生成 Skill 草案时仍需你审核后启用。</p></div></main>
    <aside className="rounded-lg border border-line bg-surface/20 p-4"><div className="font-mono text-[10px] text-faint">LOCAL MEMORY</div><h2 className="mt-2 text-[15px] font-semibold">已确认的习惯</h2><p className="mt-2 text-[11px] leading-relaxed text-muted">这些偏好会作为后续任务的可见上下文。</p><div className="mt-5"><HabitList habits={habits} onForget={forget} /></div></aside>
  </div>;
}
