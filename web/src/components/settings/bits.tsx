"use client";

import type { ReactNode } from "react";
import type { Capabilities, TestResult } from "./api";

export function KindBadge({ kind }: { kind: string }) {
  const tone: Record<string, string> = {
    openai_compatible: "border-brand/30 bg-brand/10 text-brand",
    anthropic: "border-warn/30 bg-warn/10 text-warn",
    ollama: "border-accent/30 bg-accent/10 text-accent",
  };
  const label: Record<string, string> = {
    openai_compatible: "OpenAI 兼容",
    anthropic: "Anthropic",
    ollama: "Ollama 本地",
  };
  return (
    <span
      className={`shrink-0 rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${tone[kind] ?? "border-line bg-surface-2 text-muted"}`}
    >
      {label[kind] ?? kind}
    </span>
  );
}

export function Chip({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <span
      className={`rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${
        active ? "border-accent/30 bg-accent/10 text-accent" : "border-line bg-surface-2/60 text-faint line-through"
      }`}
    >
      {children}
    </span>
  );
}

export function CapChips({ caps }: { caps: Capabilities }) {
  return (
    <div className="flex flex-wrap gap-1">
      <Chip active={Boolean(caps.streaming)}>流式</Chip>
      <Chip active={Boolean(caps.tools)}>工具</Chip>
      <Chip active={Boolean(caps.vision)}>视觉</Chip>
      <Chip active={Boolean(caps.structured_output)}>结构化</Chip>
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 text-[12px] text-muted transition-colors hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span
        className={`relative h-4 w-7 shrink-0 rounded-full transition-colors ${
          checked ? "bg-brand/70" : "bg-surface-2 ring-1 ring-line"
        }`}
      >
        <span
          className={`absolute top-0.5 size-3 rounded-full transition-all ${
            checked ? "left-3.5 bg-bg" : "left-0.5 bg-faint"
          }`}
        />
      </span>
      <span className="whitespace-nowrap">{label}</span>
    </button>
  );
}

export function StatusLine({ result }: { result: TestResult }) {
  if (result.ok) {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-md border border-accent/30 bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-accent">
            连接成功
          </span>
          <span className="font-mono text-[10px] text-muted">{result.model ?? "未知模型"}</span>
          <span className="font-mono text-[10px] text-faint">{result.latency_ms ?? 0}ms</span>
          {result.usage && (
            <span className="font-mono text-[10px] text-faint">
              {result.usage.prompt_tokens}↑ / {result.usage.completion_tokens}↓ tokens
            </span>
          )}
        </div>
        {result.capabilities && <CapChips caps={result.capabilities} />}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-md border border-danger/30 bg-danger/10 px-1.5 py-0.5 font-mono text-[10px] text-danger">
          连接失败
        </span>
        <span className="font-mono text-[10px] text-faint">{result.code ?? "unknown"}</span>
      </div>
      <p className="text-[12px] leading-relaxed text-danger/90">{result.message ?? "模型调用失败"}</p>
      {result.capabilities && <CapChips caps={result.capabilities} />}
    </div>
  );
}

export function StatTile({ label, value, tone = "text" }: { label: string; value: string; tone?: "text" | "warn" }) {
  return (
    <div className="rounded-lg border border-line bg-surface/60 px-3 py-2">
      <div className="font-mono text-[10px] tracking-wide text-faint uppercase">{label}</div>
      <div className={`mt-0.5 text-[15px] font-semibold ${tone === "warn" ? "text-warn" : "text-text"}`}>{value}</div>
    </div>
  );
}
