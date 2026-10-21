"use client";

import { useCallback, useEffect, useState } from "react";

import {
  acceptMemoryCandidate,
  fetchMemoryCandidates,
  rejectMemoryCandidate,
  submitMemoryCandidate,
  type MemoryCandidate,
} from "@/components/skills/api";
import { errorMessage } from "@/lib/http";

export type Habit = { id: string; text: string; source: string; createdAt: string };
const STORAGE_KEY = "blank.habits";

export { submitMemoryCandidate };
export type { MemoryCandidate };

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
  const remember = useCallback((text: string, source = "日常记录") => {
    const value = text.trim();
    if (!value) return;
    save([{ id: crypto.randomUUID(), text: value, source, createdAt: new Date().toISOString() }, ...habits].slice(0, 50));
  }, [habits, save]);
  const forget = useCallback((id: string) => save(habits.filter((habit) => habit.id !== id)), [habits, save]);
  return { habits, remember, forget };
}

/**
 * 待审核记忆候选：只有用户点「确认」才转成 Skill 草案（draft，仍需手动启用）。
 * 后端不可用时保持空列表并回传提示，不阻断日常页其余功能。
 */
export function useMemoryCandidates() {
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(() => {
    return fetchMemoryCandidates()
      .then((rows) => {
        setCandidates(rows);
        setError(null);
      })
      .catch((caught: unknown) => setError(errorMessage(caught)))
      .finally(() => setLoaded(true));
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const review = useCallback(
    async (id: string, action: "accept" | "reject") => {
      setBusyId(id);
      try {
        if (action === "accept") await acceptMemoryCandidate(id);
        else await rejectMemoryCandidate(id);
        setCandidates((current) =>
          current.map((item) =>
            item.id === id
              ? { ...item, status: action === "accept" ? "accepted" : "rejected", reviewed_at: new Date().toISOString() }
              : item,
          ),
        );
        setError(null);
      } catch (caught: unknown) {
        setError(errorMessage(caught));
      } finally {
        setBusyId(null);
      }
    },
    [],
  );

  const pending = candidates.filter((item) => item.status === "pending");
  return { candidates, pending, error, busyId, loaded, reload, review };
}

export function CandidateList({
  pending,
  busyId,
  onReview,
}: {
  pending: MemoryCandidate[];
  busyId: string | null;
  onReview: (id: string, action: "accept" | "reject") => void;
}) {
  if (!pending.length) {
    return <p className="text-[11px] leading-relaxed text-faint">没有待确认的记忆。Agent 不会自行写入长期偏好。</p>;
  }
  return (
    <div className="space-y-2">
      {pending.map((candidate) => (
        <div key={candidate.id} className="rounded border border-line px-2.5 py-2">
          <div className="text-[12px] leading-relaxed text-text">{candidate.text}</div>
          <div className="mt-1 flex items-center gap-2">
            <span className="font-mono text-[10px] text-faint">{candidate.source}</span>
            <button
              type="button"
              disabled={busyId === candidate.id}
              onClick={() => onReview(candidate.id, "accept")}
              className="ml-auto rounded border border-brand/40 px-2 py-0.5 text-[10px] text-brand hover:bg-brand/10 disabled:opacity-40"
            >
              确认为偏好
            </button>
            <button
              type="button"
              disabled={busyId === candidate.id}
              onClick={() => onReview(candidate.id, "reject")}
              className="rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-danger disabled:opacity-40"
            >
              忽略
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

export function HabitList({ habits, onForget }: { habits: Habit[]; onForget: (id: string) => void }) {
  if (!habits.length) return <p className="text-[11px] text-faint">还没有确认过的长期习惯。Agent 不会自动保存日常内容。</p>;
  return <div className="space-y-2">{habits.map((habit) => <div key={habit.id} className="flex items-start gap-2 rounded border border-line px-2.5 py-2"><div className="min-w-0 flex-1"><div className="text-[12px] text-text">{habit.text}</div><div className="mt-1 font-mono text-[10px] text-faint">{habit.source}</div></div><button type="button" onClick={() => onForget(habit.id)} className="text-[10px] text-faint hover:text-danger">忘记</button></div>)}</div>;
}
