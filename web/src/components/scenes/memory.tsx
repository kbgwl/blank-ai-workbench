"use client";

import { useCallback, useState } from "react";

export type Habit = { id: string; text: string; source: string; createdAt: string };
const STORAGE_KEY = "blank.habits";

export async function submitMemoryCandidate(text: string): Promise<void> {
  const response = await fetch("/api/v1/skills/memory-candidates", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text, source: "daily" }),
  });
  if (!response.ok) throw new Error("候选记忆提交失败");
}

export function readHabits(): Habit[] {
  if (typeof window === "undefined") return [];
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]") as unknown;
    return Array.isArray(value) ? value.filter((item): item is Habit => Boolean(item) && typeof item === "object" && typeof (item as Habit).text === "string") : [];
  } catch { return []; }
}

export function useHabits() {
  const [habits, setHabits] = useState<Habit[]>(readHabits);
  const save = useCallback((next: Habit[]) => { setHabits(next); window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); }, []);
  const remember = useCallback((text: string, source = "日常场景") => {
    const value = text.trim();
    if (!value) return;
    save([{ id: crypto.randomUUID(), text: value, source, createdAt: new Date().toISOString() }, ...habits].slice(0, 50));
  }, [habits, save]);
  const forget = useCallback((id: string) => save(habits.filter((habit) => habit.id !== id)), [habits, save]);
  return { habits, remember, forget };
}

export function HabitList({ habits, onForget }: { habits: Habit[]; onForget: (id: string) => void }) {
  if (!habits.length) return <p className="text-[11px] text-faint">还没有确认过的长期习惯。Agent 不会自动保存日常内容。</p>;
  return <div className="space-y-2">{habits.map((habit) => <div key={habit.id} className="flex items-start gap-2 rounded border border-line px-2.5 py-2"><div className="min-w-0 flex-1"><div className="text-[12px] text-text">{habit.text}</div><div className="mt-1 font-mono text-[10px] text-faint">{habit.source}</div></div><button type="button" onClick={() => onForget(habit.id)} className="text-[10px] text-faint hover:text-danger">忘记</button></div>)}</div>;
}
