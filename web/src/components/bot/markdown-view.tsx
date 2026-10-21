"use client";

import { useState } from "react";
import { parseInline, parseMarkdown, type Inline } from "./markdown";
import { resolveTouchedPath } from "./turns";

type AnchorProps = {
  /** 本轮改过的文件路径。给了才会把正文里的文件名变成可点的锚点。 */
  files?: readonly string[];
  onFileClick?: (path: string) => void;
};

function InlineView({ spans, files, onFileClick }: { spans: Inline[] } & AnchorProps) {
  return (
    <>
      {spans.map((span, index) => {
        if (span.kind === "code") {
          // 正文里点了文件名就落到那一处改动上。这是"回复可以只写结论"的前提：
          // 提到改动时不必把细节铺在正文里，一个可点的名字就够。
          // 认不出（或同名文件有歧义）时保持普通代码样式，不做半可点的东西。
          const path = files?.length && onFileClick ? resolveTouchedPath(span.text, files) : "";
          if (path && onFileClick) {
            const jump = onFileClick;
            return (
              <button
                key={index}
                type="button"
                onClick={() => jump(path)}
                title={`看 ${path} 这处改动`}
                className="rounded border border-line bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-brand underline decoration-brand/25 decoration-dotted underline-offset-2 transition-colors hover:border-brand/40 hover:decoration-brand"
              >
                {span.text}
              </button>
            );
          }
          return (
            <code
              key={index}
              className="rounded border border-line bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text"
            >
              {span.text}
            </code>
          );
        }
        if (span.kind === "bold") {
          return (
            <strong key={index} className="font-semibold text-text">
              {span.text}
            </strong>
          );
        }
        if (span.kind === "link") {
          return (
            <a
              key={index}
              href={span.href}
              target="_blank"
              rel="noreferrer noopener"
              className="text-brand underline decoration-brand/30 underline-offset-2 hover:decoration-brand"
            >
              {span.text}
            </a>
          );
        }
        return (
          <span key={index} className="whitespace-pre-wrap">
            {span.text}
          </span>
        );
      })}
    </>
  );
}

function CodeBlock({ text, lang }: { text: string; lang: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="my-3 overflow-hidden rounded-lg border border-line">
      <div className="flex items-center justify-between border-b border-line bg-surface-2 px-2.5 py-1">
        <span className="font-mono text-[10px] tracking-wide text-faint uppercase">{lang || "text"}</span>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(text).then(
              () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1200);
              },
              () => undefined,
            );
          }}
          className="rounded px-1.5 py-0.5 font-mono text-[10px] text-faint transition-colors hover:bg-surface-3 hover:text-text"
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="max-h-96 overflow-auto bg-surface px-3 py-2.5">
        <code className="font-mono text-[12px] leading-relaxed text-text">{text}</code>
      </pre>
    </div>
  );
}

/** 模型回复的渲染入口。空串返回 null——不要在对话里留一个空白块。 */
export function MarkdownView({ text, files, onFileClick }: { text: string } & AnchorProps) {
  const blocks = parseMarkdown(text);
  if (blocks.length === 0) return null;
  return (
    <div className="text-[13.5px] leading-[1.75] text-text">
      {blocks.map((block, index) => {
        if (block.kind === "code") return <CodeBlock key={index} text={block.text} lang={block.lang} />;
        if (block.kind === "heading") {
          const size = block.level <= 2 ? "text-[15px] font-semibold" : "text-[13.5px] font-semibold";
          return (
            <p key={index} className={`mt-4 mb-1.5 first:mt-0 ${size}`}>
              <InlineView spans={block.spans} files={files} onFileClick={onFileClick} />
            </p>
          );
        }
        if (block.kind === "list") {
          const Tag = block.ordered ? "ol" : "ul";
          return (
            <Tag
              key={index}
              className={`my-2 space-y-1 pl-5 ${block.ordered ? "list-decimal" : "list-disc"} marker:text-faint`}
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex} className="pl-0.5">
                  <InlineView spans={item} files={files} onFileClick={onFileClick} />
                </li>
              ))}
            </Tag>
          );
        }
        if (block.kind === "quote") {
          return (
            <blockquote key={index} className="my-2 border-l-2 border-line-strong pl-3 text-muted">
              <InlineView spans={block.spans} files={files} onFileClick={onFileClick} />
            </blockquote>
          );
        }
        if (block.kind === "hr") {
          return <hr key={index} className="my-4 border-line" />;
        }
        return (
          <p key={index} className="my-2 first:mt-0 last:mb-0">
            <InlineView spans={block.spans} files={files} onFileClick={onFileClick} />
          </p>
        );
      })}
    </div>
  );
}

/** 对话里只给一小段文字（工具输出、总结）时用它：不解析 markdown，保留换行。 */
export function PlainText({ text, className = "" }: { text: string; className?: string }) {
  return <span className={`whitespace-pre-wrap ${className}`}>{text}</span>;
}

export { parseInline };
