#!/usr/bin/env node
// 阶段7 评测执行器：把 task-samples.json 里的样例铺成工作区，通过任务 API 跑一遍并统计。
// 用法： node evals/run_evals.mjs [--api http://127.0.0.1:8050/api/v1] [--only T01,T03] [--concurrency 5] [--approve-high] [--provider ID] [--token T]
import { mkdir, rm, writeFile, readFile } from "node:fs/promises";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

function parseArgs(argv) {
  const args = {
    api: (process.env.BLANK_API_BASE ?? "http://127.0.0.1:8050") + "/api/v1",
    concurrency: 5,
    only: null,
    approveHigh: false,
    provider: null,
    autoApprove: true,
    token: process.env.BLANK_API_TOKEN ?? null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--api") args.api = argv[++i];
    else if (arg === "--concurrency") args.concurrency = Number(argv[++i]);
    else if (arg === "--only") args.only = new Set(argv[++i].split(","));
    else if (arg === "--approve-high") args.approveHigh = true;
    else if (arg === "--reject-high") args.autoApprove = false;
    else if (arg === "--provider") args.provider = argv[++i];
    else if (arg === "--token") args.token = argv[++i];
    else if (arg === "--help") {
      console.log(
        "用法：node evals/run_evals.mjs [--api URL] [--only T01,T03] [--concurrency N] [--approve-high] [--provider ID] [--token T]",
      );
      process.exit(0);
    }
  }
  return args;
}

