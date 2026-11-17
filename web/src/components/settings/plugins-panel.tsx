"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import { fetchProjects, type Project } from "@/components/projects/api";
import { errorMessage, fetchProjectPlugins, type ProjectPlugin, type ProjectPlugins } from "./api";

/**
 * 插件包（.claude-plugin/plugin.json）。
 *
 * 这一屏只摊开后端的判定：哪些包被发现了、各自贡献了几个组件、当前是否启用、
 * 以及哪些段（hooks / MCP）被解析但不会加载。组件正文不进接口——任务启动时
 * 才读，这里给数量就够了。
 */

const COMPONENT_LABELS: Record<string, string> = {
  commands: "命令",
  agents: "子代理",
  skills: "技能",
  "output-styles": "输出样式",
};

const IGNORED_LABELS: Record<string, string> = {
  hooks: "钩子",
  mcpServers: "MCP 服务端",
  lspServers: "LSP 服务端",
};

function sourceLabel(source: string): string {
  if (source === "project") return "项目";
  if (source === "user") return "用户";
  return source || "未知";
}

function PluginRow({ plugin }: { plugin: ProjectPlugin }) {
  const components = Object.entries(plugin.components ?? {});
  return (
    <div className="border-b border-line py-2.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[13px] text-text">{plugin.name}</span>
        <span
          className={`rounded border px-1 py-0.5 text-[11px] ${
            plugin.enabled
              ? "border-brand/40 bg-brand/10 text-brand"
              : "border-line bg-surface-2 text-faint line-through"
          }`}
        >
          {plugin.enabled ? "已启用" : "已停用"}
        </span>
        <span className="rounded border border-line bg-surface-2 px-1 py-0.5 text-[11px] text-faint">
          {sourceLabel(plugin.source)}层
        </span>
        {plugin.version && (
          <span className="font-mono text-[11px] text-faint">v{plugin.version}</span>
        )}
        {components.map(([name, count]) => (
          <span
            key={name}
            className="rounded border border-line bg-surface-2 px-1 py-0.5 font-mono text-[11px] text-muted"
          >
            {COMPONENT_LABELS[name] ?? name} {count}
          </span>
        ))}
      </div>
      {plugin.description && (
        <p className="mt-1 text-[12px] leading-relaxed text-muted">{plugin.description}</p>
      )}
      {!plugin.enabled && plugin.defaultEnabled && (
        // 默认启用却被显式关掉：说清楚是被设置关的，不是清单坏了
        <p className="mt-1 text-[11px] leading-relaxed text-faint">
          清单默认启用，被设置里的 enabledPlugins 关掉了。
        </p>
      )}
      {plugin.ignored?.map((item) => (
        <p key={item.section} className="mt-1 font-mono text-[11px] leading-relaxed break-words text-faint">
          未加载 {IGNORED_LABELS[item.section] ?? item.section}：{item.reason}
        </p>
      ))}
      {plugin.warnings?.map((warning) => (
        <p key={warning} className="mt-1 font-mono text-[12px] leading-relaxed break-words text-warn">
          ⚠ {warning}
        </p>
      ))}
      <p className="mt-1 font-mono text-[11px] break-all text-faint">{plugin.path}</p>
    </div>
  );
}

