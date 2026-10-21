/**
 * 草稿里的 `@路径` 引用。
 *
 * 引用只是文本，没有单独的附件状态——这一层的取舍是刻意的：后端的 prompt 就是一段
 * 字符串，把它拆成「结构化附件」会让发送路径多一份需要跟后端对齐的数据，
 * 而模型真正读到的还是这句话。这里只负责把已经写进文本的引用识别出来，
 * 好让界面上能显示成小标签、能一键删掉。
 */

import { normalizePath } from "./picker";

/**
 * 从文本里找出所有 `@路径`。
 *
 * 规则：`@` 必须位于开头、空白或标点之后——邮箱 `a@b.com` 里的 `@` 不算引用。
 * 路径到空白为止，允许里面带 `/`、`.`、`-`、`_` 和反斜杠。
 */
const REFERENCE = /(^|[\s（(，,、])@([^\s@]+)/g;

export function referencesIn(draft: string): string[] {
  const found: string[] = [];
  for (const match of draft.matchAll(REFERENCE)) {
    const path = normalizePath(match[2] ?? "");
    if (path && !found.includes(path)) found.push(path);
  }
  return found;
}

/**
 * 删掉某一处引用。
 *
 * 只删第一处命中连同它后面的那个空格：同一个文件在草稿里被提到两次时，
 * 用户点哪个标签就删哪一处，另一处保持原样。
 */
export function removeReference(draft: string, path: string): string {
  const target = normalizePath(path);
  if (!target) return draft;
  let cut = -1;
  let length = 0;
  for (const match of draft.matchAll(REFERENCE)) {
    if (normalizePath(match[2] ?? "") !== target) continue;
    cut = (match.index ?? 0) + (match[1] ?? "").length;
    length = match[0].length - (match[1] ?? "").length;
    break;
  }
  if (cut < 0) return draft;
  const after = draft.slice(cut + length);
  const trimmed = after.startsWith(" ") ? after.slice(1) : after;
  return `${draft.slice(0, cut)}${trimmed}`;
}

/** 引用太长的路径时列表里只显示尾部两段，中间用省略号接上。 */
export function shortPath(path: string, keep = 2): string {
  const parts = normalizePath(path).split("/").filter(Boolean);
  if (parts.length <= keep) return parts.join("/");
  return `…/${parts.slice(-keep).join("/")}`;
}