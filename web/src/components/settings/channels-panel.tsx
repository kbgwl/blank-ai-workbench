"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { errorMessage } from "@/lib/http";
import {
  createChannel,
  deleteChannel,
  fetchChannelCatalog,
  fetchChannels,
  testChannel,
  verifyChannel,
  type Channel,
  type ChannelAdapter,
  type ChannelCatalog,
  type ChannelList,
} from "./channels-api";

/**
 * 消息通道：客户填一遍就能用的那一屏。
 *
 * 三条刻意的设计：
 *
 * 1. **表单由后端目录生成。** 字段名、是否密钥、必填、按接法过滤全部来自适配器
 *    声明。前端不认识"企业微信有哪些字段"，所以后端加一项这里自动就有。
 * 2. **「已接通」只认真实调用。** 保存不算通，填满也不算。验证按钮会真的去打
 *    厂商接口；群机器人没有只读接口，界面就写「没验过，发条测试消息」。
 * 3. **收不到消息时直说原因。** 回调地址要公网，本机没有——那就把这句话印在
 *    通道卡片上，而不是让客户配完等一条永远不来的消息。
 */

const STATUS_STYLE: Record<string, string> = {
  ok: "border-brand/40 bg-brand/10 text-brand",
  failed: "border-danger/40 bg-danger/10 text-danger",
  unverified: "border-warn/35 bg-warn/10 text-warn",
};

const STATUS_LABEL: Record<string, string> = {
  ok: "已接通",
  failed: "验证失败",
  unverified: "未验证",
};

const RECEIVE_LABEL: Record<string, string> = {
  webhook: "回调收消息",
  stream: "长连接收消息",
  off: "只发不收",
};

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="shrink-0 rounded border border-line-strong bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted hover:text-text"
    >
      {copied ? "已复制" : "复制"}
    </button>
  );
}

function ChannelCard({
  channel,
  adapter,
  busy,
  notice,
  onVerify,
  onTest,
  onDelete,
}: {
  channel: Channel;
  adapter?: ChannelAdapter;
  busy: boolean;
  notice?: { ok: boolean; text: string };
  onVerify: () => void;
  onTest: () => void;
  onDelete: () => void;
}) {
  const style = STATUS_STYLE[channel.status] ?? STATUS_STYLE.unverified;
  return (
    <div className="rounded-md border border-line/70 bg-surface/40 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[12.5px] font-medium">{channel.name}</span>
        <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 text-[10px] text-faint">
          {channel.kind_label}
        </span>
        <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 text-[10px] text-faint">
          {adapter?.modes.find((item) => item.value === channel.mode)?.label ?? channel.mode}
        </span>
        <span className={`rounded border px-1 py-0.5 text-[10px] ${style}`}>
          {STATUS_LABEL[channel.status] ?? channel.status}
        </span>
        <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 text-[10px] text-faint">
          {RECEIVE_LABEL[channel.receive_mode] ?? channel.receive_mode}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            disabled={busy}
            onClick={onVerify}
            className="rounded border border-line-strong bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted hover:text-text disabled:opacity-50"
          >
            验证凭据
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onTest}
            className="rounded border border-brand/35 bg-brand/[0.06] px-2 py-0.5 text-[10.5px] text-brand hover:bg-brand/10 disabled:opacity-50"
          >
            发条测试消息
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onDelete}
            className="rounded border border-line bg-surface px-2 py-0.5 text-[10.5px] text-faint hover:border-danger/40 hover:text-danger disabled:opacity-50"
          >
            删除
          </button>
        </span>
      </div>
      {channel.status_note && (
        <p
          className={`mt-1.5 text-[11px] leading-relaxed ${
            channel.status === "failed" ? "text-danger/90" : "text-muted"
          }`}
        >
          {channel.status_note}
        </p>
      )}
      {channel.receipt.url ? (
        <div className="mt-1.5 flex items-center gap-2 rounded border border-line/70 bg-surface-2/40 px-2 py-1">
          <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-faint">{channel.receipt.url}</span>
          <CopyButton value={channel.receipt.url} />
        </div>
      ) : (
        channel.receipt.note && (
          <p className="mt-1.5 text-[10.5px] leading-relaxed text-faint">{channel.receipt.note}</p>
        )
      )}
      {channel.receipt.url && <p className="mt-1 text-[10.5px] leading-relaxed text-faint">{channel.receipt.note}</p>}
      <p className="mt-1 font-mono text-[10px] text-faint">
        {channel.last_inbound_at ? `最近收到消息 ${channel.last_inbound_at}` : "还没收到过消息"}
        {channel.last_inbound_error ? ` · 入站错误：${channel.last_inbound_error}` : ""}
      </p>
      {notice && (
        <p className={`mt-1 text-[11px] leading-relaxed ${notice.ok ? "text-brand" : "text-danger/90"}`}>
          {notice.text}
        </p>
      )}
    </div>
  );
}

