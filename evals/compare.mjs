#!/usr/bin/env node
/**
 * 同题对标：同一批任务、同一套判分，跑「空白格 / Claude Code / Codex」三家。
 *
 * 为什么要有这个脚本：「比别家好」如果只是感觉，就没法验收。这里把"好"拆成
 * 五个可测量的分项，全部由机器判定，不看谁的自我描述漂亮：
 *
 *   1. 正确性 45%  —— 任务自带的 verify 命令（pytest / node --test）是否全绿
 *   2. 命中率 20%  —— 任务要求改的文件，是否真的改了
 *   3. 最小改动 15% —— 有没有顺手改了一堆不相干的东西
 *   4. 自证 10%   —— 它自己有没有跑过验证命令（不是说"已修复"，是跑了）
 *   5. 成本 10%   —— token 量，同一题里最省的拿满分
 *
 * 用法：
 *   node evals/compare.mjs --only T01,T02,T03 --agents ours,claude
 *   node evals/compare.mjs --agents ours,claude,codex --timeout 600
 *   node evals/compare.mjs --only T01 --agents ours --api http://127.0.0.1:8060/api/v1
 *
 * 说明：
 * - 每个 agent 拿到的 prompt 与工作区完全相同，工作区每次从 fixture 重建。
 * - 三家都自动放行高风险操作（ours: approve_high_risk，claude: bypassPermissions），
 *   否则会停在审批上，比出来的不是能力而是耐心。
 * - 模型不同是事实，输出里会打印每家用的模型名，别把模型差异算成产品差异。
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
// .blank_backups（下划线）是应用自己的可回滚备份目录，不是 agent 的产出，
// 算进"最小改动"会让每次正经改动都被扣分。
const IGNORED_DIRS = new Set(["node_modules", ".git", ".blank_backups", ".blank-backups", ".pytest_cache", "__pycache__", ".venv", "dist"]);
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "paused"]);
const PY = path.join(ROOT, "services", "api", ".venv", "Scripts", "python.exe");
// 竞品 CLI 的安装位置。这台机器上都在 E:\env\bin（不是默认 npm 全局目录），
// 而 cmd 不认 POSIX 形式的 PATH 项，所以这里直接拼绝对路径，别指望 PATH。
const CLI_DIR = process.env.BLANK_CLI_DIR ?? "E:\\env\\bin";

function cli(name) {
  for (const candidate of [`${name}.cmd`, name]) {
    const full = path.join(CLI_DIR, candidate);
    if (existsSync(full)) return `"${full}"`;
  }
  return name;
}

function parseArgs(argv) {
  const args = {
    api: process.env.BLANK_API_BASE ? `${process.env.BLANK_API_BASE.replace(/\/$/, "")}/api/v1` : "http://127.0.0.1:8000/api/v1",
    agents: ["ours", "claude"],
    only: null,
    timeout: 420,
    keep: false,
    includeGreen: false,
    cleanup: false,
    provider: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--api") args.api = argv[++i].replace(/\/$/, "");
    else if (arg === "--agents") args.agents = argv[++i].split(",").map((s) => s.trim()).filter(Boolean);
    else if (arg === "--only") args.only = new Set(argv[++i].split(",").map((s) => s.trim()));
    else if (arg === "--timeout") args.timeout = Number(argv[++i]);
    else if (arg === "--provider") args.provider = argv[++i];
    else if (arg === "--keep") args.keep = true;
    else if (arg === "--include-green") args.includeGreen = true;
    else if (arg === "--cleanup") args.cleanup = true;
    else if (arg === "--help") {
      console.log(readFileSync(new URL(import.meta.url)).toString().split("*/")[0]);
      process.exit(0);
    }
  }
  return args;
}

// ── 工作区 ────────────────────────────────────────────────────────────

/** Windows 上刚被 agent 进程释放的目录经常还占着句柄，直接删会 EPERM。重试即可。 */
function safeRemove(target) {
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      rmSync(target, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 });
      return true;
    } catch {
      // 句柄没放开，稍等再试；仍然不行就不删，不能让清理失败毁掉整轮评测
      const until = Date.now() + 400;
      while (Date.now() < until) {
        /* 忙等 0.4s：同步上下文里没有更好的等待方式 */
      }
    }
  }
  return false;
}

