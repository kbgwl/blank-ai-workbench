"use client";

import { useState } from "react";
import { HabitList, submitMemoryCandidate, useHabits } from "@/components/habits";
import { PageBody, PageHeader } from "@/components/page";

export default function DailyPage() {
  const { habits, remember, forget } = useHabits();
  const [message, setMessage] = useState("");
  const [notice, setNotice] = useState("");
  async function saveHabit() {
    if (!message.trim()) return;
    remember(message, "用户确认 · 日常记录");
    try {
      await submitMemoryCandidate(message);
      setNotice("已加入本地习惯档案，并提交为待审核 Skill 候选；不会自动启用。");
    } catch {
      setNotice("已加入本地习惯档案；后端不可用，Skill 候选将在连接恢复后再提交。");
    }
  }

  return (
    <PageBody>
      <PageHeader
        title="日常"
        description="想法、待办、偏好、工作上下文都可以先放在这里。临时内容不会自动变成长期记忆。"
      />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <section className="rounded-lg border border-line bg-surface p-5">
          <h2 className="text-[13px] font-medium">跟 Agent 说一句</h2>
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            rows={9}
            placeholder="例如：我喜欢先看结论，再看细节；每次改代码都要先跑最相关的测试。"
            className="mt-3 w-full resize-y rounded-md border border-line bg-canvas p-3 text-[13px] leading-relaxed outline-none placeholder:text-faint focus:border-brand"
          />
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setNotice("已记录为本次日常上下文；当前不会写入长期记忆。")}
              className="rounded-md border border-line px-3 py-1.5 text-[12px] text-muted transition-colors hover:border-line-strong hover:text-text"
            >
              仅用于本次
            </button>
            <button
              type="button"
              onClick={saveHabit}
              className="rounded-md bg-brand px-3 py-1.5 text-[12px] font-medium text-on-brand transition-colors hover:bg-brand/90"
            >
              记住这个习惯
            </button>
          </div>
          {notice && <p className="mt-4 text-[11.5px] text-muted">{notice}</p>}
          <div className="mt-8 border-t border-line pt-4">
            <h3 className="text-[12px] font-medium">学习规则</h3>
            <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted">
              Agent 只学习你明确确认的内容；学习记录保存在当前浏览器本地，不上传服务器。未来生成 Skill
              草案时仍需你审核后启用。
            </p>
          </div>
        </section>
        <aside className="rounded-lg border border-line bg-surface p-4">
          <h2 className="text-[13px] font-medium">已确认的习惯</h2>
          <p className="mt-1 text-[11.5px] leading-relaxed text-muted">这些偏好会作为后续任务的可见上下文。</p>
          <div className="mt-4">
            <HabitList habits={habits} onForget={forget} />
          </div>
        </aside>
      </div>
    </PageBody>
  );
}