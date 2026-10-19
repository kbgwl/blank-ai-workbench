"use client";

import { useState, type FormEvent } from "react";
import { Button, Field, Input, Notice } from "@/components/projects/ui";
import { createProvider, errorMessage, type Preset, type Provider } from "./api";
import { Toggle } from "./bits";

type Props = {
  presets: Record<string, Preset>;
  existingNames: string[];
  onCreated: (provider: Provider) => void;
  onCancel: () => void;
};

type Draft = {
  presetKey: string;
  name: string;
  kind: string;
  base_url: string;
  model: string;
  api_key: string;
  temperature: string;
  max_tokens: string;
  context_window: string;
  reasoning_effort: string;
  verbosity: string;
  total_params_b: string;
  active_params_b: string;
  supports_tools: boolean;
  supports_vision: boolean;
  enabled: boolean;
};

const presetLabels: Record<string, string> = {
  openai: "OpenAI",
  openai_responses: "OpenAI Responses",
  deepseek: "DeepSeek",
  qwen: "通义千问",
  anthropic: "Claude",
  ollama: "Ollama",
};

/** 推理档位：空串表示不下发 reasoning 字段，按普通模型走 temperature。 */
const effortOptions = ["", "minimal", "low", "medium", "high"];
/** 输出详略：仅 OpenAI Responses 支持，空串表示不下发。 */
const verbosityOptions = ["", "low", "medium", "high"];

const emptyDraft: Draft = {
  presetKey: "",
  name: "",
  kind: "openai_compatible",
  base_url: "",
  model: "",
  api_key: "",
  temperature: "0.2",
  max_tokens: "2048",
  context_window: "128000",
  reasoning_effort: "",
  verbosity: "",
  total_params_b: "",
  active_params_b: "",
  supports_tools: false,
  supports_vision: false,
  enabled: true,
};

function toDraft(key: string, preset: Preset): Draft {
  return {
    ...emptyDraft,
    presetKey: key,
    name: presetLabels[key] ?? key,
    kind: preset.kind,
    base_url: preset.base_url,
    model: preset.model,
    supports_tools: Boolean(preset.supports_tools),
    supports_vision: Boolean(preset.supports_vision),
    // Responses 端点默认开推理档位，其余保持普通温度采样
    reasoning_effort: preset.kind === "openai_responses" ? "medium" : "",
  };
}

