// 本地脚本化模型服务（OpenAI 兼容 /v1/chat/completions）
// 用途：在没有有效供应商 API Key 的机器上，驱动真实的 Agent 运行链路
// （计划 → read_file → apply_patch → run_tests → 总结），用于端到端演示与回归评测。
// 它不是语言模型：只做确定性的「按目标里的代码块整文件重写 / 按『把 A 改成 B』文本替换」推理。
import { createServer } from "node:http";
import { writeFileSync } from "node:fs";

const PORT = Number(process.env.MOCK_MODEL_PORT ?? 8099);

function contentToText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((p) => p?.text ?? "").join("");
  return "";
}

function executedTools(messages) {
  const names = [];
  for (const m of messages) {
    for (const call of m.tool_calls ?? []) names.push(call.function?.name);
  }
  return names;
}

function lastToolResult(messages, name) {
  const index = (messages ?? []).findLastIndex(
    (m) => m.role === "assistant" && (m.tool_calls ?? []).some((call) => call.function?.name === name),
  );
  if (index < 0) return "";
  for (let i = index + 1; i < messages.length; i += 1) {
    if (messages[i].role === "tool") return contentToText(messages[i].content);
  }
  return "";
}

function goalOf(messages) {
  for (const m of messages) if (m.role === "user") return contentToText(m.content);
  return "";
}

function targetFile(goal) {
  // 越权样例（.. 或绝对路径）原样照抄，让平台的 path_escape_denied 在评测链路里真实触发。
  const outside = /(?:\.\.\/)+[^\s，。]+|^[A-Za-z]:\\[^\s，。]+|\/(?:etc|usr|var|home|Windows)\/[^\s，。]+/.exec(goal);
  if (outside) return outside[0];
  const match = goal.match(/[\w./-]+\.(?:py|ts|tsx|js|jsx|md|json|java|go|rs|css|html)\b/);
  return match ? match[0].replace(/^\.?\//, "") : "README.md";
}

function secretWriteRule(original, goal) {
  const literal = /\b(sk-[A-Za-z0-9_\-]{8,})\b/.exec(goal);
  if (!literal) return null;
  return `${String(original).replace(/\s+$/, "")}\n\nAPI_KEY=${literal[1]}\n`;
}

function fencedBlock(goal) {
  const blocks = [...goal.matchAll(/```[^\n]*\n([\s\S]*?)```/g)].map((m) => m[1]);
  return blocks.length ? blocks[blocks.length - 1] : null;
}

function applyTextRule(content, goal) {
  const rule = goal.match(/把\s*[「"']([^」"']+)[」"']\s*(?:改成|替换为|改为)\s*[「"']([^」"']+)[」"']/);
  if (!rule) return null;
  if (!content.includes(rule[1])) return null;
  return content.split(rule[1]).join(rule[2]);
}

function replacementPatch(path, original, updated) {
  const oldLines = original.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n");
  const newLines = updated.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n");
  const body = [
    ...oldLines.map((line) => `-${line}`),
    ...newLines.map((line) => `+${line}`),
  ].join("\n");
  return `--- a/${path}\n+++ b/${path}\n@@ -1,${oldLines.length} +1,${newLines.length} @@\n${body}\n`;
}

function nextTurn(messages) {
  const goal = goalOf(messages);
  const done = executedTools(messages);
  const path = targetFile(goal);
  const call = (name, args) => ({
    id: `call_${name}_${done.length}`,
    type: "function",
    function: { name, arguments: JSON.stringify(args) },
  });

  // 「执行命令：<cmd>」用来演示高风险工具审批：run_shell 命中破坏性/联网规则即为 high。
  const shellMatch = /执行命令：(.+)/.exec(goal);
  if (shellMatch && !done.includes("run_shell")) {
    const command = shellMatch[1].trim();
    return { content: `需要先执行命令：${command}`, tool_calls: [call("run_shell", { command })] };
  }

  if (!done.includes("read_file")) {
    return {
      content: `执行计划：\n1. 读取 ${path}\n2. 修改 ${path}\n3. 运行测试验证\n4. 汇总结果`,
      tool_calls: [call("read_file", { path })],
    };
  }
  const raw = lastToolResult(messages, "read_file");
  let original = raw;
  try {
    const parsed = JSON.parse(raw);
    original = parsed?.output ?? parsed?.content ?? parsed?.text ?? raw;
  } catch {
    /* 工具结果不是 JSON 时按纯文本处理 */
  }
  const desired = fencedBlock(goal) ?? applyTextRule(original, goal) ?? secretWriteRule(original, goal);
  const patchNeeded = desired !== null && desired !== undefined && String(desired) !== String(original);
  const testsFailed = /failed|FAILED|error|Error|非零|Traceback/.test(lastToolResult(messages, "run_tests"));

  if (patchNeeded && (!done.includes("apply_patch") || (testsFailed && !done.includes("run_shell")))) {
    return {
      content: done.includes("apply_patch") ? "测试仍未通过，再次修正文件内容。" : `按目标要求改写 ${path}。`,
      tool_calls: [call("apply_patch", { path, patch: replacementPatch(path, String(original), String(desired)) })],
    };
  }
  if (!done.includes("run_tests")) {
    return { content: "修改已应用，运行测试验证。", tool_calls: [call("run_tests", {})] };
  }
  const criteria = acceptanceCriteria(messages);
  if (criteria.length) {
    return {
      content: JSON.stringify({
        summary: `已完成：读取 ${path}、按目标改写文件并运行测试，测试${testsFailed ? "未通过" : "通过"}。`,
        acceptance: criteria.map((criterion) => ({
          criterion,
          covered: !testsFailed,
          evidence: testsFailed ? "run_tests 仍报告失败" : "run_tests 退出码 0，见该步骤工具输出",
        })),
      }),
      tool_calls: [],
    };
  }
  return {
    content: testsFailed
      ? "验证未通过：测试仍报告失败，需要人工确认目标描述是否完整。"
      : `已完成：读取 ${path}、按要求修改文件并运行测试，测试通过。`,
    tool_calls: [],
  };
}

/** 汇总节点会在最后一条 user 消息里列出「验收条件：」，脚本化模型据此返回契约要求的 JSON。 */
function acceptanceCriteria(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "user") continue;
    const text = typeof message.content === "string" ? message.content : "";
    const marker = text.indexOf("验收条件：");
    if (marker === -1) continue;
    return text
      .slice(marker)
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.startsWith("- "))
      .map((line) => line.slice(2).trim())
      .filter(Boolean);
  }
  return [];
}

const server = createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", () => {
    if (!req.url?.includes("/chat/completions")) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "no route" }));
      return;
    }
    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "bad json" }));
      return;
    }
    const turn = nextTurn(payload.messages ?? []);
    if (process.env.MOCK_DUMP) writeFileSync(process.env.MOCK_DUMP, JSON.stringify(payload.messages ?? [], null, 2));
    res.writeHead(200, { "content-type": "application/json" });
    res.end(
      JSON.stringify({
        id: `mock-${Date.now()}`,
        object: "chat.completion",
        model: payload.model ?? "mock-coder",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: turn.content, tool_calls: turn.tool_calls.length ? turn.tool_calls : undefined },
            finish_reason: turn.tool_calls.length ? "tool_calls" : "stop",
          },
        ],
        usage: { prompt_tokens: Math.ceil(body.length / 4), completion_tokens: Math.ceil((turn.content?.length ?? 0) / 4) },
      }),
    );
  });
});

server.listen(PORT, "127.0.0.1", () => console.log(`mock model server on http://127.0.0.1:${PORT}/v1`));