function buildWorkspace(files, target) {
  safeRemove(target);
  for (const [rel, content] of Object.entries(files)) {
    const file = path.join(target, rel);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, content, "utf8");
  }
  return target;
}

function snapshotTree(dir, base = dir, out = new Map()) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (IGNORED_DIRS.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) snapshotTree(full, base, out);
    else if (statSync(full).size < 2_000_000) out.set(path.relative(base, full).replaceAll("\\", "/"), readFileSync(full));
  }
  return out;
}

/** 与 fixture 逐字节比：改了哪些、新增了哪些。比 git diff 更适合"从零铺出来的工作区"。 */
function changedFiles(before, after) {
  const changed = [];
  for (const [rel, content] of after) {
    const old = before.get(rel);
    if (!old || !old.equals(content)) changed.push(rel);
  }
  for (const rel of before.keys()) if (!after.has(rel)) changed.push(rel);
  return changed.sort();
}

function runCommand(command, cwd, timeoutMs, env = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, {
      cwd,
      shell: true,
      env: { ...process.env, ...env },
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve({ code: 124, stdout, stderr: `${stderr}\n[benchmark] 超时 ${timeoutMs}ms`, timedOut: true });
    }, timeoutMs);
    child.stdout?.on("data", (chunk) => (stdout += chunk));
    child.stderr?.on("data", (chunk) => (stderr += chunk));
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ code: 1, stdout, stderr: `${stderr}${error.message}`, timedOut: false });
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr, timedOut: false });
    });
  });
}

function verifyCommand(sample) {
  const command = String(sample.verify ?? "").trim();
  if (!command) return null;
  // 基准里写的是 `python -m pytest -q`：这台机器上要用项目自己的 venv，
  // 系统 python 里没有 pytest。别的地方保持原样。
  if (process.platform === "win32" && existsSync(PY)) return command.replace(/^python\b/, `"${PY}"`);
  return command;
}

// ── 三家执行器 ────────────────────────────────────────────────────────

