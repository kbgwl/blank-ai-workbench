"use client";

import { useCallback, useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { Button, Notice, Spinner, StateLine } from "@/components/projects/ui";
import {
  deleteProvider,
  errorMessage,
  fetchPresets,
  fetchProviders,
  fetchUsage,
  testProvider,
  type PresetsResponse,
  type Provider,
  type TestResult,
  type UsageSummary,
} from "./api";
import { StatusLine as TestSummary } from "./bits";
import { ProviderCreateForm } from "./provider-create-form";
import { ProviderList, type TestState } from "./provider-list";
import { ProviderEditPanel } from "./provider-edit-panel";
import { StreamDemo } from "./stream-demo";
import { UsagePanel } from "./usage-panel";

export function ProvidersView() {
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [presets, setPresets] = useState<PresetsResponse | null>(null);
  const [presetError, setPresetError] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});

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
  const count = providers?.length ?? 0;

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4 xl:grid-cols-[minmax(0,21rem)_minmax(0,1fr)]">
      <div className="flex min-w-0 flex-col gap-4">
        <Panel
          title="模型供应商"
          hint={providers === null ? "载入中" : `共 ${count} 个 · /api/v1/providers`}
        >
          <div className="flex flex-col gap-3">
            <div className="flex gap-2">
              <Button
                variant="primary"
                className="self-start"
                onClick={() => setCreating((current) => !current)}
              >
                {creating ? "收起新建表单" : "新建供应商"}
              </Button>
              <Button className="self-start" onClick={() => void refresh()}>
                刷新
              </Button>
            </div>

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
              <div className="flex items-center gap-2 border-b border-line pb-3 font-mono text-[11px] text-faint">
                <Spinner />
                读取预设列表…
              </div>
            )}
            {presetError && <Notice>预设读取失败：{presetError}</Notice>}

            <ProviderList
              providers={providers}
              error={listError}
              selectedId={selectedId}
              tests={tests}
              onSelect={setSelectedId}
              onTest={(provider) => void runTest(provider)}
              onDelete={removeProvider}
              onRetry={() => void refresh()}
            />
          </div>
        </Panel>
      </div>

      <div className="flex min-w-0 flex-col gap-4">
        <Panel title="供应商配置" hint={selected ? "PATCH /api/v1/providers/:id" : "未选择供应商"}>
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
                  <span className="font-mono text-[10px] tracking-wide text-faint uppercase">连接测试</span>
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
                  <div className="rounded-lg border border-line bg-surface/60 px-3 py-2.5">
                    <TestSummary result={selectedTest.result} />
                  </div>
                )}
                {selectedTest?.status === "failed" && <Notice>{selectedTest.message}</Notice>}
                {!selectedTest && (
                  <p className="text-[11px] leading-relaxed text-faint">
                    未保存的修改不会参与测试；测试会记录一次调用与 Token 用量。
                  </p>
                )}
              </div>
            </div>
          ) : providers === null ? (
            <StateLine>载入供应商…</StateLine>
          ) : (
            <StateLine>在左侧选择一个供应商，或点「新建供应商」</StateLine>
          )}
        </Panel>

        <Panel title="流式输出演示" hint="POST /api/v1/providers/:id/chat/stream">
          <StreamDemo providers={providers ?? []} onFinished={refreshUsage} />
        </Panel>

        <Panel title="Token 用量" hint={usage ? `共 ${usage.total_calls} 次调用` : "载入中"}>
          <UsagePanel summary={usage} error={usageError} onRetry={() => void refreshUsage()} />
        </Panel>
      </div>
    </div>
  );
}