export function ProviderCreateForm({ presets, existingNames, onCreated, onCancel }: Props) {
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const patch = (next: Partial<Draft>) => setDraft((current) => ({ ...current, ...next }));

  const numbers = {
    temperature: Number(draft.temperature),
    max_tokens: Number(draft.max_tokens),
    context_window: Number(draft.context_window),
  };
  const invalidNumber =
    !Number.isFinite(numbers.temperature) ||
    numbers.temperature < 0 ||
    numbers.temperature > 2 ||
    !Number.isInteger(numbers.max_tokens) ||
    numbers.max_tokens < 1 ||
    !Number.isInteger(numbers.context_window) ||
    numbers.context_window < 1;
  // 参数量可以为空（当成"未填"），但不能是负数；激活参数也不可能大于总参数，
  // 那条基本能把"把两个输入框填反了"当场抓住。
  const params = {
    total: draft.total_params_b.trim() === "" ? 0 : Number(draft.total_params_b),
    active: draft.active_params_b.trim() === "" ? 0 : Number(draft.active_params_b),
  };
  const invalidParams =
    !Number.isFinite(params.total) ||
    params.total < 0 ||
    !Number.isFinite(params.active) ||
    params.active < 0 ||
    (params.total > 0 && params.active > params.total);
  const nameTaken = Boolean(draft.name.trim()) && existingNames.includes(draft.name.trim());
  const canSubmit = !busy && draft.name.trim().length > 0 && draft.base_url.trim().length > 0 && draft.model.trim().length > 0 && !invalidNumber && !invalidParams && !nameTaken;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createProvider({
        name: draft.name.trim(),
        kind: draft.kind,
        base_url: draft.base_url.trim(),
        api_key: draft.api_key,
        model: draft.model.trim(),
        temperature: numbers.temperature,
        max_tokens: numbers.max_tokens,
        context_window: numbers.context_window,
        supports_tools: draft.supports_tools,
        supports_vision: draft.supports_vision,
        enabled: draft.enabled,
        reasoning_effort: draft.reasoning_effort,
        verbosity: draft.verbosity,
        total_params_b: params.total,
        active_params_b: params.active,
      });
      onCreated(created);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 border-b border-line pb-4">
      <div className="flex flex-wrap gap-1.5">
        {Object.entries(presets).map(([key, preset]) => {
          const active = draft.presetKey === key;
          return (
            <button
              key={key}
              type="button"
              onClick={() => patch(toDraft(key, preset))}
              className={`rounded-md border px-2 py-1 text-[11px] transition-colors ${
                active
                  ? "border-brand/50 bg-brand/15 text-brand"
                  : "border-line bg-surface-2/60 text-muted hover:border-line-strong hover:text-text"
              }`}
            >
              {presetLabels[key] ?? key}
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => patch({ ...emptyDraft, presetKey: "" })}
          className={`rounded-md border px-2 py-1 text-[11px] transition-colors ${
            draft.presetKey === ""
              ? "border-brand/50 bg-brand/15 text-brand"
              : "border-line bg-surface-2/60 text-muted hover:border-line-strong hover:text-text"
          }`}
        >
          自定义
        </button>
      </div>

      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <Field label="名称">
          <Input
            value={draft.name}
            onChange={(event) => patch({ name: event.target.value })}
            placeholder="例如 DeepSeek 主账号"
            aria-label="供应商名称"
            maxLength={100}
          />
        </Field>
        <Field label="类型">
          <Input
            value={draft.kind}
            onChange={(event) => patch({ kind: event.target.value })}
            aria-label="供应商类型"
            spellCheck={false}
            className="font-mono text-[12px]"
            maxLength={40}
          />
        </Field>
      </div>

      <Field label="接入点 BASE_URL">
        <Input
          value={draft.base_url}
          onChange={(event) => patch({ base_url: event.target.value })}
          placeholder="https://api.deepseek.com/v1"
          aria-label="接入点"
          spellCheck={false}
          autoComplete="off"
          className="font-mono text-[12px]"
          maxLength={500}
        />
      </Field>

      <Field label="默认模型">
        <Input
          value={draft.model}
          onChange={(event) => patch({ model: event.target.value })}
          placeholder="deepseek-chat"
          aria-label="默认模型"
          spellCheck={false}
          autoComplete="off"
          className="font-mono text-[12px]"
          maxLength={200}
        />
      </Field>

      <Field label="API KEY（仅写入，不会回显）">
        <Input
          type="password"
          value={draft.api_key}
          onChange={(event) => patch({ api_key: event.target.value })}
          placeholder="留空表示稍后再填"
          aria-label="API Key"
          autoComplete="new-password"
          spellCheck={false}
          className="font-mono text-[12px]"
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

      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <Field label="推理档位 reasoning_effort">
          <select
            value={draft.reasoning_effort}
            onChange={(event) => patch({ reasoning_effort: event.target.value })}
            aria-label="推理档位"
            className="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[12px] text-text focus:border-brand/60 focus:outline-none"
          >
            {effortOptions.map((value) => (
              <option key={value || "off"} value={value}>
                {value || "不启用"}
              </option>
            ))}
          </select>
        </Field>
        <Field label="输出详略 verbosity">
          <select
            value={draft.verbosity}
            onChange={(event) => patch({ verbosity: event.target.value })}
            aria-label="输出详略"
            className="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[12px] text-text focus:border-brand/60 focus:outline-none"
          >
            {verbosityOptions.map((value) => (
              <option key={value || "off"} value={value}>
                {value || "不启用"}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {draft.reasoning_effort !== "" && (
        <Notice tone="muted">
          已启用推理档位：请求不再下发 temperature（推理模型带温度会直接 400），改为回传推理内容供多轮续推。
          verbosity 仅 OpenAI Responses 端点生效，其他接入点会忽略。
        </Notice>
      )}

      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        <Field label="总参数（十亿 B）">
          <Input
            value={draft.total_params_b}
            onChange={(event) => patch({ total_params_b: event.target.value })}
            placeholder="671，留空表示未填"
            aria-label="总参数"
            inputMode="decimal"
            className="font-mono text-[12px]"
          />
        </Field>
        <Field label="激活参数（十亿 B）">
          <Input
            value={draft.active_params_b}
            onChange={(event) => patch({ active_params_b: event.target.value })}
            placeholder="37，稠密模型留空"
            aria-label="激活参数"
            inputMode="decimal"
            className="font-mono text-[12px]"
          />
        </Field>
      </div>

      <p className="-mt-1 text-[11px] leading-relaxed text-faint">
        总参数决定权重占多少显存（能不能塞进这台机器），激活参数决定每个 token 走多少算力（跑多快、多贵）。
        稀疏模型两者差一个数量级（如 671B / 37B），填对了成本预估才有意义；稠密模型只填总参数即可。
      </p>

      <div className="flex flex-wrap items-center gap-4">
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
      {invalidParams && <Notice tone="warn">参数量需为非负数字，且激活参数不能大于总参数。</Notice>}
      {nameTaken && <Notice tone="warn">名称「{draft.name.trim()}」已存在，供应商名称需唯一。</Notice>}
      {error && <Notice>{error}</Notice>}

      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={!canSubmit}>
          {busy ? "创建中" : "创建供应商"}
        </Button>
        <Button type="button" onClick={onCancel}>
          取消
        </Button>
      </div>
    </form>
  );
}
