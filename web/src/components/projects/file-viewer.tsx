"use client";

import { useEffect, useState } from "react";
import { errorDetails, fetchFile, type FileContent, type TreeEntry } from "./api";
import { clipMiddle, formatSize } from "./format";
import { describeFailure } from "./file-tree";
import { Notice, Spinner, StateLine } from "./ui";

type Failure = { code: string; message: string };

type Result = { key: string; data: FileContent } | { key: string; failure: Failure };

type Props = {
  projectId: string;
  workspaceId: string;
  entry: TreeEntry | null;
};

export function FileViewer({ projectId, workspaceId, entry }: Props) {
  const [result, setResult] = useState<Result | null>(null);
  const path = entry?.path ?? null;
  const requestKey = path === null ? null : `${projectId}:${workspaceId}:${path}`;

  useEffect(() => {
    if (!path || !requestKey) return;
    let ignore = false;
    fetchFile(projectId, path)
      .then((data) => {
        if (!ignore) setResult({ key: requestKey, data });
      })
      .catch((error: unknown) => {
        if (!ignore) setResult({ key: requestKey, failure: errorDetails(error) });
      });
    return () => {
      ignore = true;
    };
  }, [projectId, requestKey, path]);

  if (!entry || !path || !requestKey) {
    return <StateLine>从文件树选择文件</StateLine>;
  }

  const settled = result?.key === requestKey ? result : null;
  if (!settled) {
    return (
      <div className="flex items-center justify-center gap-2 py-6 font-mono text-[11px] text-faint">
        <Spinner />
        读取 {clipMiddle(path, 32)}
      </div>
    );
  }

  if ("failure" in settled) {
    const note = describeFailure(settled.failure);
    return <Notice tone={note.tone}>文件读取失败：{note.text}</Notice>;
  }

  const { data } = settled;
  return (
    <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-line bg-black/30">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-line px-3 py-2">
        <span className="min-w-0 truncate font-mono text-[11px] text-brand" title={data.path}>
          {clipMiddle(data.path)}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-faint">
          {formatSize(data.size)} · {data.encoding}
        </span>
      </div>
      <div className="flex min-h-0 flex-col">
        {data.binary ? (
          <div className="p-3">
            <Notice tone="muted">{data.message || "无法预览该文件"}</Notice>
          </div>
        ) : (
          <>
            {data.truncated && (
              <div className="border-b border-warn/20 bg-warn/[0.06] px-3 py-2 text-[11px] text-warn">
                {data.message || "文件过大，内容已截断"}
              </div>
            )}
            <pre
              tabIndex={0}
              className="max-h-[24rem] overflow-auto px-3 py-2.5 font-mono text-[11px] leading-relaxed whitespace-pre text-muted outline-none focus-visible:ring-1 focus-visible:ring-brand/40"
            >
              {data.content}
            </pre>
          </>
        )}
      </div>
    </div>
  );
}
