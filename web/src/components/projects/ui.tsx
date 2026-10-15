"use client";

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

type ButtonVariant = "primary" | "ghost" | "danger";

const buttonBase =
  "inline-flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";

const buttonVariants: Record<ButtonVariant, string> = {
  primary: "bg-brand text-bg hover:bg-brand/85",
  ghost: "bg-surface-2 text-muted ring-1 ring-line hover:text-text hover:ring-line-strong",
  danger: "bg-danger/10 text-danger ring-1 ring-danger/30 hover:bg-danger/20",
};

export function Button({
  variant = "ghost",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; className?: string }) {
  return (
    <button type="button" className={`${buttonBase} ${buttonVariants[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
}

export function IconButton({
  label,
  tone = "muted",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; tone?: "muted" | "danger"; className?: string }) {
  const toneClass = tone === "danger" ? "hover:text-danger" : "hover:text-brand";
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={`grid size-6 shrink-0 place-items-center rounded-md text-faint transition-colors hover:bg-surface-2 ${toneClass} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-[13px] text-text placeholder:text-faint focus:border-brand/60 focus:outline-none ${className}`}
      {...props}
    />
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-1 flex-col gap-1.5">
      <span className="font-mono text-[10px] tracking-wide text-faint uppercase">{label}</span>
      {children}
    </label>
  );
}

export function Notice({
  tone = "error",
  children,
}: {
  tone?: "error" | "warn" | "muted";
  children: ReactNode;
}) {
  const toneClass =
    tone === "error"
      ? "border-danger/30 bg-danger/[0.06] text-danger"
      : tone === "warn"
        ? "border-warn/30 bg-warn/[0.06] text-warn"
        : "border-line bg-surface-2/60 text-muted";
  return <div className={`rounded-lg border px-3 py-2 text-[12px] leading-relaxed ${toneClass}`}>{children}</div>;
}

export function StateLine({ children }: { children: ReactNode }) {
  return <div className="px-1 py-6 text-center font-mono text-[11px] text-faint">{children}</div>;
}

export function ActiveDot({ active }: { active: boolean }) {
  return <span className={`size-1.5 shrink-0 rounded-full ${active ? "bg-accent" : "bg-faint"}`} />;
}

export function Spinner() {
  return (
    <svg viewBox="0 0 24 24" className="size-3.5 animate-spin" fill="none" stroke="currentColor" strokeWidth={2}>
      <path d="M12 3a9 9 0 1 0 9 9" />
    </svg>
  );
}

export function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`size-3 shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

export function FolderIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="size-3.5 shrink-0 text-brand/70"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
    >
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </svg>
  );
}

export function FileIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="size-3.5 shrink-0 text-faint"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
    >
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </svg>
  );
}

export function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.6}>
      <path d="M4 20h4l10-10-4-4L4 16z" />
      <path d="m13 6 4 4" />
    </svg>
  );
}

export function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.6}>
      <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" />
    </svg>
  );
}
