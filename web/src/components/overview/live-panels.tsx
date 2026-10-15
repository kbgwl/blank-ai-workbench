"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Panel } from "@/components/panel";
import { Button, Notice, StateLine } from "@/components/projects/ui";
import { activeWorkspace, fetchProjects, type Project } from "@/components/projects/api";
import { fetchProviders, fetchUsage, type Provider, type UsageSummary } from "@/components/settings/api";
import { errorMessage } from "@/lib/http";

export function LivePanels() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const load = useCallback(() => {
    void Promise.all([fetchProjects(), fetchProviders(), fetchUsage()])
      .then(([projectRows, providerRows, usageSummary]) => {
        setProjects(projectRows);
        setProviders(providerRows);
        setUsage(usageSummary);
        setError(null);
      })
      .catch((caught: unknown) => {
        setProjects((current) => current ?? []);
        setProviders((current) => current ?? []);
        setError(errorMessage(caught));
      });
  }, []);

  useEffect(() => {
    load();
  }, [load, nonce]);

  const bound = projects?.filter((project) => project.workspaces.length > 0).length ?? 0;
  const enabled = providers?.filter((provider) => provider.enabled).length ?? 0;
  const failed = usage?.providers.reduce((sum, row) => sum + row.failed, 0) ?? 0;
  const tokens =
    usage?.providers.reduce((sum, row) => sum + row.prompt_tokens + row.completion_tokens, 0) ?? 0;

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {error && (
        <div className="flex flex-col gap-2">
          <Notice>实时数据加载失败：{error}</Notice>
          <Button
            className="self-start"
            onClick={() => {
              setError(null);
              setNonce((value) => value + 1);
            }}
          >
            重试
          </Button>
        </div>
      )}

      <Panel title="项目" hint={projects === null ? "载入中" : `${projects.length} 个 · ${bound} 个已绑定`}>
        <ul className="flex flex-col gap-1.5">
          {projects === null ? (
            <StateLine>读取项目…</StateLine>
          ) : projects.length === 0 ? (
            <StateLine>还没有项目</StateLine>
          ) : (
            projects.slice(0, 4).map((project) => {
              const workspace = activeWorkspace(project);
              return (
                <li key={project.id} className="rounded-lg border border-line bg-surface/60 px-3 py-2">
                  <div className="truncate text-[13px] font-medium">{project.name}</div>
                  {workspace ? (
                    <div className="mt-0.5 truncate font-mono text-[10px] text-faint">{workspace.path}</div>
                  ) : (
                    <div className="mt-0.5 font-mono text-[10px] text-warn">未绑定工作区</div>
                  )}
                </li>
              );
            })
          )}
        </ul>
        <Link
          href="/projects"
          className="mt-3 inline-block font-mono text-[11px] text-brand hover:underline"
        >
          打开项目工作台 →
        </Link>
      </Panel>

      <Panel title="模型供应商" hint={providers === null ? "载入中" : `${providers.length} 个 · ${enabled} 个启用`}>
        <ul className="flex flex-col gap-1.5">
          {providers === null ? (
            <StateLine>读取供应商…</StateLine>
          ) : providers.length === 0 ? (
            <StateLine>还没有供应商</StateLine>
          ) : (
            providers.slice(0, 4).map((provider) => (
              <li key={provider.id} className="flex items-center gap-2 rounded-lg border border-line bg-surface/60 px-3 py-2">
                <span className={`size-1.5 shrink-0 rounded-full ${provider.enabled ? "bg-accent" : "bg-faint"}`} />
                <span className="min-w-0 flex-1 truncate text-[13px]">{provider.name}</span>
                <span className="shrink-0 truncate font-mono text-[10px] text-faint">{provider.model}</span>
              </li>
            ))
          )}
        </ul>
        <Link href="/settings" className="mt-3 inline-block font-mono text-[11px] text-brand hover:underline">
          前往模型设置 →
        </Link>
      </Panel>

      <Panel title="Token 用量" hint={usage === null ? "载入中" : `${usage.total_calls} 次调用`}>
        {usage === null ? (
          <StateLine>读取用量…</StateLine>
        ) : usage.total_calls === 0 ? (
          <StateLine>暂无调用记录</StateLine>
        ) : (
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="rounded-lg border border-line bg-surface/60 px-2 py-2">
              <div className="font-mono text-[10px] text-faint uppercase">调用</div>
              <div className="text-[15px] font-semibold">{usage.total_calls}</div>
            </div>
            <div className="rounded-lg border border-line bg-surface/60 px-2 py-2">
              <div className="font-mono text-[10px] text-faint uppercase">失败</div>
              <div className={`text-[15px] font-semibold ${failed > 0 ? "text-warn" : "text-text"}`}>{failed}</div>
            </div>
            <div className="rounded-lg border border-line bg-surface/60 px-2 py-2">
              <div className="font-mono text-[10px] text-faint uppercase">TOKENS</div>
              <div className="text-[15px] font-semibold">{tokens.toLocaleString("en-US")}</div>
            </div>
          </div>
        )}
        <Link href="/settings" className="mt-3 inline-block font-mono text-[11px] text-brand hover:underline">
          查看用量明细 →
        </Link>
      </Panel>
    </div>
  );
}
