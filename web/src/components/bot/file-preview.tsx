"use client";

import type { FileContent } from "../projects/api";
import { MarkdownView } from "./markdown-view";
import { delimiterFor, formatJson, parseDelimited, previewKind } from "./preview";

type FilePreviewProps = {
  path: string;
  content: FileContent;
  files?: readonly string[];
  onFileClick?: (path: string) => void;
};

/**
 * 文件预览：按扩展名选渲染器。
 *
 * HTML 只用最严格的 sandbox（`sandbox=""`）：脚本不执行、表单不能提交、不能访问
 * 父页面存储。这是刻意的——产物是模型写的，预览区不该替它执行代码。相对路径的
 * CSS/JS/图片也不会加载（srcDoc 没有文档基地址），需要完整页面时走多文件应用预览。
 */
export function FilePreview({ path, content, files, onFileClick }: FilePreviewProps) {
  if (content.binary) {
    return (
      <div className="grid h-full place-items-center bg-transparent px-4 text-center">
        <p className="text-[11px] leading-relaxed text-faint">二进制文件，无法在结果区预览。</p>
      </div>
    );
  }
  if (!content.content) {
    return (
      <div className="grid h-full place-items-center bg-transparent px-4 text-center">
        <p className="text-[11px] leading-relaxed text-faint">空文件，没有可预览的内容。</p>
      </div>
    );
  }

  const kind = previewKind(path);
  return (
    <div className="flex h-full flex-col bg-transparent">
      {content.preview === "office" && (
        <div className="shrink-0 border-b border-line bg-surface-2 px-3 py-2 text-[11px] leading-relaxed text-faint">
          {content.message || "文档文本预览：样式与图表已简化。"}
        </div>
      )}
      {content.truncated && content.preview !== "office" && (
        <div className="shrink-0 border-b border-line bg-surface-2 px-3 py-2 text-[11px] leading-relaxed text-faint">
          文件较大，预览仅显示前一部分；完整内容请用只读工具读取。
        </div>
      )}
      <div className="min-h-0 flex-1">{renderBody(kind, path, content.content, files, onFileClick)}</div>
    </div>
  );
}

function renderBody(
  kind: ReturnType<typeof previewKind>,
  path: string,
  text: string,
  files?: readonly string[],
  onFileClick?: (path: string) => void,
) {
  if (kind === "html") {
    return (
      <div className="flex h-full flex-col">
        <div className="shrink-0 border-b border-line bg-warn/[0.08] px-3 py-2 text-[11px] leading-relaxed text-warn">
          静态预览：脚本不会执行，相对路径的 CSS/JS/图片不会加载。需要完整交互请用多文件应用预览。
        </div>
        <iframe
          title={`预览 ${path}`}
          sandbox=""
          referrerPolicy="no-referrer"
          srcDoc={text}
          className="min-h-0 flex-1 border-0 bg-white"
        />
      </div>
    );
  }

  if (kind === "markdown") {
    return (
      <div className="h-full overflow-y-auto px-4 py-3">
        <MarkdownView text={text} files={files} onFileClick={onFileClick} />
      </div>
    );
  }

  if (kind === "json") {
    const formatted = formatJson(text);
    return (
      <div className="h-full overflow-y-auto">
        {!formatted.ok && (
          <div className="border-b border-line bg-warn/[0.08] px-3 py-2 text-[11px] leading-relaxed text-warn">
            JSON 解析失败：{formatted.error}；下面按原文显示。
          </div>
        )}
        <pre className="overflow-x-auto whitespace-pre px-4 py-3 font-mono text-[12px] leading-[1.7] text-muted">
          {formatted.ok ? formatted.text : text}
        </pre>
      </div>
    );
  }

  if (kind === "csv") {
    const table = parseDelimited(text, delimiterFor(path));
    if (table.rows.length === 0) {
      return (
        <div className="grid h-full place-items-center px-4 text-center">
          <p className="text-[11px] leading-relaxed text-faint">表格是空的。</p>
        </div>
      );
    }
    const [header, ...body] = table.rows;
    return (
      <div className="h-full overflow-auto p-3">
        {table.truncated && (
          <p className="mb-2 text-[11px] leading-relaxed text-faint">
            只显示前 {table.rows.length} 行、每行前 50 列。
          </p>
        )}
        <table className="border-collapse font-mono text-[11px]">
          <thead>
            <tr>
              {header.map((cell, index) => (
                <th
                  key={index}
                  className="sticky top-0 border border-line bg-surface-2 px-2 py-1 text-left font-medium text-text"
                >
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className="border border-line px-2 py-1 text-muted">
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <pre className="h-full overflow-auto whitespace-pre px-4 py-3 font-mono text-[12px] leading-[1.7] text-muted">
      {text}
    </pre>
  );
}
