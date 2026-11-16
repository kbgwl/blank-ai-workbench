"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/panel";
import { SelectPopover } from "@/components/ui/select-popover";
import { fetchProjects, type Project } from "@/components/projects/api";
import {
  approveMcpServer,
  errorMessage,
  fetchMcp,
  revokeMcpServer,
  type McpResponse,
  type McpServerStatus,
} from "./api";

/**
 * 外部工具服务端（MCP）。
 *
 * 这一屏存在的理由只有一个：用户改错配置里一个字段时，界面上唯一的症状是
 * 「工具变少了」。所以这里不做开关，只把后端每一行的判定结果原样摊开——
 * 从哪一层读到的、连上了几个、各自给了几个工具、没连上的那句原因是什么。
 *
 * 项目层（`.mcp.json`）与本机层（`.blank/mcp.local.json`）的服务端还要过一次
 * 批准：克隆来的仓库不能因为被打开就启动进程。批准记的是整条定义的哈希，
 * 改一个字节就会退回待批准。
 */

const STATE_LABELS: Record<string, string> = {
  ready: "已连接",
  failed: "连接失败",
  skipped: "已跳过",
};

const STATE_STYLES: Record<string, string> = {
  ready: "border-brand/40 bg-brand/10 text-brand",
  failed: "border-danger/40 bg-danger/10 text-danger",
  skipped: "border-line bg-surface-2 text-muted",
};

const SCOPE_LABELS: Record<string, string> = {
  user: "用户层",
  project: "项目层",
  local: "本机层",
};

const APPROVAL_LABELS: Record<string, string> = {
  pending: "待批准",
  approved: "已批准",
};

