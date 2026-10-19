"use client";

import { useState } from "react";
import { Button, IconButton, Notice, Spinner, StateLine, TrashIcon } from "@/components/projects/ui";
import type { Provider, TestResult } from "./api";
import { KindBadge, ParamBadge } from "./bits";

export type TestState =
  | { status: "running" }
  | { status: "done"; result: TestResult }
  | { status: "failed"; message: string };

type Props = {
  providers: Provider[] | null;
  error: string | null;
  selectedId: string | null;
  tests: Record<string, TestState>;
  onSelect: (id: string) => void;
  onTest: (provider: Provider) => void;
  onDelete: (provider: Provider) => Promise<boolean>;
  onRetry: () => void;
};

export function ProviderList({ providers, error, selectedId, tests, onSelect, onTest, onDelete, onRetry }: Props) {
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function confirmDelete(provider: Provider) {
    setDeletingId(provider.id);
    setDeleteError(null);
    const ok = await onDelete(provider);
    setDeletingId(null);
    if (ok) setConfirmId(null);
    else setDeleteError("删除失败，请稍后重试。");
  }

  if (providers === null) return <StateLine>载入供应商…</StateLine>;

  return (
    <div className="flex flex-col gap-2">
      {error && (
        <div className="flex flex-col gap-2">
          <Notice>供应商列表加载失败：{error}</Notice>
          <Button className="self-start" onClick={onRetry}>
            重试
          </Button>
        </div>
      )}

      {providers.length === 0 && !error ? (
        <StateLine>还没有模型供应商 · 点上方「新建供应商」从预设创建</StateLine>
      ) : (
        <ul className="flex flex-col gap-2">
          {providers.map((provider) => {
            const selected = provider.id === selectedId;
            const state = tests[provider.id];
            return (
              <li
                key={provider.id}
                className={`rounded-xl border transition-colors ${
                  selected ? "border-brand/40 bg-surface-2" : "border-line bg-surface/60 hover:border-line-strong"
                }`}
              >
                <div className="flex items-start gap-2 p-3">
                  <button type="button" onClick={() => onSelect(provider.id)} className="min-w-0 flex-1 text-left">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className={`truncate text-[13px] font-medium ${selected ? "text-brand" : "text-text"}`}>
                        {provider.name}
                      </span>
                      <KindBadge kind={provider.kind} />
                      <ParamBadge total={provider.total_params_b ?? 0} active={provider.active_params_b ?? 0} />
                      {!provider.enabled && (
                        <span className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">
                          已停用
                        </span>
                      )}
                    </span>
                    <span className="mt-1 block truncate font-mono text-[11px] text-muted">{provider.model}</span>
                    <span className="mt-0.5 block truncate font-mono text-[10px] text-faint">
                      {provider.base_url}
                      {provider.api_key_hint ? ` · Key ${provider.api_key_hint}` : " · 未设置 Key"}
                    </span>
                  </button>
                  <IconButton
                    label="删除"
                    tone="danger"
                    onClick={() => {
                      setDeleteError(null);
                      setConfirmId(confirmId === provider.id ? null : provider.id);
                    }}
                  >
                    <TrashIcon />
                  </IconButton>
                </div>

                <div className="flex items-center gap-2 px-3 pb-3">
                  <Button
                    onClick={() => onTest(provider)}
                    disabled={state?.status === "running"}
                    className="h-7 px-2.5 py-1 text-[11px]"
                  >
                    {state?.status === "running" ? (
                      <span className="flex items-center gap-1.5">
                        <Spinner />
                        测试中
                      </span>
                    ) : (
                      "测试连接"
                    )}
                  </Button>
                  {state?.status === "done" && (
                    <span
                      className={`font-mono text-[10px] ${state.result.ok ? "text-accent" : "text-danger"}`}
                    >
                      {state.result.ok
                        ? `成功 ${state.result.latency_ms ?? 0}ms`
                        : (state.result.message ?? "连接失败")}
                    </span>
                  )}
                  {state?.status === "failed" && <span className="font-mono text-[10px] text-danger">{state.message}</span>}
                </div>

                {confirmId === provider.id && (
                  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line bg-danger/[0.04] px-3 py-2">
                    <span className="text-[12px] text-muted">删除「{provider.name}」及其用量记录？</span>
                    <div className="flex gap-2">
                      <Button onClick={() => setConfirmId(null)}>取消</Button>
                      <Button variant="danger" disabled={deletingId === provider.id} onClick={() => void confirmDelete(provider)}>
                        {deletingId === provider.id ? "删除中" : "确认删除"}
                      </Button>
                    </div>
                  </div>
                )}
                {confirmId === provider.id && deleteError && (
                  <div className="border-t border-line px-3 py-2">
                    <Notice>{deleteError}</Notice>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
