"use client";

import { useEffect, useRef, useState } from "react";
import { Button, Notice, Spinner } from "@/components/projects/ui";
import { errorMessage, streamChat, type Provider } from "./api";

type Props = {
  providers: Provider[];
  onFinished: () => void;
};

type Status = "idle" | "running" | "done" | "error";

const starter = "用一句话说明你是谁，以及你能帮开发者做什么。";

export function StreamDemo({ providers, onFinished }: Props) {
  const enabled = providers.filter((provider) => provider.enabled);
  const [providerId, setProviderId] = useState("");
  const [message, setMessage] = useState(starter);
  const [text, setText] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [notice, setNotice] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const active = enabled.find((provider) => provider.id === providerId) ?? enabled[0] ?? null;

  async function start() {
    if (!active || status === "running") return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setText("");
    setNotice(null);
    setStatus("running");
    setElapsed(0);
    const ticker = window.setInterval(
      () => setElapsed((current) => Math.round((current + 0.1) * 10) / 10),
      100,
    );
    let received = 0;
    const stream = { failed: false };
    try {
      await streamChat(active.id, message.trim(), {
        signal: abort.signal,
        onDelta: (chunk) => {
          received += chunk.length;
          setText((current) => current + chunk);
        },
        onNotice: (line) => {
          stream.failed = true;
          setNotice(line);
          setStatus("error");
        },
      });
      if (stream.failed) setStatus("error");
      else if (received === 0) {
        setNotice("流式连接已建立，但模型没有返回任何内容。");
        setStatus("error");
      } else setStatus("done");
    } catch (caught) {
      if ((caught as Error).name !== "AbortError") {
        setNotice(errorMessage(caught));
        setStatus("error");
      }
    } finally {
      window.clearInterval(ticker);
      onFinished();
    }
  }

  function stop() {
    controller.current?.abort();
    setStatus("idle");
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={active?.id ?? ""}
          onChange={(event) => setProviderId(event.target.value)}
          aria-label="选择供应商"
          disabled={enabled.length === 0}
          className="min-w-0 flex-1 rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[12px] text-text focus:border-brand/60 focus:outline-none disabled:opacity-60"
        >
          {enabled.length === 0 && <option value="">没有已启用的供应商</option>}
          {enabled.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name} · {provider.model}
            </option>
          ))}
        </select>
        {status === "running" ? (
          <Button variant="danger" onClick={stop}>
            停止
          </Button>
        ) : (
          <Button variant="primary" disabled={!active || !message.trim()} onClick={() => void start()}>
            {status === "done" ? "重新发送" : "发送并流式接收"}
          </Button>
        )}
      </div>

      <textarea
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        aria-label="演示提示词"
        rows={2}
        spellCheck={false}
        className="w-full resize-y rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12px] leading-relaxed text-text placeholder:text-faint focus:border-brand/60 focus:outline-none"
        placeholder="输入一段发给模型的文本"
      />

      <div className="min-h-[9rem] rounded-lg border border-line bg-surface-2 p-3">
        {status === "idle" && !text && !notice ? (
          <div className="flex items-center gap-2 font-mono text-[11px] text-faint">
            <span className="text-accent">$</span>
            POST /providers/:id/chat/stream · 等待发送
          </div>
        ) : (
          <pre className="font-mono text-[11px] leading-relaxed break-words whitespace-pre-wrap text-muted">
            {text}
            {status === "running" && <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-brand align-middle" />}
          </pre>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {status === "running" && (
          <span className="flex items-center gap-1.5 font-mono text-[10px] text-brand">
            <Spinner />
            流式接收中 · {elapsed.toFixed(1)}s
          </span>
        )}
        {status === "done" && (
          <span className="font-mono text-[10px] text-accent">
            完成 · {text.length} 字符 · {elapsed.toFixed(1)}s
          </span>
        )}
        {active && (
          <span className="font-mono text-[10px] text-faint">
            SSE 帧：event: delta / data: {"{ \"text\": … }"}
          </span>
        )}
      </div>

      {notice && <Notice tone="error">流式调用失败：{notice}</Notice>}
    </div>
  );
}
