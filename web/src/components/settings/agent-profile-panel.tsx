"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import { fetchProjects, type Project } from "@/components/projects/api";
import { errorMessage, fetchProjectAgents, type AgentCatalog } from "./api";
import { agentSourceLabel, agentToolsText, orderAgents } from "./agent-profile";

/**
 * 助手档案。
 *
 * 派活之前要能看清"这个助手能干什么"：它可用的工具、被禁止的工具、从哪一层来的。
 * 原来这些只在模型自己的上下文里，用户能看到的只有一个名字——
 * 名字正是最不能用来判断能力的东西。
 */
export function AgentProfilePanel() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState("");
  const [catalog, setCatalog] = useState<AgentCatalog | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((list) => {
        if (!alive) return;
        setProjects(list);
        const bound = list.find((item) => item.workspaces.length > 0);
        setProjectId((current) => current || bound?.id || "");
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    setCatalog(null);
    setError("");
    fetchProjectAgents(projectId)
      .then((data) => {
        if (alive) setCatalog(data);
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const bound = (projects ?? []).filter((item) => item.workspaces.length > 0);
  const agents = catalog ? orderAgents(catalog.agents) : [];

  return (
    <Panel
      title="助手档案"
      hint="派活之前，先看它能干什么"
      action={
        bound.length > 1 ? (
          <SelectPopover
            value={projectId}
            onChange={setProjectId}
            options={bound.map((item) => ({ value: item.id, label: item.name }))}
            ariaLabel="选择项目"
          />
        ) : undefined
      }
    >
      {error && <p className="text-[13px] text-danger">读取失败：{error}</p>}
      {!catalog && !error && <p className="font-mono text-[12px] text-faint">读取助手档案…</p>}

      {catalog && catalog.errors.length > 0 && (
        <div className="mb-2 rounded-md border border-danger/30 bg-danger/[0.05] px-2.5 py-2">
          <p className="text-[12px] font-medium text-danger">有 {catalog.errors.length} 份定义没加载起来</p>
          {catalog.errors.map((line, index) => (
            <p key={index} className="mt-0.5 break-all font-mono text-[11px] text-muted">
              {line}
            </p>
          ))}
        </div>
      )}

      {agents.length > 0 && (
        <ul className="flex flex-col gap-2">
          {agents.map((agent) => (
            <li key={agent.name} className="rounded-lg border border-line bg-surface px-2.5 py-2">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="text-[13px] font-medium text-text">{agent.name}</span>
                <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted">
                  {agentSourceLabel(agent.source)}
                </span>
                {agent.permissionMode && (
                  <span className="font-mono text-[11px] text-faint">{agent.permissionMode}</span>
                )}
                {agent.isolation && <span className="font-mono text-[11px] text-faint">{agent.isolation}</span>}
              </div>
              {agent.description && (
                <p className="mt-1 text-[12px] leading-relaxed text-muted">{agent.description}</p>
              )}
              <p className="mt-1 break-words text-[12px] leading-relaxed text-faint">
                {agentToolsText(agent.tools, agent.disallowedTools)}
              </p>
              {agent.skills && agent.skills.length > 0 && (
                <p className="mt-0.5 text-[11px] text-faint">随行技能：{agent.skills.join("、")}</p>
              )}
              {(agent.path || agent.chars > 0) && (
                <p className="mt-0.5 truncate font-mono text-[11px] text-faint" title={agent.path}>
                  {agent.path || `${agent.chars} 字说明`}
                  {agent.path && agent.chars > 0 ? ` · ${agent.chars} 字说明` : ""}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