async function runOurs(sample, workspace, args) {
  const api = args.api;
  const call = async (method, pathname, body) => {
    const response = await fetch(`${api}${pathname}`, {
      method,
      headers: body ? { "content-type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await response.text();
    if (!response.ok) throw new Error(`${method} ${pathname} → ${response.status} ${text.slice(0, 200)}`);
    return text ? JSON.parse(text) : null;
  };

  const project = await call("POST", "/projects", { name: `bench-${sample.id}`, description: sample.title });
  const workspaceRecord = await call("POST", `/projects/${project.id}/workspaces`, { path: workspace, label: sample.id });
  const task = await call("POST", `/projects/${project.id}/tasks`, {
    title: sample.title,
    goal: sample.prompt,
    acceptance_criteria: sample.acceptance ?? [],
    provider_id: args.provider,
  });
  const started = Date.now();
  await call("POST", `/tasks/${task.id}/run`, { provider_id: args.provider, approve_high_risk: true });

  let evidence = null;
  while (Date.now() - started < args.timeout * 1000) {
    evidence = await call("GET", `/tasks/${task.id}/evidence`);
    const status = evidence?.task?.status ?? "running";
    if (status === "waiting_approval") {
      await call("POST", `/tasks/${task.id}/approval`, { approve: true, provider_id: args.provider });
      continue;
    }
    if (TERMINAL.has(status)) break;
    await new Promise((resolve) => setTimeout(resolve, 1_500));
  }
  const usage = evidence?.usage ?? {};
  const tools = evidence?.tools ?? [];
  const commands = tools.map((tool) => String(tool.arguments?.command ?? ""));
  const verify = String(sample.verify ?? "");
  return {
    status: evidence?.task?.status ?? "timeout",
    tokens: Number(usage.total_tokens ?? (usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0)),
    turns: Number(usage.calls ?? tools.length),
    model: (usage.models ?? []).join(",") || "-",
    // 我们有自己的 run_tests 工具，命令在工具内部拼，不在 arguments 里，
    // 所以"跑过测试"要按工具名+结果判，不能只看命令行文本。
    selfVerified:
      tools.some((tool) => tool.name === "run_tests" && tool.status === "ok") ||
      commands.some((command) => verify && command.includes(verify.split(" ")[0])),
    transcript: (evidence?.task?.result_summary ?? "").slice(0, 400),
    tools: tools.map((tool) => ({ name: tool.name, status: tool.status })),
    wallMs: Date.now() - started,
    projectId: project.id,
    workspaceId: workspaceRecord?.id,
    taskId: task.id,
  };
}

async function runClaude(sample, workspace, args) {
  const started = Date.now();
  const result = await runCommand(
    `${cli("claude")} -p "${escapeShell(sample.prompt)}" --output-format json --permission-mode bypassPermissions`,
    workspace,
    args.timeout * 1000,
  );
  const wallMs = Date.now() - started;
  let parsed = null;
  try {
    parsed = JSON.parse(result.stdout.trim().split("\n").filter(Boolean).pop());
  } catch {
    parsed = null;
  }
  if (!parsed) {
    return {
      status: result.code === 124 ? "timeout" : "error",
      tokens: 0,
      turns: 0,
      model: "claude-code",
      selfVerified: false,
      transcript: (result.stderr || result.stdout).slice(0, 400),
      tools: [],
      wallMs,
      unavailable: true,
    };
  }
  const usage = parsed.usage ?? {};
  const tokens =
    Number(usage.input_tokens ?? 0) +
    Number(usage.cache_read_input_tokens ?? 0) +
    Number(usage.cache_creation_input_tokens ?? 0) +
    Number(usage.output_tokens ?? 0);
  const text = String(parsed.result ?? "");
  const verify = String(sample.verify ?? "").split(" ")[0];
  return {
    status: parsed.is_error ? "failed" : "succeeded",
    tokens,
    turns: Number(parsed.num_turns ?? 0),
    model: Object.keys(parsed.modelUsage ?? {}).join(",") || "claude",
    // 只能从它自己的报告里判断：json 输出不含工具明细，这一点写在文档里，
    // 别把"没检测到"当成"没跑过"。
    selfVerified: verify ? new RegExp(escapeRegExp(verify)).test(text) : false,
    transcript: text.slice(0, 400),
    tools: [],
    wallMs,
  };
}

async function runCodex(sample, workspace, args) {
  const started = Date.now();
  const result = await runCommand(
    `${cli("codex")} exec --json --skip-git-repo-check -C "${workspace}" "${escapeShell(sample.prompt)}" < nul`,
    workspace,
    args.timeout * 1000,
  );
  const wallMs = Date.now() - started;
  const lines = result.stdout.split("\n").filter((line) => line.trim().startsWith("{"));
  let tokens = 0;
  let turns = 0;
  let lastText = "";
  for (const line of lines) {
    try {
      const event = JSON.parse(line);
      const usage = event.usage ?? event.msg?.usage;
      if (usage) {
        tokens = Number(usage.input_tokens ?? 0) + Number(usage.output_tokens ?? 0);
        turns = Number(usage.input_tokens ? turns + 1 : turns);
      }
      if (event.type === "item.completed" && event.item?.text) lastText = event.item.text;
    } catch {
      /* 非 JSON 行（横幅、警告）忽略 */
    }
  }
  const unavailable = /unavailable|404 Not Found|Reconnecting/.test(result.stderr || result.stdout);
  return {
    status: unavailable ? "unavailable" : tokens > 0 ? "succeeded" : "error",
    tokens,
    turns,
    model: "codex-cli",
    selfVerified: false,
    transcript: (lastText || result.stderr || result.stdout).slice(0, 400),
    tools: [],
    wallMs,
    unavailable,
  };
}

function escapeShell(text) {
  return String(text).replace(/"/g, '\\"').replace(/\r?\n/g, " ");
}

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const RUNNERS = { ours: runOurs, claude: runClaude, codex: runCodex };

// ── 判分 ─────────────────────────────────────────────────────────────

function score(record, sample, cheapestTokens) {
  // 验证通过还不够：一行代码都没动却"测试通过"，说明它什么也没做（基线本来就绿）。
  // 这类必须给 0，否则"什么都不做"会拿到 60 分。
  const tests = record.verifyOk && record.changed.length > 0 ? 1 : 0;
  const expected = sample.expect_files ?? [];
  const hit = expected.length
    ? expected.filter((rel) => record.changed.includes(rel)).length / expected.length
    : 1;
  const extra = record.changed.filter((rel) => !expected.includes(rel)).length;
  const minimal = extra === 0 ? 1 : extra === 1 ? 0.6 : extra === 2 ? 0.3 : 0;
  const self = record.selfVerified ? 1 : 0;
  const cost = record.tokens > 0 && cheapestTokens > 0 ? Math.min(1, cheapestTokens / record.tokens) : 0;
  const total = 0.45 * tests + 0.2 * hit + 0.15 * minimal + 0.1 * self + 0.1 * cost;
  return { tests, hit, minimal, self, cost, total: Number((total * 100).toFixed(1)) };
}

function fmtTokens(value) {
  if (!value) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`;
  return String(value);
}

function renderTable(agents, rows) {
  const header = ["任务", ...agents.map((agent) => `${agent} 分数`)];
  const lines = [`| ${header.join(" | ")} |`, `| ${header.map(() => "---").join(" | ")} |`];
  for (const row of rows) {
    const cells = [row.id];
    for (const agent of agents) {
      const record = row.runs[agent];
      cells.push(record?.score ? `${record.score.total}` : "—");
    }
    lines.push(`| ${cells.join(" | ")} |`);
  }
  return lines.join("\n");
}

function renderDetail(agents, rows) {
  const header = ["任务", "agent", "分", "测试", "改动文件", "自证", "token", "轮次", "耗时", "状态"];
  const lines = [`| ${header.join(" | ")} |`, `| ${header.map(() => "---").join(" | ")} |`];
  for (const row of rows) {
    for (const agent of agents) {
      const record = row.runs[agent];
      if (!record) continue;
      lines.push(
        `| ${row.id} | ${agent} | ${record.score?.total ?? "-"} | ${record.verifyOk ? "通过" : "未通过"} | ` +
          `${record.changed.length} | ${record.selfVerified ? "是" : "否"} | ${fmtTokens(record.tokens)} | ` +
          `${record.turns} | ${(record.wallMs / 1000).toFixed(0)}s | ${record.status} |`,
      );
    }
  }
  return lines.join("\n");
}

function summarize(agents, rows, samples) {
  const summary = {};
  for (const agent of agents) {
    const finished = rows
      .filter((row) => !row.skipped)
      .map((row) => row.runs[agent])
      .filter((record) => record && record.status !== "unavailable");
    const scored = finished.filter((record) => record.score);
    summary[agent] = {
      tasks: finished.length,
      avg_score: scored.length ? Number((scored.reduce((acc, r) => acc + r.score.total, 0) / scored.length).toFixed(1)) : 0,
      pass_rate: scored.length ? Number((scored.filter((r) => r.score.tests === 1).length / scored.length).toFixed(3)) : 0,
      tokens: finished.reduce((acc, r) => acc + r.tokens, 0),
      avg_tokens: scored.length ? Math.round(finished.reduce((acc, r) => acc + r.tokens, 0) / scored.length) : 0,
      avg_wall_ms: scored.length ? Math.round(finished.reduce((acc, r) => acc + r.wallMs, 0) / scored.length) : 0,
      models: [...new Set(finished.map((r) => r.model).filter(Boolean))],
      unavailable: rows.filter((row) => row.runs[agent]?.status === "unavailable").length,
    };
  }
  return summary;
}

// ── 主流程 ────────────────────────────────────────────────────────────

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const catalog = JSON.parse(readFileSync(path.join(ROOT, "evals", "task-samples.json"), "utf8"));
  const samples = args.only ? catalog.samples.filter((sample) => args.only.has(sample.id)) : catalog.samples;
  if (!samples.length) throw new Error("没有选中任何任务：检查 --only");

  console.log(`同题对标：${samples.length} 个任务、${args.agents.length} 个 agent、单任务上限 ${args.timeout}s`);
  const rows = [];
  for (const sample of samples) {
    const row = { id: sample.id, runs: {} };
    for (const agent of args.agents) {
      const runner = RUNNERS[agent];
      if (!runner) throw new Error(`未知 agent：${agent}（可选 ${Object.keys(RUNNERS).join(", ")}）`);
      const workspace = path.join(ROOT, "evals", "workspaces", `bench-${sample.id}-${agent}`);
      buildWorkspace(catalog.fixtures[sample.fixture], workspace);
      const before = snapshotTree(workspace);
      if (agent === args.agents[0]) {
        // 基线自检：记下"什么都不做"时验证命令的结果。多数样例的既有测试本来
        // 就是绿的（任务要求的是新增能力 + 新增测试），所以这个值只用于解释分数，
        // 不作为跳过条件——真正的区分在"有没有动到该动的文件"。
        const command = verifyCommand(sample);
        const baseline = command ? await runCommand(command, workspace, 180_000) : { code: 1 };
        row.baselineOk = baseline.code === 0;
      }
      process.stdout.write(`  ${sample.id} / ${agent} … `);
      let record;
      try {
        record = await runner(sample, workspace, args);
      } catch (error) {
        record = { status: "error", tokens: 0, turns: 0, model: "-", selfVerified: false, transcript: String(error).slice(0, 300), tools: [], wallMs: 0 };
      }
      const after = snapshotTree(workspace);
      record.changed = changedFiles(before, after);
      // 验证命令由我们（而不是 agent）跑：它自己说"全绿"不算数。
      const command = verifyCommand(sample);
      if (command && record.status !== "unavailable") {
        const verified = await runCommand(command, workspace, 180_000);
        record.verifyOk = verified.code === 0;
        record.verifyOutput = (verified.stdout || verified.stderr).slice(-600);
      } else {
        record.verifyOk = false;
      }
      record.verifyCommand = command ?? "";
      row.runs[agent] = record;
      console.log(`分 ${record.tokens ? "" : ""}${record.verifyOk ? "测试通过" : "测试未过"} · ${fmtTokens(record.tokens)} token · ${record.changed.length} 个文件改动`);
      if (!args.keep) safeRemove(workspace);
      if (args.cleanup && record.projectId) {
        await fetch(`${args.api}/projects/${record.projectId}`, { method: "DELETE" }).catch(() => null);
      }
    }
    rows.push(row);
  }

  // 成本分要跨家比才有意义：同一题里 token 最少的拿满分。
  for (const row of rows) {
    const sample = samples.find((item) => item.id === row.id);
    const counts = Object.values(row.runs).map((record) => record.tokens).filter((value) => value > 0);
    const cheapest = counts.length ? Math.min(...counts) : 0;
    for (const record of Object.values(row.runs)) {
      if (record.status === "unavailable") continue;
      record.score = score(record, sample, cheapest);
    }
  }

  const summary = summarize(args.agents, rows, samples);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outDir = path.join(ROOT, "evals", "results");
  mkdirSync(outDir, { recursive: true });
  const file = path.join(outDir, `compare-${stamp}.json`);
  writeFileSync(file, JSON.stringify({ summary, rows, args: { ...args, only: [...(args.only ?? [])] } }, null, 2), "utf8");

  console.log("\n== 综合分 ==");
  console.log(renderTable(args.agents, rows));
  console.log("\n== 明细 ==");
  console.log(renderDetail(args.agents, rows));
  console.log("\n== 汇总 ==");
  for (const agent of args.agents) {
    const item = summary[agent];
    console.log(
      `${agent.padEnd(7)} 平均分 ${String(item.avg_score).padStart(5)} · 通过率 ${(item.pass_rate * 100).toFixed(0)}% · ` +
        `平均 ${fmtTokens(item.avg_tokens)} token/任务 · ${(item.avg_wall_ms / 1000).toFixed(0)}s · 模型 ${item.models.join(",") || "-"}` +
        (item.unavailable ? ` · ${item.unavailable} 个任务不可用` : ""),
    );
  }
  // 显式退出：竞品 CLI 可能留下挂着 stdio 的子进程，不主动退出会让整轮评测
  // 跑完了却挂着不退，或被判成失败。
  console.log(`\n结果写入 ${file}`);
  console.log("判分口径见 docs/QUALITY.md：正确性 45%、命中 20%、最小改动 15%、自证 10%、成本 10%。");
}

main().then(
  () => process.exit(0),
  (error) => {
    console.error(error);
    process.exit(1);
  },
);
