"use client";

import { Fragment, type ReactNode } from "react";
import { isTrivialItem, splitReport, type ReportSection } from "./report";

const SECTION_STYLE: Record<ReportSection["tone"], { label: string; chip: string; bullet: string }> = {
  done: { label: "text-accent", chip: "border-accent/25 bg-accent/[0.08] text-accent", bullet: "bg-accent/60" },
  undone: { label: "text-warn", chip: "border-warn/25 bg-warn/[0.08] text-warn", bullet: "bg-warn/60" },
  next: { label: "text-brand", chip: "border-brand/25 bg-brand/[0.08] text-brand", bullet: "bg-brand/60" },
};

/** 行内强调：只处理 `**粗体**` 与 `` `代码` ``，其余原样输出。 */
function inline(text: string, key: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
  let last = 0;
  let index = 0;
  let match: RegExpExecArray | null = pattern.exec(text);
  while (match) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) {
      nodes.push(
        <strong key={`${key}-b-${index}`} className="font-semibold text-text">
          {token.slice(2, -2)}
        </strong>,
      );
    } else {
      nodes.push(
        <code key={`${key}-c-${index}`} className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text/90">
          {token.slice(1, -1)}
        </code>,
      );
    }
    last = match.index + token.length;
    index += 1;
    match = pattern.exec(text);
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

type Piece = { kind: "p" | "ul" | "ol" | "code"; text: string[] };

/** 把纯文本切成段落 / 列表 / 代码块。够用就行，不做完整 Markdown。 */
function toPieces(text: string): Piece[] {
  const pieces: Piece[] = [];
  let fence: string[] | null = null;
  let list: Piece | null = null;

  const flushList = () => {
    if (list) {
      pieces.push(list);
      list = null;
    }
  };

  for (const raw of (text ?? "").replace(/\r\n/g, "\n").split("\n")) {
    if (raw.trim().startsWith("```")) {
      if (fence) {
        pieces.push({ kind: "code", text: fence });
        fence = null;
      } else {
        flushList();
        fence = [];
      }
      continue;
    }
    if (fence) {
      fence.push(raw);
      continue;
    }
    const ordered = /^\s*\d+[.、)]\s+/.test(raw);
    const unordered = /^\s*[-*•]\s+/.test(raw);
    if (ordered || unordered) {
      const want = ordered ? "ol" : "ul";
      if (!list || list.kind !== want) {
        flushList();
        list = { kind: want, text: [] };
      }
      list.text.push(raw.replace(/^\s*(?:[-*•]|\d+[.、)])\s+/, "").trim());
      continue;
    }
    flushList();
    if (raw.trim()) pieces.push({ kind: "p", text: [raw.trim()] });
  }
  flushList();
  if (fence) pieces.push({ kind: "code", text: fence });
  return pieces;
}

function RichText({ text }: { text: string }) {
  const pieces = toPieces(text);
  return (
    <div className="flex flex-col gap-1.5">
      {pieces.map((piece, index) => {
        const key = `piece-${index}`;
        if (piece.kind === "code") {
          return (
            <pre
              key={key}
              className="overflow-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap break-words text-text/85"
            >
              {piece.text.join("\n")}
            </pre>
          );
        }
        if (piece.kind === "ul" || piece.kind === "ol") {
          const Tag = piece.kind === "ul" ? "ul" : "ol";
          return (
            <Tag key={key} className={`flex flex-col gap-1 ${piece.kind === "ol" ? "list-none" : "list-none"}`}>
              {piece.text.map((item, itemIndex) => (
                <li key={`${key}-${itemIndex}`} className="flex items-start gap-2">
                  <span className="mt-[7px] size-1 shrink-0 rounded-full bg-line-strong" />
                  <span className="min-w-0 flex-1">{inline(item, `${key}-${itemIndex}`)}</span>
                </li>
              ))}
            </Tag>
          );
        }
        return (
          <p key={key} className="whitespace-pre-wrap">
            {inline(piece.text.join("\n"), key)}
          </p>
        );
      })}
    </div>
  );
}

function SectionView({ section }: { section: ReportSection }) {
  const style = SECTION_STYLE[section.tone];
  return (
    <section className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className={`rounded border px-1.5 py-0.5 text-[10.5px] font-medium tracking-wide ${style.chip}`}>
          {section.title}
        </span>
        <span className={`h-px min-w-0 flex-1 ${section.tone === "done" ? "bg-accent/15" : section.tone === "undone" ? "bg-warn/15" : "bg-brand/15"}`} />
      </div>
      <ul className="flex flex-col gap-1">
        {section.items.map((item, index) => {
          const trivial = isTrivialItem(item);
          return (
            <li key={`${section.title}-${index}`} className="flex items-start gap-2">
              <span className={`mt-[7px] size-1 shrink-0 rounded-full ${trivial ? "bg-line-strong" : style.bullet}`} />
              <span className={`min-w-0 flex-1 ${trivial ? "text-faint" : "text-text/90"}`}>
                {inline(item, `${section.title}-${index}`)}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * 交付视图：三段式结果按分区渲染，普通回答按富文本渲染。
 *
 * 判据只看结构，不看状态——模型偶尔会漏写某一段，缺的那段不补占位，
 * 少一行比多一段"无"更干净。
 */
export function ReplyView({ text, className = "" }: { text: string; className?: string }) {
  const { sections, body } = splitReport(text);
  if (sections.length === 0) {
    return (
      <div className={`text-[13.5px] leading-relaxed text-text/90 ${className}`}>
        <RichText text={body || text} />
      </div>
    );
  }
  return (
    <div className={`flex flex-col gap-3 text-[13.5px] leading-relaxed ${className}`}>
      {body && (
        <p className="whitespace-pre-wrap text-text/80">
          {body.split("\n").map((line, index) => (
            <Fragment key={`lead-${index}`}>
              {index > 0 && <br />}
              {inline(line, `lead-${index}`)}
            </Fragment>
          ))}
        </p>
      )}
      {sections.map((section) => (
        <SectionView key={section.title} section={section} />
      ))}
    </div>
  );
}