async function request(api, method, pathname, body, args) {
  const headers = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (args?.token) headers.authorization = `Bearer ${args.token}`;
  const response = await fetch(`${api}${pathname}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 204) return null;
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    const error = data?.error ?? {};
    throw new EvalError(error.code ?? `http_${response.status}`, error.message ?? response.statusText, pathname);
  }
  return data;
}

class EvalError extends Error {
  constructor(code, message, pathname) {
    super(`${code}: ${message} (${pathname})`);
    this.code = code;
  }
}

async function buildWorkspace(fixtureFiles, target) {
  await rm(target, { recursive: true, force: true });
  for (const [rel, content] of Object.entries(fixtureFiles)) {
    const file = path.join(target, rel);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, content, "utf8");
  }
  return target;
}

async function runOne(sample, fixtures, args) {
  const started = Date.now();
  const record = {
    id: sample.id,
    title: sample.title,
    category: sample.category,
    risk: sample.risk,
    status: "unknown",
    failure: null,
    events: 0,
    usage: null,
    latency_ms: 0,
  };
  const workspaceDir = path.join(ROOT, "evals", "workspaces", sample.id);
  await buildWorkspace(fixtures[sample.fixture], workspaceDir);

  try {
    const project = await request(args.api, "POST", "/projects", {
      name: `eval-${sample.id}`,
      description: sample.title,
    }, args);
    await request(args.api, "POST", `/projects/${project.id}/workspaces`, { path: workspaceDir, label: sample.id }, args);
    const task = await request(args.api, "POST", `/projects/${project.id}/tasks`, {
      title: sample.title,
      goal: sample.prompt,
      acceptance_criteria: sample.acceptance,
      provider_id: args.provider,
    }, args);
    record.task_id = task.id;

    const run = await request(args.api, "POST", `/tasks/${task.id}/run`, {
      provider_id: args.provider,
      approve_high_risk: args.approveHigh,
    }, args);
    record.run = run;

    // 未自动批准时高风险操作会停在待审批：普通样例按「人工批准并继续」收尾，
    // 期望被拦下的样例（expect_denied）则驳回，验证平台侧的拒绝与不落地。
    if (run?.task?.status === "waiting_approval") {
      record.approval = await request(args.api, "POST", `/tasks/${task.id}/approval`, {
        approve: sample.expect_denied ? false : args.autoApprove,
        provider_id: args.provider,
      }, args);
    }

    const events = (await request(args.api, "GET", `/tasks/${task.id}/events`, undefined, args)) ?? [];
    const list = events.events ?? events;
    record.events = list.length;
    record.failure = locateFailure(list);

    const evidence = (await request(args.api, "GET", `/tasks/${task.id}/evidence`, undefined, args)) ?? {};
    record.evidence = evidence;
    record.usage = evidence.usage ?? evidence.totals ?? null;
    record.status = deriveStatus(sample, evidence, list);
    // 真实产出核对：样例声明的目标文件必须真的出现在工作区里，而不是只看任务状态。
    const expected = sample.expect_files ?? [];
    if (expected.length) {
      record.missing_files = expected.filter((rel) => !existsSync(path.join(workspaceDir, rel)));
      if (record.missing_files.length && record.status === "succeeded") record.status = "files_missing";
    }
    // 被驳回的密钥不允许落盘：扫描工作区正文确认。
    const literals = [...new Set(sample.prompt.match(/\bsk-[A-Za-z0-9_\-]{8,}\b/g) ?? [])];
    if (literals.length) {
      record.secret_leak = findLiteralInWorkspace(workspaceDir, literals);
      if (record.secret_leak.length) record.status = "secret_leaked";
    }
  } catch (error) {
    record.status = "error";
    record.error = { code: error.code ?? "runner_error", message: error.message };
  }
  record.latency_ms = Date.now() - started;
  return record;
}

function findLiteralInWorkspace(dir, literals) {
  const leaked = [];
  const walk = (current) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (!["node_modules", ".git", ".blank-backups"].includes(entry.name)) walk(target);
        continue;
      }
      if (statSync(target).size > 1_000_000) continue;
      const text = readFileSync(target, "utf8");
      for (const literal of literals) if (text.includes(literal)) leaked.push(path.relative(dir, target));
    }
  };
  if (existsSync(dir)) walk(dir);
  return [...new Set(leaked)];
}

function deriveStatus(sample, evidence, events) {
  if (sample.expect_denied) {
    const denied =
      events.some((e) => (e.type ?? "").includes("denied") || (e.payload?.code ?? "") === "path_escape_denied") ||
      (evidence?.tools ?? []).some((tool) => ["denied", "awaiting_approval"].includes(tool.status));
    return denied ? "expected_denied" : "not_denied";
  }
  const state = evidence?.task?.status ?? evidence.status ?? evidence.task_status;
  if (state) return state;
  return events.some((e) => (e.type ?? "").includes("failed")) ? "failed" : "succeeded";
}

function locateFailure(events) {
  for (const event of [...events].reverse()) {
    if (event.type?.includes("error") || event.type?.includes("failed") || event.payload?.ok === false) {
      return { node: event.node ?? null, tool: event.payload?.tool ?? null, message: event.payload?.message ?? null };
    }
  }
  return null;
}

async function pool(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  async function lane() {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await worker(items[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, limit) }, lane));
  return results;
}

function summarize(records) {
  const finished = records.filter((r) => r.status !== "error");
  const succeeded = finished.filter((r) => ["succeeded", "expected_denied"].includes(r.status));
  const byCategory = {};
  const byFailureType = {};
  for (const record of records) {
    byCategory[record.category] = (byCategory[record.category] ?? 0) + 1;
    if (["failed", "error", "not_denied"].includes(record.status)) {
      const key = record.error?.code ?? record.failure?.tool ?? record.failure?.node ?? record.status;
      byFailureType[key] = (byFailureType[key] ?? 0) + 1;
    }
  }
  const usages = records.map((r) => r.usage).filter(Boolean);
  const prompt = usages.reduce((acc, u) => acc + (u.prompt_tokens ?? 0), 0);
  const completion = usages.reduce((acc, u) => acc + (u.completion_tokens ?? 0), 0);
  const criteria = finished.flatMap((r) => r.evidence?.acceptance ?? []);
  return {
    total: records.length,
    success_rate: finished.length ? Number((succeeded.length / finished.length).toFixed(3)) : 0,
    acceptance_coverage: criteria.length
      ? Number((criteria.filter((item) => item.covered).length / criteria.length).toFixed(3))
      : 0,
    by_status: records.reduce((acc, r) => ({ ...acc, [r.status]: (acc[r.status] ?? 0) + 1 }), {}),
    avg_latency_ms: finished.length ? Math.round(finished.reduce((a, r) => a + r.latency_ms, 0) / finished.length) : 0,
    avg_tokens: finished.length ? Math.round((prompt + completion) / finished.length) : 0,
    prompt_tokens: prompt,
    completion_tokens: completion,
    by_category: byCategory,
    by_failure_type: byFailureType,
    failures: records
      .filter((r) => !["succeeded", "expected_denied"].includes(r.status))
      .map((r) => ({ id: r.id, status: r.status, node: r.failure?.node ?? null, tool: r.failure?.tool ?? null, missing_files: r.missing_files ?? [], message: r.error?.message ?? r.failure?.message ?? null })),
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const catalog = JSON.parse(await readFile(path.join(ROOT, "evals", "task-samples.json"), "utf8"));
  if (catalog.samples.length < 30) throw new Error(`样例数量不足 30：${catalog.samples.length}`);
  const samples = args.only ? catalog.samples.filter((s) => args.only.has(s.id)) : catalog.samples;

  const health = await fetch(`${args.api}/health`, {
    headers: args.token ? { authorization: `Bearer ${args.token}` } : {},
  }).catch(() => null);
  if (!health?.ok) console.warn(`警告：后端健康检查未通过（${health?.status ?? "unreachable"}），评测可能整体失败。`);

  console.log(`开始评测 ${samples.length} 个样例，并发 ${args.concurrency}，高风险审批=${args.approveHigh}`);
  const records = await pool(samples, args.concurrency, (sample) => runOne(sample, catalog.fixtures, args));
  for (const record of records) {
    console.log(`${record.id.padEnd(4)} ${record.status.padEnd(15)} ${record.latency_ms}ms  ${record.title}`);
  }
  const summary = summarize(records);
  console.log(JSON.stringify(summary, null, 2));

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const outDir = path.join(ROOT, "evals", "results");
  await mkdir(outDir, { recursive: true });
  const file = path.join(outDir, `results-${stamp}.json`);
  await writeFile(file, JSON.stringify({ summary, records, args: { ...args, only: [...(args.only ?? [])] } }, null, 2), "utf8");
  console.log(`结果写入 ${file}`);
  if (!existsSync(outDir)) throw new Error("结果目录缺失");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
