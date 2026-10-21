/**
 * 极简 markdown → 块。不引第三方渲染器，因为这里只服务于"模型回复"这一种输入：
 * 它的实际用法集中在标题、列表、代码块、行内代码和粗体，以及偶尔的引用。
 *
 * 两条必须守住的边界：
 * 1. **流式过程中输入是不完整的**。半个 ``` 围栏要按代码块渲染，而不是吞掉后面的内容；
 *    解析器宁可多显示一个空代码块，也不能让正在输出的正文凭空消失。
 * 2. **不实现 HTML**。模型输出里可能带 `<div>`，那段应该原样显示成文字，
 *    而不是变成页面结构——这是一个能改文件的程序，拿它的输出当 DOM 用等于把方向盘交出去。
 */

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "bold"; text: string }
  | { kind: "link"; text: string; href: string };

export type MdBlock =
  | { kind: "p"; spans: Inline[] }
  | { kind: "code"; lang: string; text: string }
  | { kind: "heading"; level: number; spans: Inline[] }
  | { kind: "list"; ordered: boolean; items: Inline[][] }
  | { kind: "quote"; spans: Inline[] }
  | { kind: "hr" };

const INLINE_PATTERN = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

export function parseInline(text: string): Inline[] {
  const spans: Inline[] = [];
  let cursor = 0;
  INLINE_PATTERN.lastIndex = 0;
  for (let match = INLINE_PATTERN.exec(text); match; match = INLINE_PATTERN.exec(text)) {
    if (match.index > cursor) spans.push({ kind: "text", text: text.slice(cursor, match.index) });
    if (match[1] !== undefined) spans.push({ kind: "code", text: match[1] });
    else if (match[2] !== undefined) spans.push({ kind: "bold", text: match[2] });
    else if (match[3] !== undefined) spans.push({ kind: "link", text: match[3], href: match[4] });
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) spans.push({ kind: "text", text: text.slice(cursor) });
  return spans.length > 0 ? spans : [{ kind: "text", text }];
}

const FENCE = /^\s*```(\S*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const UL = /^\s*[-*+]\s+(.*)$/;
const OL = /^\s*\d+[.)]\s+(.*)$/;

export function parseMarkdown(source: string): MdBlock[] {
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: MdBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const lang = fence[1] ?? "";
      const body: string[] = [];
      index += 1;
      // 没闭合也要收尾：流式输出到一半时这就是"正在写的代码块"。
      while (index < lines.length && !FENCE.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ kind: "code", lang, text: body.join("\n") });
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1].length, spans: parseInline(heading[2].trim()) });
      index += 1;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      blocks.push({ kind: "hr" });
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const body: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        body.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push({ kind: "quote", spans: parseInline(body.join(" ")) });
      continue;
    }

    const ordered = OL.test(line);
    if (ordered || UL.test(line)) {
      const pattern = ordered ? OL : UL;
      const items: Inline[][] = [];
      while (index < lines.length && pattern.test(lines[index])) {
        const item = (ordered ? OL : UL).exec(lines[index])![1];
        // 缩进的续行属于上一条，合并进去——否则一条长列表会被拆成互不相干的碎片。
        let text = item;
        index += 1;
        while (index < lines.length && /^\s{2,}\S/.test(lines[index]) && !pattern.test(lines[index])) {
          text += ` ${lines[index].trim()}`;
          index += 1;
        }
        items.push(parseInline(text));
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }

    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim() && !FENCE.test(lines[index]) && !HEADING.test(lines[index]) && !UL.test(lines[index]) && !OL.test(lines[index]) && !/^\s*>\s?/.test(lines[index])) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({ kind: "p", spans: parseInline(paragraph.join("\n")) });
  }

  return blocks;
}
