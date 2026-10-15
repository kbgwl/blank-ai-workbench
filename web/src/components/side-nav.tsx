"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

type NavItem = {
  href: string;
  label: string;
  match: (p: string) => boolean;
  icon: React.ReactNode;
};

const items: NavItem[] = [
  {
    href: "/",
    label: "概览",
    match: (p) => p === "/",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <rect x="3" y="3" width="7" height="9" rx="1.5" />
        <rect x="14" y="3" width="7" height="5" rx="1.5" />
        <rect x="14" y="12" width="7" height="9" rx="1.5" />
        <rect x="3" y="16" width="7" height="5" rx="1.5" />
      </svg>
    ),
  },
  {
    href: "/projects",
    label: "项目",
    match: (p) => p.startsWith("/projects"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      </svg>
    ),
  },
  {
    href: "/tasks",
    label: "任务",
    match: (p) => p.startsWith("/tasks"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <path d="M9 6h11M9 12h11M9 18h11" />
        <path d="m4 5 1.5 1.5L8 4M4 11l1.5 1.5L8 10M4 17l1.5 1.5L8 16" />
      </svg>
    ),
  },
  {
    href: "/brainstorm",
    label: "头脑风暴",
    match: (p) => p.startsWith("/brainstorm"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <path d="M9 18h6M10 21h4M8.5 14.5a6 6 0 1 1 7 0c-.9.65-1.5 1.4-1.5 2.5h-4c0-1.1-.6-1.85-1.5-2.5Z" />
        <path d="M12 3v1M4.9 5.9l.7.7M19.1 5.9l-.7.7" />
      </svg>
    ),
  },
  {
    href: "/daily",
    label: "日常",
    match: (p) => p.startsWith("/daily"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <path d="M5 5h14v11H8l-3 3z" /><path d="M8 9h8M8 12h5" />
      </svg>
    ),
  },
  {
    href: "/reverse-lab",
    label: "逆向实验室",
    match: (p) => p.startsWith("/reverse-lab"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <path d="M7 4h10v16H7zM10 8h4M10 12h4M10 16h2" />
      </svg>
    ),
  },
  {
    href: "/features",
    label: "功能",
    match: (p) => p.startsWith("/features"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <rect x="4" y="4" width="6" height="6" rx="1" />
        <rect x="14" y="4" width="6" height="6" rx="1" />
        <rect x="4" y="14" width="6" height="6" rx="1" />
        <rect x="14" y="14" width="6" height="6" rx="1" />
      </svg>
    ),
  },
  {
    href: "/settings",
    label: "设置",
    match: (p) => p.startsWith("/settings"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <circle cx="12" cy="12" r="3" />
        <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8" />
      </svg>
    ),
  },
];

export function SideNav() {
  const pathname = usePathname();
  return (
    <nav className="flex flex-col gap-1 p-3">
      {items.map((item) => {
        const active = item.match(pathname);
        return (
          <Link
            key={item.href}
            href={item.href}
            className={`group flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition-colors ${
              active
                ? "bg-surface-2 text-text font-medium"
                : "text-muted hover:bg-surface-2/60 hover:text-text"
            }`}
          >
            <span className={active ? "text-brand" : "text-faint group-hover:text-muted"}>{item.icon}</span>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
