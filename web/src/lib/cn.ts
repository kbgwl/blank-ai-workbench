import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** 合并 Tailwind 类名，自动处理冲突（tailwind-merge）并过滤假值（clsx） */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}