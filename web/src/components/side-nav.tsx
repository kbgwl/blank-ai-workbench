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
    href: "/tasks",
    label: "对话",
    match: (p) => p.startsWith("/tasks"),
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="size-4">
        <path d="M9 6h11M9 12h11M9 18h11" />
        <path d="m4 5 1.5 1.5L8 4M4 11l1.5 1.5L8 10M4 17l1.5 1.5L8 16" />
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
            className={`group flex items-center gap-2.5 rounded-lg px-3 py-2 text-[14px] transition-colors ${
              active
                ? "bg-surface-2 text-text font-medium"
                : "text-muted hover:bg-surface-2 hover:text-text"
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
