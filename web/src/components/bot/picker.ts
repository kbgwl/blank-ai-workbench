/**
 * `@` 选择器的纯逻辑：目录进出、过滤、排序、键盘移动。
 *
 * 抽出来是为了能测：这些规则写错时界面不会报错，只会「选到不想要的文件」，
 * 手点几下很难发现。
 */

/** 与 `../tasks/api` 的 `TreeEntry` 同形的子集，避免这里反向依赖数据层。 */
export type PickEntry = { name: string; path: string; is_dir: boolean };

/** 一次最多显示多少行：够翻，又不至于把长列表整个塞进 DOM。 */
export const PICK_LIMIT = 60;

/** 统一分隔符：Windows 上后端返回反斜杠，比较路径前必须先归一。 */
export function normalizePath(value: string): string {
  return value.replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/+$/, "");
}

export function joinPath(dir: string, name: string): string {
  const base = normalizePath(dir);
  return base ? `${base}/${name}` : name;
}

/** 上一层目录；已经在根目录时返回空串。 */
export function parentPath(dir: string): string {
  const path = normalizePath(dir);
  const cut = path.lastIndexOf("/");
  return cut <= 0 ? "" : path.slice(0, cut);
}

/** 路径所在的目录。 */
export function dirOf(path: string): string {
  const current = normalizePath(path);
  const cut = current.lastIndexOf("/");
  return cut <= 0 ? "" : current.slice(0, cut);
}

function nameOf(entry: PickEntry): string {
  return (entry.name || normalizePath(entry.path).split("/").pop() || "").toLowerCase();
}

/**
 * 当前目录下的直接子项，目录排在文件前面。
 *
 * 组内保持后端返回的顺序，这里不再按名字排一次：二次排序会让「刚才第 3 行」
 * 变成「现在第 9 行」，连续按方向键就会点错。
 */
export function childrenOf(entries: PickEntry[], dir: string): PickEntry[] {
  const target = normalizePath(dir);
  const dirs: PickEntry[] = [];
  const files: PickEntry[] = [];
  for (const entry of entries) {
    if (dirOf(entry.path) !== target) continue;
    if (entry.is_dir) dirs.push(entry);
    else files.push(entry);
  }
  return [...dirs, ...files];
}

/**
 * 按查询词给整棵树的候选排序。
 *
 * 档位：文件名完全相等 → 文件名前缀 → 文件名包含 → 路径包含。同一档里目录略高于
 * 文件，因为打开选择器后最常见的一步是往下钻一层，而不是选中同名的文件。
 */
export function rankEntries(entries: PickEntry[], query: string, limit = PICK_LIMIT): PickEntry[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const scored: { entry: PickEntry; score: number; order: number }[] = [];
  entries.forEach((entry, order) => {
    const path = normalizePath(entry.path).toLowerCase();
    const name = nameOf(entry);
    let score: number;
    if (name === needle) score = 0;
    else if (name.startsWith(needle)) score = 1;
    else if (name.includes(needle)) score = 2;
    else if (path.includes(needle)) score = 3;
    else return;
    scored.push({ entry, score: score * 2 + (entry.is_dir ? 0 : 1), order });
  });
  scored.sort((a, b) => (a.score === b.score ? a.order - b.order : a.score - b.score));
  return scored.slice(0, limit).map((row) => row.entry);
}

/**
 * 选择器要显示的行。
 *
 * 有查询词时在全树里搜，没有时只看当前目录——两件事混在一起，「进了一个空目录」
 * 和「搜不到东西」在界面上就长得一样了。
 */
export function pickerRows(
  entries: PickEntry[],
  dir: string,
  query: string,
  limit = PICK_LIMIT,
): PickEntry[] {
  if (query.trim()) return rankEntries(entries, query, limit);
  return childrenOf(entries, dir).slice(0, limit);
}

/** 键盘上下移动：越界时夹住而不是回绕，免得按到底突然跳回第一行。 */
export function moveIndex(index: number, delta: number, length: number): number {
  if (length <= 0) return 0;
  const next = index + delta;
  if (next < 0) return 0;
  if (next >= length) return length - 1;
  return next;
}

/**
 * 把选中的路径拼进草稿。
 *
 * 只在需要时补空格：光标停在一句话中间时，硬插空格会把中文断成两截。
 * 结尾固定补一个空格，因为引用后面接着写的是正文。
 */
export function insertReference(draft: string, path: string): string {
  const ref = `@${normalizePath(path)}`;
  if (!draft) return `${ref} `;
  return draft.endsWith(" ") || draft.endsWith("\n") ? `${draft}${ref} ` : `${draft} ${ref} `;
}