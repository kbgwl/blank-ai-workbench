"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { errorMessage, fetchMcp, type McpResponse, type McpServerStatus } from "./api";

/**
 * 外部工具服务端（MCP）。
 *
 * 这一屏存在的理由只有一个：用户改错 mcp.json 一个字段时，界面上唯一的症状是
 * 「工具变少了」。所以这里不做开关，只把后端每一行的判定结果原样摊开——
 * 连上了几个、各自给了几个工具、没连上的那句原因是什么。
 */

const STATE_LABELS: Record<string, string> = {
  ready: "已连接",
  failed: "连接失败",
  skipped: "已跳过",
};

const STATE_STYLES: Record<string, string> = {
  ready: "border-brand/40 bg-brand/10 text-brand",
  failed: "border-danger/40 bg-danger/10 text-danger",
  skipped: "border-line bg-surface-2/60 text-muted",
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

function ServerRow({ server }: { server: McpServerStatus }) {
  const style = STATE_STYLES[server.state] ?? STATE_STYLES.skipped;
  return (
    <div className="border-b border-line/60 py-2.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[12px] text-text">{server.name}</span>
        <span className={`rounded border px-1 py-0.5 text-[10px] ${style}`}>
          {STATE_LABELS[server.state] ?? server.state}
        </span>
        <span className="rounded border border-line bg-surface-2/60 px-1 py-0.5 text-[10px] text-faint">
          {server.transport || "未知传输"}
        </span>
        {server.state === "ready" && (
          <span className="font-mono text-[10px] text-faint">{server.tools} 个工具</span>
        )}
        {server.server && (
          <span className="font-mono text-[10px] text-faint">· {server.server}</span>
        )}
      </div>
      {server.error && (
        // 原因要能一眼读完：用户下一步就是照着这句去改配置
        <p className="mt-1 font-mono text-[11px] leading-relaxed break-words text-danger/90">
          {server.error}
        </p>
      )}
    </div>
  );
}

export function McpPanel() {
  const [data, setData] = useState<McpResponse | null>(null);
  const [error, setError] = useState("");
  // 用 token 触发重拉：真正要同步的是外部系统（后端的连接状态），
  // 而刷新按钮的语义就是「我刚改了文件，再连一次」。
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let alive = true;
    fetchMcp()
      .then((next) => {
        if (!alive) return;
        setData(next);
        setError("");
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  if (!data) {
    return (
      <Panel title="外部工具（MCP）" hint="GET /settings/mcp">
        <p className="text-[12px] text-faint">{error || "加载中…"}</p>
      </Panel>
    );
  }

  const ready = data.servers.filter((item) => item.state === "ready").length;

  return (
    <Panel title="外部工具（MCP）" hint={`GET /settings/mcp · ${data.servers.length} 个服务端`}>
      {data.servers.length === 0 ? (
        <div className="space-y-2">
          <p className="text-[12px] leading-relaxed text-muted">
            还没有配置外部工具服务端。在下面这个位置放一份 <span className="font-mono">mcp.json</span>，
            重启后它们就会出现在工具表里——和内置工具用同一套审批与审计。
          </p>
          <p className="rounded-md border border-line bg-surface-2/50 px-2.5 py-1.5 font-mono text-[11px] break-all text-faint">
            {data.config}
          </p>
          <pre className="overflow-x-auto rounded-md border border-line bg-surface-2/50 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted">
            {TEMPLATE}
          </pre>
          <p className="text-[11px] leading-relaxed text-faint">
            外部工具默认按高风险走审批：服务端能做什么我们看不出来，所以不默认放行。
            要调低某个服务端的风险等级，在它的配置里写 <span className="font-mono">&quot;risk&quot;: &quot;low&quot;</span>。
          </p>
        </div>
      ) : (
        <>
          <p className="mb-1 text-[11px] leading-relaxed text-faint">
            {ready} 个已连接，共 {data.tools} 个工具。这些工具默认不出现在提示词里，
            模型需要时用工具检索拉回来——接的服务端越多，这一步越重要。
          </p>
          <div className="flex flex-col">
            {data.servers.map((server) => (
              <ServerRow key={server.name} server={server} />
            ))}
          </div>
        </>
      )}

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-line/70 pt-2">
        <span className="min-w-0 flex-1 font-mono text-[10px] break-all text-faint">
          {error || data.config}
        </span>
        <button
          type="button"
          onClick={() => setReloadToken((token) => token + 1)}
          className="shrink-0 rounded-md border border-line bg-surface px-2.5 py-1 text-[11px] text-muted hover:border-line-strong"
        >
          重新连接
        </button>
      </div>
    </Panel>
  );
}
