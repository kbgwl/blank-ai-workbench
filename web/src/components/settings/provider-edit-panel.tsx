"use client";

import { useMemo, useState, type FormEvent } from "react";
import { Button, Field, Input, Notice, Spinner } from "@/components/projects/ui";
import { errorMessage, updateProvider, type Provider, type ProviderPatch } from "./api";
import { KindBadge, Toggle } from "./bits";

type Props = {
  provider: Provider;
  kinds: string[];
  onSaved: (provider: Provider) => void;
};

type Draft = {
  kind: string;
  base_url: string;
  model: string;
  api_key: string;
  temperature: string;
  max_tokens: string;
  context_window: string;
  supports_tools: boolean;
  supports_vision: boolean;
  enabled: boolean;
};

function draftOf(provider: Provider): Draft {
  return {
    kind: provider.kind,
    base_url: provider.base_url,
    model: provider.model,
    api_key: "",
    temperature: String(provider.temperature),
    max_tokens: String(provider.max_tokens),
    context_window: String(provider.context_window),
    supports_tools: provider.supports_tools,
    supports_vision: provider.supports_vision,
    enabled: provider.enabled,
  };
}

export function ProviderEditPanel({ provider, kinds, onSaved }: Props) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(provider));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const patch = (next: Partial<Draft>) => {
    setDraft((current) => ({ ...current, ...next }));
    setSavedAt(null);
  };

  const numbers = useMemo(
    () => ({
      temperature: Number(draft.temperature),
      max_tokens: Number(draft.max_tokens),
      context_window: Number(draft.context_window),
    }),
    [draft.temperature, draft.max_tokens, draft.context_window],
  );

  const invalidNumber =
    !Number.isFinite(numbers.temperature) ||
    numbers.temperature < 0 ||
    numbers.temperature > 2 ||
    !Number.isInteger(numbers.max_tokens) ||
    numbers.max_tokens < 1 ||
    !Number.isInteger(numbers.context_window) ||
    numbers.context_window < 1;

  const changes = useMemo<ProviderPatch>(() => {
    const next: ProviderPatch = {};
    if (draft.kind !== provider.kind) next.kind = draft.kind.trim();
    if (draft.base_url.trim() !== provider.base_url) next.base_url = draft.base_url.trim();
    if (draft.model.trim() !== provider.model) next.model = draft.model.trim();
    if (draft.api_key) next.api_key = draft.api_key;
    if (!invalidNumber) {
      if (numbers.temperature !== provider.temperature) next.temperature = numbers.temperature;
      if (numbers.max_tokens !== provider.max_tokens) next.max_tokens = numbers.max_tokens;
      if (numbers.context_window !== provider.context_window) next.context_window = numbers.context_window;
    }
    if (draft.supports_tools !== provider.supports_tools) next.supports_tools = draft.supports_tools;
    if (draft.supports_vision !== provider.supports_vision) next.supports_vision = draft.supports_vision;
    if (draft.enabled !== provider.enabled) next.enabled = draft.enabled;
    return next;
  }, [draft, invalidNumber, numbers, provider]);

  const dirty = Object.keys(changes).length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!dirty || busy || invalidNumber) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await updateProvider(provider.id, changes);
      onSaved(saved);
      setDraft(draftOf(saved));
      setSavedAt(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[14px] font-medium text-text">{provider.name}</span>
          <KindBadge kind={provider.kind} />
        </div>
        <span className="font-mono text-[10px] text-faint">PATCH /providers/{provider.id.slice(0, 8)}</span>
      </div>
      <p className="-mt-1 text-[11px] leading-relaxed text-faint">
        名称由后端唯一约束，当前版本不支持修改；API Key 只写不读，保存后仅返回掩码提示。
      </p>

      <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
        <Field label="类型">
          <select
            value={kinds.includes(draft.kind) ? draft.kind : "__custom"}
            onChange={(event) => {
              if (event.target.value !== "__custom") patch({ kind: event.target.value });
            }}
            disabled={!kinds.includes(draft.kind)}
            className="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[12px] text-text focus:border-brand/60 focus:outline-none disabled:opacity-60"
          >
            {kinds.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
            {!kinds.includes(draft.kind) && <option value="__custom">{draft.kind}</option>}
          </select>
        </Field>
        <Field label="默认模型">
          <Input
            value={draft.model}
            onChange={(event) => patch({ model: event.target.value })}
            aria-label="默认模型"
            spellCheck={false}
            autoComplete="off"
            className="font-mono text-[12px]"
            maxLength={200}
          />
        </Field>
      </div>

      <Field label="接入点 BASE_URL">
        <Input
          value={draft.base_url}
          onChange={(event) => patch({ base_url: event.target.value })}
          aria-label="接入点"
          spellCheck={false}
          autoComplete="off"
          className="font-mono text-[12px]"
          maxLength={500}
        />
      </Field>

      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
        <Field label="温度 0-2">
          <Input
            value={draft.temperature}
            onChange={(event) => patch({ temperature: event.target.value })}
            aria-label="温度"
            inputMode="decimal"
            className="font-mono text-[12px]"
          />
        </Field>
        <Field label="最大输出 TOKENS">
          <Input
            value={draft.max_tokens}
            onChange={(event) => patch({ max_tokens: event.target.value })}
            aria-label="最大输出 tokens"
            inputMode="numeric"
            className="font-mono text-[12px]"
          />
        </Field>
        <Field label="上下文窗口">
          <Input
            value={draft.context_window}
            onChange={(event) => patch({ context_window: event.target.value })}
            aria-label="上下文窗口"
            inputMode="numeric"
            className="font-mono text-[12px]"
          />
        </Field>
      </div>

      <Field label={provider.api_key_hint ? `API KEY（当前 ${provider.api_key_hint}，留空则不修改）` : "API KEY（未设置）"}>
        <Input
          type="password"
          value={draft.api_key}
          onChange={(event) => patch({ api_key: event.target.value })}
          placeholder={provider.api_key_hint ? `替换为新的 Key · 现有 ${provider.api_key_hint}` : "sk-…"}
          aria-label="API Key"
          autoComplete="new-password"
          spellCheck={false}
          className="font-mono text-[12px]"
        />
      </Field>

      <div className="flex flex-wrap items-center gap-4 border-t border-line pt-3">
        <Toggle
          checked={draft.supports_tools}
          onChange={(next) => patch({ supports_tools: next })}
          label="支持工具调用"
        />
        <Toggle
          checked={draft.supports_vision}
          onChange={(next) => patch({ supports_vision: next })}
          label="支持视觉输入"
        />
        <Toggle checked={draft.enabled} onChange={(next) => patch({ enabled: next })} label="启用" />
      </div>

      {invalidNumber && <Notice tone="warn">温度需为 0-2 的数字，tokens 与上下文窗口需为正整数。</Notice>}
      {error && <Notice>{error}</Notice>}

      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" variant="primary" disabled={!dirty || busy || invalidNumber}>
          {busy ? (
            <span className="flex items-center gap-1.5">
              <Spinner />
              保存中
            </span>
          ) : dirty ? (
            `保存变更（${Object.keys(changes).length}）`
          ) : (
            "已保存"
          )}
        </Button>
        {dirty && (
          <Button
            type="button"
            onClick={() => {
              setDraft(draftOf(provider));
              setError(null);
            }}
          >
            撤销修改
          </Button>
        )}
        {savedAt && <span className="font-mono text-[10px] text-accent">已于 {savedAt} 保存</span>}
      </div>
    </form>
  );
}