export function PluginsPanel() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState("");
  const [projectsError, setProjectsError] = useState("");
  // 结果与项目一起存：换项目时旧项目的数据立刻失效，不必在 effect 里同步置空。
  const [result, setResult] = useState<{
    projectId: string;
    data: ProjectPlugins | null;
    error: string;
  } | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((rows) => {
        if (!alive) return;
        setProjects(rows);
        // 默认选第一个绑了工作区的项目：没有工作区就没有项目层可扫
        const bound = rows.find((item) => item.workspaces.length > 0) ?? rows[0];
        setProjectId((current) => current || bound?.id || "");
      })
      .catch((err) => {
        if (alive) setProjectsError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    fetchProjectPlugins(projectId)
      .then((next) => {
        if (alive) setResult({ projectId, data: next, error: "" });
      })
      .catch((err) => {
        if (alive) setResult({ projectId, data: null, error: errorMessage(err) });
      });
    return () => {
      alive = false;
    };
  }, [projectId, reloadToken]);

  const data = result?.projectId === projectId ? result.data : null;
  const error = projectsError || (result?.projectId === projectId ? result.error : "");

  const options = useMemo(
    () =>
      (projects ?? []).map((project) => ({
        value: project.id,
        label: project.name,
        description:
          project.workspaces.length > 0
            ? (project.workspaces.find((item) => item.is_active) ?? project.workspaces[0]).path
            : "未绑定工作区，只扫用户层插件",
      })),
    [projects],
  );

  const selected = projects?.find((project) => project.id === projectId) ?? null;
  const noWorkspace = selected !== null && selected.workspaces.length === 0;

  return (
    <Panel
      title="插件包（plugin.json）"
      hint={data ? `GET /projects/{id}/plugins · ${data.count} 个插件` : "GET /projects/{id}/plugins"}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-[12px] text-faint">工作区</span>
        <SelectPopover
          value={projectId}
          options={options}
          onChange={setProjectId}
          ariaLabel="选择插件所属项目"
          placeholder={projects === null ? "加载项目…" : "没有项目"}
          disabled={options.length === 0}
          triggerClassName="border border-line bg-surface"
        />
      </div>

      {error && <p className="mb-2 text-[13px] leading-relaxed text-danger/90">{error}</p>}

      {projects !== null && projects.length === 0 && (
        <p className="text-[13px] leading-relaxed text-muted">
          还没有项目。插件按工作区发现：先在「项目」页新建项目并绑定工作区，
          再来这里看插件贡献了哪些命令、子代理与技能。
        </p>
      )}

      {projectId && !data && !error && (
        <p className="text-[13px] text-faint">加载中…</p>
      )}

      {data && (
        <>
          {noWorkspace && (
            <p className="mb-2 text-[12px] leading-relaxed text-warn">
              该项目还没绑定工作区：只扫描了用户层插件，项目层目录不存在。
            </p>
          )}
          {data.count === 0 ? (
            <div className="space-y-2">
              <p className="text-[13px] leading-relaxed text-muted">
                这个工作区还没有插件。每个插件是一个目录，里面放一份清单：
              </p>
              <p className="rounded-md border border-line bg-surface-2 px-2.5 py-1.5 font-mono text-[12px] break-all text-faint">
                .claude-plugin/plugin.json
              </p>
              <pre className="overflow-x-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[12px] leading-relaxed text-muted">
{`{
  "name": "deploy-tools",
  "description": "部署相关命令",
  "version": "1.0.0",
  "commands": "./commands",
  "agents": "./agents",
  "skills": "./skills"
}`}
              </pre>
              <p className="text-[12px] leading-relaxed text-faint">
                组件路径必须从 <span className="font-mono">./</span> 起算；加载后会加
                <span className="font-mono"> 插件名: </span> 前缀，例如
                <span className="font-mono"> deploy-tools:reviewer</span>。默认启用；
                在设置里写 <span className="font-mono">&quot;enabledPlugins&quot;: {"{"}&quot;deploy-tools&quot;: false{"}"}</span> 可关掉。
              </p>
            </div>
          ) : (
            <>
              <p className="mb-1 text-[12px] leading-relaxed text-faint">
                {data.count} 个插件，{data.enabled.length} 个启用。插件只贡献提示词组件；
                清单里的 hooks 与 MCP 服务端会被解析但不加载，边界仍由设置页决定。
              </p>
              <div className="flex flex-col">
                {data.plugins.map((plugin) => (
                  <PluginRow key={`${plugin.source}:${plugin.name}`} plugin={plugin} />
                ))}
              </div>
            </>
          )}

          {data.errors.map((item) => (
            <p key={item} className="mt-1 font-mono text-[12px] leading-relaxed break-words text-danger/90">
              {item}
            </p>
          ))}
          {data.warnings.map((item) => (
            <p key={item} className="mt-1 font-mono text-[12px] leading-relaxed break-words text-warn">
              ⚠ {item}
            </p>
          ))}

          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-2">
            <div className="min-w-0 flex-1 space-y-0.5 font-mono text-[11px] break-all text-faint">
              <p>用户层：{data.userDir}</p>
              <p>项目层（工作区下）：{data.projectDir || "（项目未绑定工作区）"}</p>
            </div>
            <button
              type="button"
              onClick={() => setReloadToken((token) => token + 1)}
              className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 text-[12px] text-muted hover:border-line-strong"
            >
              重新扫描
            </button>
          </div>
        </>
      )}
    </Panel>
  );
}