function AddForm({
  adapter,
  busy,
  onCancel,
  onSubmit,
}: {
  adapter: ChannelAdapter;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (draft: {
    name: string;
    mode: string;
    config: Record<string, string>;
    push: boolean;
    receive: boolean;
  }) => void;
}) {
  const [mode, setMode] = useState(adapter.modes[0]?.value ?? "app");
  const [name, setName] = useState(adapter.label);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const seed: Record<string, string> = {};
    for (const field of adapter.fields) seed[field.key] = field.default;
    return seed;
  });
  const [push, setPush] = useState(true);
  const [receive, setReceive] = useState((adapter.modes[0]?.receive.length ?? 0) > 0);

  const visible = adapter.fields.filter(
    (field) => field.only_modes.length === 0 || field.only_modes.includes(mode),
  );
  const meta = adapter.modes.find((item) => item.value === mode);

  return (
    <div className="rounded-md border border-brand/25 bg-brand/[0.03] px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12.5px] font-medium">接入 {adapter.label}</span>
        <span className="font-mono text-[10px] text-faint">{adapter.vendor}</span>
        <button type="button" onClick={onCancel} className="ml-auto text-[10.5px] text-faint hover:text-text">
          取消
        </button>
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-muted">{adapter.summary}</p>

      {adapter.modes.length > 1 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {adapter.modes.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => {
                setMode(item.value);
                setReceive(item.receive.length > 0);
              }}
              className={`rounded border px-2 py-1 text-left text-[11px] ${
                mode === item.value
                  ? "border-brand/40 bg-brand/10 text-brand"
                  : "border-line bg-surface text-muted hover:border-line-strong"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
      {meta?.hint && <p className="mt-1 text-[10.5px] leading-relaxed text-faint">{meta.hint}</p>}

      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <label className="text-[11px] text-muted">
          名字（自己认得出就行）
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            className="mt-1 w-full rounded border border-line bg-surface px-2 py-1 text-[12px] text-text"
          />
        </label>
        {visible.map((field) => (
          <label key={field.key} className={`text-[11px] text-muted ${field.key === "template" ? "sm:col-span-2" : ""}`}>
            {field.label}
            {field.required && <span className="ml-0.5 text-danger/80">*</span>}
            <input
              value={values[field.key] ?? ""}
              placeholder={field.placeholder}
              onChange={(event) => setValues((prev) => ({ ...prev, [field.key]: event.target.value }))}
              className="mt-1 w-full rounded border border-line bg-surface px-2 py-1 font-mono text-[11.5px] text-text"
            />
            {field.help && <span className="mt-0.5 block text-[10px] leading-relaxed text-faint">{field.help}</span>}
          </label>
        ))}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-4 text-[11px] text-muted">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={push} onChange={(event) => setPush(event.target.checked)} />
          任务提醒推到这里（等审批 / 失败 / 完成）
        </label>
        <label className={`flex items-center gap-1.5 ${meta && meta.receive.length === 0 ? "opacity-50" : ""}`}>
          <input
            type="checkbox"
            checked={receive}
            disabled={!meta || meta.receive.length === 0}
            onChange={(event) => setReceive(event.target.checked)}
          />
          允许从这里收消息开任务
        </label>
      </div>

      <details className="mt-2 rounded border border-line/70 bg-surface/40 px-2 py-1.5">
        <summary className="cursor-pointer text-[11px] text-muted">在厂商后台要点哪几步</summary>
        <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-[10.5px] leading-relaxed text-faint">
          {adapter.setup.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
        {adapter.docs && (
          <a
            href={adapter.docs}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-block text-[10.5px] text-brand hover:underline"
          >
            官方文档
          </a>
        )}
      </details>

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onSubmit({ name, mode, config: values, push, receive })}
          className="rounded-md bg-brand px-3 py-1 text-[11.5px] font-medium text-canvas disabled:opacity-50"
        >
          {busy ? "保存中…" : "保存并验证"}
        </button>
        <span className="text-[10px] leading-relaxed text-faint">
          保存后会自动打一次厂商接口。密钥加密存在本机，不会回显。
        </span>
      </div>
    </div>
  );
}

export function ChannelsPanel() {
  const [catalog, setCatalog] = useState<ChannelCatalog | null>(null);
  const [list, setList] = useState<ChannelList | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState("");
  const [notices, setNotices] = useState<Record<string, { ok: boolean; text: string }>>({});
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    Promise.all([fetchChannelCatalog(), fetchChannels()])
      .then(([nextCatalog, nextList]) => {
        if (!alive) return;
        setCatalog(nextCatalog);
        setList(nextList);
        setError("");
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  const adapters = useMemo(
    () => Object.fromEntries((catalog?.adapters ?? []).map((item) => [item.kind, item])),
    [catalog],
  );

  const refresh = useCallback(() => setReloadToken((token) => token + 1), []);

  function note(id: string, ok: boolean, text: string) {
    setNotices((prev) => ({ ...prev, [id]: { ok, text } }));
  }

  async function submit(kind: string, draft: { name: string; mode: string; config: Record<string, string>; push: boolean; receive: boolean }) {
    setBusy("create");
    try {
      const adapter = adapters[kind];
      const receiveMode = draft.receive
        ? (adapter?.modes.find((item) => item.value === draft.mode)?.receive[0] ?? "")
        : "";
      const created = await createChannel({ kind, ...draft, receive_mode: receiveMode, project_id: "" });
      setAdding("");
      refresh();
      // 保存后立刻验证一次：客户不用猜"到底通没通"。
      const result = await verifyChannel(created.channel.id);
      note(
        created.channel.id,
        result.check.ok,
        result.check.ok ? `已验证：${result.check.detail}` : result.check.detail,
      );
      refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function verify(id: string) {
    setBusy(id);
    try {
      const result = await verifyChannel(id);
      note(id, result.check.ok, result.check.detail);
      refresh();
    } catch (err) {
      note(id, false, errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function test(id: string) {
    setBusy(id);
    try {
      const result = await testChannel(id);
      note(id, result.send.ok, result.send.detail);
      refresh();
    } catch (err) {
      note(id, false, errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  async function remove(id: string) {
    setBusy(id);
    try {
      await deleteChannel(id);
      refresh();
    } catch (err) {
      note(id, false, errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  if (!catalog || !list) {
    return (
      <Panel title="消息通道" hint="GET /channels">
        <p className="text-[12px] text-faint">{error || "加载中…"}</p>
      </Panel>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel title="消息通道" hint={`${list.channels.length} 条已配置`}>
        <p className="text-[11.5px] leading-relaxed text-muted">
          把任务提醒推到客户自己的企业微信 / 钉钉 / 飞书，也可以在那边发一句话让它动手。
          凭据加密存在本机；「已接通」只有在真打过厂商接口之后才会出现。
        </p>
        {!list.triggers_enabled && (
          <p className="mt-2 rounded border border-line/70 bg-surface-2/40 px-2 py-1 text-[11px] leading-relaxed text-faint">
            通道收发不依赖「后台主动执行」那个开关：消息是你主动发的，定时与盯着也是你交代的。
            那个开关管的是 Agent 自己发起的事（自动修复等）。
          </p>
        )}
        {list.channels.length === 0 ? (
          <p className="mt-2 rounded border border-line/70 bg-surface-2/40 px-2 py-2 text-[11.5px] leading-relaxed text-faint">
            还没有配置。下面选一家填上凭据即可——最快的路径是群机器人（只发提醒）或钉钉 Stream（可收发，不需要公网）。
          </p>
        ) : (
          <div className="mt-2 flex flex-col gap-2">
            {list.channels.map((channel) => (
              <ChannelCard
                key={channel.id}
                channel={channel}
                adapter={adapters[channel.kind]}
                busy={busy === channel.id}
                notice={notices[channel.id]}
                onVerify={() => void verify(channel.id)}
                onTest={() => void test(channel.id)}
                onDelete={() => void remove(channel.id)}
              />
            ))}
          </div>
        )}

        <div className="mt-3 border-t border-line/70 pt-2.5">
          <p className="text-[11px] text-muted">加一条通道</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {catalog.adapters.map((adapter) => (
              <button
                key={adapter.kind}
                type="button"
                onClick={() => setAdding(adapter.kind === adding ? "" : adapter.kind)}
                className={`rounded-md border px-2.5 py-1 text-[11.5px] ${
                  adding === adapter.kind
                    ? "border-brand/40 bg-brand/10 text-brand"
                    : "border-line bg-surface text-muted hover:border-line-strong hover:text-text"
                }`}
              >
                {adapter.label}
              </button>
            ))}
          </div>
          {adding && adapters[adding] && (
            <div className="mt-2">
              <AddForm
                adapter={adapters[adding]}
                busy={busy === "create"}
                onCancel={() => setAdding("")}
                onSubmit={(draft) => void submit(adding, draft)}
              />
            </div>
          )}
        </div>

        {error && <p className="mt-2 text-[11px] leading-relaxed text-danger/90">{error}</p>}
      </Panel>

      <Panel title="收消息要什么条件" hint={list.public_base ? `对外地址 ${list.public_base}` : "未配置对外地址"}>
        <div className="flex flex-col gap-1.5">
          {catalog.receive_notes.map((item) => (
            <div key={item.mode} className="rounded border border-line/70 bg-surface/40 px-2 py-1.5">
              <p className="text-[11.5px] text-text">{item.label}</p>
              <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">{item.note}</p>
            </div>
          ))}
        </div>
        <p className="mt-2 text-[10.5px] leading-relaxed text-faint">
          本机形态没有公网地址时，只有「长连接」这一条路能用（目前是钉钉）。要收企业微信/飞书的消息，
          得先把后端挂到一个公网可达的 https 地址上，再把 BLANK_PUBLIC_BASE_URL 指过去，回调地址才会正确生成。
        </p>
      </Panel>
    </div>
  );
}
