"use client";

import { useCallback, useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { Button, Notice, Spinner, StateLine } from "@/components/projects/ui";
import {
  deleteProvider,
  errorMessage,
  fetchLedger,
  fetchPresets,
  fetchProviders,
  fetchUsage,
  testProvider,
  type Ledger,
  type PresetsResponse,
  type Provider,
  type TestResult,
  type UsageSummary,
} from "./api";
import { StatusLine as TestSummary } from "./bits";
import { LedgerPanel } from "./ledger-panel";
import { ProviderCreateForm } from "./provider-create-form";
import { type TestState } from "./provider-list";
import { ProviderEditPanel } from "./provider-edit-panel";
import { SelectPopover } from "../ui/select-popover";
import { StreamDemo } from "./stream-demo";
import { UsagePanel } from "./usage-panel";

/** 供应商 key → 展示名。下拉里显示「中文名 · 型号」，比裸英文键好认。 */
const PROVIDER_NAMES: Record<string, string> = {
  deepseek: "DeepSeek",
  qwen: "通义千问",
  zhipu: "智谱 GLM",
  moonshot: "Kimi",
  anthropic: "Claude",
  openai: "OpenAI",
  ollama: "Ollama 本地",
};

export function ProvidersView() {
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [presets, setPresets] = useState<PresetsResponse | null>(null);
  const [presetError, setPresetError] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [ledgerError, setLedgerError] = useState<string | null>(null);
  const [ledgerDays, setLedgerDays] = useState(7);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});

  const refreshLedger = useCallback(
    (days: number) => {
      return fetchLedger(days)
        .then((rows) => {
          setLedger(rows);
          setLedgerError(null);
        })
        .catch((caught: unknown) => setLedgerError(errorMessage(caught)));
    },
    [],
  );

  useEffect(() => {
    void refreshLedger(ledgerDays);
  }, [ledgerDays, refreshLedger]);

  const refreshUsage = useCallback(() => {
    return fetchUsage()
      .then((summary) => {
        setUsage(summary);
        setUsageError(null);
      })
      .catch((caught: unknown) => setUsageError(errorMessage(caught)));
  }, []);

  const refresh = useCallback(() => {
    return fetchProviders()
      .then((rows) => {
        setProviders(rows);
        setListError(null);
        setSelectedId((current) =>
          current !== null && rows.some((provider) => provider.id === current) ? current : (rows[0]?.id ?? null),
        );
      })
      .catch((caught: unknown) => {
        setProviders((current) => current ?? []);
        setListError(errorMessage(caught));
      });
  }, []);

  useEffect(() => {
    void refresh();
    void refreshUsage();
    fetchPresets()
      .then(setPresets)
      .catch((caught: unknown) => setPresetError(errorMessage(caught)));
  }, [refresh, refreshUsage]);


  const runTest = useCallback(
    async (provider: Provider) => {
      setTests((current) => ({ ...current, [provider.id]: { status: "running" } }));
      try {
        const result: TestResult = await testProvider(provider.id);
        setTests((current) => ({ ...current, [provider.id]: { status: "done", result } }));
      } catch (caught) {
        setTests((current) => ({ ...current, [provider.id]: { status: "failed", message: errorMessage(caught) } }));
      } finally {
        void refreshUsage();
      }
    },
    [refreshUsage],
  );

  const removeProvider = useCallback(
    async (provider: Provider) => {
      try {
        await deleteProvider(provider.id);
        setTests((current) => {
          const next = { ...current };
          delete next[provider.id];
          return next;
        });
        await refresh();
        void refreshUsage();
        return true;
      } catch (caught) {
        setTests((current) => ({ ...current, [provider.id]: { status: "failed", message: errorMessage(caught) } }));
        return false;
      }
    },
    [refresh, refreshUsage],
  );

  const selected = providers?.find((provider) => provider.id === selectedId) ?? null;
  const selectedTest = selected ? tests[selected.id] : undefined;
  // 只数在用的：列表里那三个停用的不该算进「共 N 个」。
  const count = providers?.filter((provider) => provider.enabled).length ?? 0;
  // 配了 Key 才算能真正发请求。全为空时界面必须直说，否则用户会以为"模型很蠢"，
  // 实际是一次调用都没成功过（密钥为空 → 401，读起来却像模型不干活）。
  const readyCount = providers?.filter((provider) => provider.enabled && provider.api_key_hint).length ?? 0;
  const noKey = providers !== null && readyCount === 0;

  return (
    <div className="flex flex-col gap-4">
      <Panel title="模型" hint={providers === null ? "载入中" : `共 ${count} 家`}>
        <div className="flex flex-col gap-3">
          <p className="text-[12px] leading-relaxed text-faint">
            选一个模型，填上 API Key 就能用。接入点和型号已经配好，通常只需要填 Key。
          </p>
          {noKey && (
            <Notice tone="warn">
              还没有可用模型：这些都没填 API Key，发消息会直接失败。在下面填一个 Key 即可。
            </Notice>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <SelectPopover
              ariaLabel="选择模型"
              value={selectedId ?? ""}
              placeholder={providers === null ? "载入中…" : "选择模型"}
              options={(providers ?? []).map((provider) => ({
                value: provider.id,
                label: `${PROVIDER_NAMES[provider.name] ?? provider.name} · ${provider.model}`,
                description: provider.api_key_hint ? "已填 Key，可直接使用" : "还没填 Key",
              }))}
              onChange={(value) => {
                setSelectedId(value);
                setConfirmDelete(false);
              }}
              triggerClassName="min-w-[22rem]"
              menuMinWidth={360}
              disabled={providers === null || (providers ?? []).length === 0}
            />
            <Button onClick={() => setCreating((current) => !current)}>
              {creating ? "收起" : "添加其他厂商"}
            </Button>
            <Button onClick={() => void refresh()}>刷新</Button>
            {selected && (
              <Button variant="danger" onClick={() => setConfirmDelete((current) => !current)}>
                删除
              </Button>
            )}
          </div>

          {confirmDelete && selected && (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-danger/25 bg-danger/[0.04] px-3 py-2">
              <span className="text-[13px] text-muted">删除「{selected.name}」及其用量记录？</span>
              <div className="flex gap-2">
                <Button onClick={() => setConfirmDelete(false)}>取消</Button>
                <Button
                  variant="danger"
                  onClick={async () => {
                    const ok = await removeProvider(selected);
                    if (ok) setConfirmDelete(false);
                  }}
                >
                  确认删除
                </Button>
              </div>
            </div>
          )}

          {listError && (
            <div className="flex items-center gap-2">
              <Notice>{listError}</Notice>
              <Button className="self-start" onClick={() => void refresh()}>重试</Button>
            </div>
          )}
          {presetError && <Notice>预设读取失败：{presetError}</Notice>}

          {creating && presets && (
            <ProviderCreateForm
              presets={presets.presets}
              existingNames={(providers ?? []).map((provider) => provider.name)}
              onCreated={(provider) => {
                setCreating(false);
                setSelectedId(provider.id);
                void refresh();
              }}
              onCancel={() => setCreating(false)}
            />
          )}
          {creating && !presets && (
            <div className="flex items-center gap-2 border-b border-line pb-3 font-mono text-[12px] text-faint">
              <Spinner />
              读取预设列表…
            </div>
          )}
        </div>
      </Panel>

      <Panel title="配置" hint={selected ? "修改会立即保存到本机" : "先在上面选一个模型"}>
        {selected ? (
          <div className="flex flex-col gap-4">
            <ProviderEditPanel
              key={selected.id}
              provider={selected}
              kinds={presets?.kinds ?? ["openai_compatible", "anthropic", "ollama"]}
              onSaved={(saved) =>
                setProviders((current) => (current ? current.map((row) => (row.id === saved.id ? saved : row)) : current))
              }
            />
            <div className="flex flex-col gap-2 border-t border-line pt-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[11px] tracking-wide text-faint uppercase">连接测试</span>
                <Button
                  className="self-start"
                  disabled={selectedTest?.status === "running"}
                  onClick={() => void runTest(selected)}
                >
                  {selectedTest?.status === "running" ? (
                    <span className="flex items-center gap-1.5">
                      <Spinner />
                      测试中
                    </span>
                  ) : (
                    "测试连接"
                  )}
                </Button>
              </div>
              {selectedTest?.status === "done" && (
                <div className="rounded-lg border border-line bg-surface px-3 py-2.5">
                  <TestSummary result={selectedTest.result} />
                </div>
              )}
              {selectedTest?.status === "failed" && <Notice>{selectedTest.message}</Notice>}
              {!selectedTest && (
                <p className="text-[12px] leading-relaxed text-faint">
                  未保存的修改不会参与测试；测试会记录一次调用与 Token 用量。
                </p>
              )}
            </div>
          </div>
        ) : providers === null ? (
          <StateLine>载入供应商…</StateLine>
        ) : (
          <StateLine>还没有模型供应商。点上方「添加其他厂商」手动建一个。</StateLine>
        )}
      </Panel>

      {/* 调试与用量是低频查看项，收进折叠区，别和配置挤在一起抢首屏。 */}
      <details className="rounded-lg border border-line bg-surface">
        <summary className="flex cursor-pointer items-center gap-2 px-4 py-2.5 text-[14px] font-medium tracking-tight">
          用量与调试
          <span className="text-[12px] font-normal text-faint">· 流式演示、Token 用量与花费归因</span>
        </summary>
        <div className="flex flex-col gap-4 border-t border-line p-4">
          <Panel title="Token 用量" hint={usage ? `共 ${usage.total_calls} 次调用` : "载入中"}>
            <UsagePanel summary={usage} error={usageError} onRetry={() => void refreshUsage()} />
          </Panel>
          <Panel title="花得值不值" hint="按任务把 token 归到产物 / 判断 / 废纸">
            <LedgerPanel
              ledger={ledger}
              error={ledgerError}
              days={ledgerDays}
              onDays={setLedgerDays}
              onRetry={() => void refreshLedger(ledgerDays)}
            />
          </Panel>
          <Panel title="流式输出演示" hint="选一个供应商，实时看模型逐字返回">
            <StreamDemo providers={providers ?? []} onFinished={refreshUsage} />
          </Panel>
        </div>
      </details>
    </div>
  );
}