/** 空状态时直接给一段能粘的配置，比一句"请编辑 mcp.json"有用得多。 */
const TEMPLATE = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\\\work"]
    }
  }
}`;

function jobLimits(enforcement: McpServerStatus["enforcement"]): string {
  const parts: string[] = [];
  if (enforcement?.memory_enforced && enforcement.memory_bytes) {
    parts.push(`内存 ${Math.round(enforcement.memory_bytes / (1024 * 1024))} MB`);
  }
  if (enforcement?.pids_enforced && enforcement.pids_limit) {
    parts.push(`${enforcement.pids_limit} 个进程`);
  }
  if (enforcement?.tree_kill_enforced) parts.push("关连接带走整棵树");
  return parts.length ? `已挂 Job：${parts.join(" / ")}` : "已挂 Job";
}

function ServerRow({
  server,
  busy,
  onApprove,
  onRevoke,
}: {
  server: McpServerStatus;
  busy: boolean;
  onApprove: (server: McpServerStatus) => void;
  onRevoke: (server: McpServerStatus) => void;
}) {
  const style = STATE_STYLES[server.state] ?? STATE_STYLES.skipped;
  const pending = server.approval === "pending";
  return (
    <div className="border-b border-line py-2.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[12px] text-text">{server.name}</span>
        <span className={`rounded border px-1 py-0.5 text-[10px] ${style}`}>
          {STATE_LABELS[server.state] ?? server.state}
        </span>
        <span className="rounded border border-line bg-surface-2 px-1 py-0.5 text-[10px] text-faint">
          {SCOPE_LABELS[server.scope] ?? server.scope}
        </span>
        {server.approval && server.approval !== "user" && (
          <span
            className={`rounded border px-1 py-0.5 text-[10px] ${
              pending
                ? "border-warn/40 bg-warn/10 text-warn"
                : "border-brand/40 bg-brand/10 text-brand"
            }`}
          >
            {APPROVAL_LABELS[server.approval] ?? server.approval}
          </span>
        )}
        <span className="rounded border border-line bg-surface-2 px-1 py-0.5 text-[10px] text-faint">
          {server.transport || "未知传输"}
        </span>
        {server.state === "ready" && (
          <span className="font-mono text-[10px] text-faint">{server.tools} 个工具</span>
        )}
        {server.server && (
          <span className="font-mono text-[10px] text-faint">· {server.server}</span>
        )}
        {server.approval && server.approval !== "user" && (
          <button
            type="button"
            disabled={busy}
            onClick={() => (pending ? onApprove(server) : onRevoke(server))}
            className="ml-auto shrink-0 rounded-md border border-line bg-surface px-2 py-0.5 text-[10px] text-muted hover:border-line-strong disabled:opacity-50"
          >
            {busy ? "处理中…" : pending ? "批准" : "撤销批准"}
          </button>
        )}
      </div>
      {server.error && (
        // 原因要能一眼读完：用户下一步就是照着这句去改配置或点批准
        <p className="mt-1 font-mono text-[11px] leading-relaxed break-words text-danger/90">
          {server.error}
        </p>
      )}
      {server.warnings?.map((warning) => (
        // 变量没配时占位符会原样留着：这里不说，用户只会在 401 时才发现
        <p key={warning} className="mt-1 font-mono text-[11px] leading-relaxed break-words text-warn">
          ⚠ {warning}
        </p>
      ))}
      {server.state === "ready" && server.boundary === "job_object" && (
        <p className="mt-1 font-mono text-[10px] leading-relaxed break-words text-faint">
          {jobLimits(server.enforcement)}
        </p>
      )}
      {server.state === "ready" && server.boundary === "host_process_unbounded" && (
        // 连上了不等于进了边界：没挂进 Job 的进程按宿主权限跑，必须红字说清。
        <p className="mt-1 font-mono text-[11px] leading-relaxed break-words text-danger/90">
          {server.boundary_note || "⚠ 没有 Job 事实：该服务端按宿主权限运行，未由内核强制"}
        </p>
      )}
      {server.state === "ready" && server.boundary === "remote" && (
        <p className="mt-1 font-mono text-[10px] leading-relaxed break-words text-faint">
          进程在远端，本机 Job 管不到；出站请求仍走审批与审计
        </p>
      )}
    </div>
  );
}

export function McpPanel() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectId, setProjectId] = useState("");
  const [projectsError, setProjectsError] = useState("");
  // 结果与项目一起存：换项目时旧项目的数据立刻失效，不必在 effect 里同步置空。
  const [result, setResult] = useState<{
    projectId: string;
    data: McpResponse | null;
    error: string;
  } | null>(null);
  // 用 token 触发重拉：真正要同步的是外部系统（后端的连接状态），
  // 而刷新按钮的语义就是「我刚改了文件，再连一次」。
  const [reloadToken, setReloadToken] = useState(0);
  const [busyKey, setBusyKey] = useState("");
  const [actionError, setActionError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchProjects()
      .then((rows) => {
        if (!alive) return;
        setProjects(rows);
        // 默认选第一个绑了工作区的项目：没有工作区就没有项目层可读
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
    let alive = true;
    fetchMcp(projectId || undefined)
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
    () => [
      {
        value: "",
        label: "仅用户层",
        description: "不读任何工作区的 .mcp.json / mcp.local.json",
      },
      ...(projects ?? []).map((project) => ({
        value: project.id,
        label: project.name,
        description:
          project.workspaces.length > 0
            ? (project.workspaces.find((item) => item.is_active) ?? project.workspaces[0]).path
            : "未绑定工作区，只读用户层配置",
      })),
    ],
    [projects],
  );

  const selected = projects?.find((project) => project.id === projectId) ?? null;
  const noWorkspace = selected !== null && selected.workspaces.length === 0;

  async function act(kind: "approve" | "revoke", server: McpServerStatus) {
    const key = `${server.scope}:${server.name}`;
    setBusyKey(key);
    setActionError("");
    try {
      if (kind === "approve") {
        await approveMcpServer({ projectId, scope: server.scope, name: server.name });
      } else {
        await revokeMcpServer({ projectId, scope: server.scope, name: server.name });
      }
      setReloadToken((token) => token + 1);
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setBusyKey("");
    }
  }

  const ready = data?.servers.filter((item) => item.state === "ready").length ?? 0;
  const pending = data?.servers.filter((item) => item.approval === "pending").length ?? 0;

  return (
    <Panel
      title="外部工具（MCP）"
      hint={data ? `GET /settings/mcp · ${data.servers.length} 个服务端` : "GET /settings/mcp"}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-faint">工作区</span>
        <SelectPopover
          value={projectId}
          options={options}
          onChange={setProjectId}
          ariaLabel="选择 MCP 配置所属项目"
          placeholder={projects === null ? "加载项目…" : "仅用户层"}
          disabled={projects === null}
          triggerClassName="border border-line bg-surface"
        />
      </div>

      {error && <p className="mb-2 text-[12px] leading-relaxed text-danger/90">{error}</p>}
      {actionError && (
        <p className="mb-2 text-[12px] leading-relaxed text-danger/90">{actionError}</p>
      )}

      {projectId && noWorkspace && (
        <p className="mb-2 text-[11px] leading-relaxed text-warn">
          该项目还没绑定工作区：只读了用户层配置，工作区下没有项目层/本机层可读。
        </p>
      )}

      {!data && !error && <p className="text-[12px] text-faint">加载中…</p>}

      {data && (
        <>
          {data.servers.length === 0 ? (
            <div className="space-y-2">
              <p className="text-[12px] leading-relaxed text-muted">
                这里还没有读到外部工具服务端。配置分三层，后面的同名条目整条覆盖前面的：
              </p>
              <div className="space-y-1 font-mono text-[11px] text-faint">
                <p>
                  用户层 <span className="text-muted">{data.config}</span>
                </p>
                {data.scopes.project && (
                  <p>
                    项目层 <span className="text-muted">{data.scopes.project.path}</span>
                    {data.scopes.project.exists ? "" : "（不存在）"}
                  </p>
                )}
                {data.scopes.local && (
                  <p>
                    本机层 <span className="text-muted">{data.scopes.local.path}</span>
                    {data.scopes.local.exists ? "" : "（不存在）"}
                  </p>
                )}
              </div>
              <pre className="overflow-x-auto rounded-md border border-line bg-surface-2 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted">
                {TEMPLATE}
              </pre>
              <p className="text-[11px] leading-relaxed text-faint">
                外部工具默认按高风险走审批：服务端能做什么我们看不出来，所以不默认放行。
                要调低某个服务端的风险等级，在它的配置里写{" "}
                <span className="font-mono">&quot;risk&quot;: &quot;low&quot;</span>。
                项目层与本机层的条目还要在下面点一次「批准」才会启动。
              </p>
            </div>
          ) : (
            <>
              <p className="mb-1 text-[11px] leading-relaxed text-faint">
                {ready} 个已连接，共 {data.tools} 个工具。
                {pending > 0 && ` ${pending} 个待批准（批准前不会启动进程）。`}
                这些工具默认不出现在提示词里，模型需要时用工具检索拉回来。
              </p>
              <div className="flex flex-col">
                {data.servers.map((server) => (
                  <ServerRow
                    key={`${server.scope}:${server.name}`}
                    server={server}
                    busy={busyKey === `${server.scope}:${server.name}`}
                    onApprove={(item) => void act("approve", item)}
                    onRevoke={(item) => void act("revoke", item)}
                  />
                ))}
              </div>
            </>
          )}

          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-2">
            <span className="min-w-0 flex-1 font-mono text-[10px] break-all text-faint">
              {data.config}
            </span>
            <button
              type="button"
              onClick={() => setReloadToken((token) => token + 1)}
              className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:border-line-strong"
            >
              重新连接
            </button>
          </div>
        </>
      )}
    </Panel>
  );
}
