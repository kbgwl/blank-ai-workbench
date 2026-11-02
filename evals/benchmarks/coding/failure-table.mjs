#!/usr/bin/env node
/**
 * 把一次 benchmark 结果展开成「逐例事实表」。
 *
 * last-run.json 里只有判分结论（checks 已算了什么），没有交付正文、没有终态原因、
 * 没有审批次数。报告要的是这些原始事实——所以按任务 id 回到评测后端取证：
 * 每个 id 取最新一条同名任务，读 evidence 与 events，逐例一行。
 *
 * 用法：
 *   node failure-table.mjs --report last-run.json --out table.md [--json rows.json] [--failed-only]
 *   BLANK_BENCH_API 默认 http://127.0.0.1:8060/api/v1
 */
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";

const argv = process.argv.slice(2);
const argOf = (name, fallback = null) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[i + 1] : fallback;
};

const API = process.env.BLANK_BENCH_API ?? "http://127.0.0.1:8060/api/v1";
const REPORT = argOf("--report", "last-run.json");
const OUT_MD = argOf("--out");
const OUT_JSON = argOf("--json");
const FAILED_ONLY = argv.includes("--failed-only");

const run = JSON.parse(readFileSync(path.resolve(REPORT), "utf8"));

async function getJson(urlPath) {
  const resp = await fetch(`${API}${urlPath}`);
  if (!resp.ok) throw new Error(`${urlPath} -> ${resp.status}`);
  return resp.json();
}

// /tasks 没有 offset（只有 limit<=200，按创建时间倒序）：取最新一页即可覆盖本轮。
// 之前写成 offset 分页会一直拿到同一页、死循环——接口不认的参数不会报错，只会静默忽略。
const list = await getJson("/tasks?limit=200");
const byTitle = new Map();
for (const task of list) {
  const previous = byTitle.get(task.title);
  // 同名任务取最新一条：评测里每次重跑都会新建，旧的那条是上一次的事实。
  if (!previous || String(task.created_at) > String(previous.created_at)) byTitle.set(task.title, task);
}

const rows = [];
for (const result of run.results) {
  const task = byTitle.get(result.id);
  const row = {
    id: result.id,
    category: result.category,
    status: result.status,
    agentStatus: result.agentStatus ?? result.status,
    expectedStatus: result.expectedStatus,
    attempts: result.attempts ?? 1,
    passed: result.passed,
    failureReason: "",
    testExit: result.testExit ?? null,
    changedFiles: result.changedFiles ?? [],
    unexpectedTouched: result.unexpectedTouched ?? [],
    approvals: 0,
    toolCalls: [],
    delivery: "",
    falseDone: result.checks ? !result.checks.noFalseDone : null,
    unauthorizedHighRisk: result.checks ? !result.checks.highRiskUnauthorized : null,
    checks: result.checks ?? {},
    tokens: result.tokenUsage?.total_tokens ?? null,
  };
  if (task) {
    try {
      const evidence = await getJson(`/tasks/${task.id}/evidence`);
      row.taskId = task.id;
      row.failureReason = String(evidence.task?.failure_reason ?? "");
      row.delivery = String(evidence.task?.result_summary ?? "");
      row.acceptance = (evidence.acceptance ?? []).map((item) => ({
        criterion: item.criterion,
        covered: Boolean(item.covered),
      }));
      row.unverifiedRisks = evidence.unverified_risks ?? [];
    } catch (error) {
      row.failureReason = `证据读取失败：${error.message}`;
    }
    try {
      const events = await getJson(`/tasks/${task.id}/events`);
      row.approvals = events.filter((event) => /^approval\./.test(event.type)).length;
      row.toolCalls = events
        .filter((event) => event.type === "tool.requested")
        .map((event) => event.payload?.tool)
        .filter(Boolean);
    } catch (error) {
      row.toolCalls = [];
      row.approvals = -1;
    }
  } else {
    row.failureReason = "评测后端里没有同名任务（可能是 mock 或已清理）";
  }
  rows.push(row);
}

const cell = (value, max = 90) => {
  const text = String(value ?? "").replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
};

const shown = FAILED_ONLY ? rows.filter((row) => !row.passed) : rows;
const lines = [
  "| 任务 | 类别 | 终态/期望 | 失败原因 | 测试退出码 | 修改文件 | 额外文件 | 审批 | 工具调用 | 交付正文 | falseDone | 未授权高风险 |",
  "|---|---|---|---|---|---|---|---|---|---|---|---|",
];
for (const row of shown) {
  lines.push(
    `| ${row.id} | ${row.category} | ${row.status}/${row.expectedStatus} | ${cell(row.failureReason, 70)} | ${row.testExit} | ${
      row.changedFiles.join(", ") || "-"
    } | ${row.unexpectedTouched.join(", ") || "-"} | ${row.approvals} | ${[...new Set(row.toolCalls)].join("/") || "-"} | ${cell(
      row.delivery,
      100
    )} | ${row.falseDone ? "是" : "否"} | ${row.unauthorizedHighRisk ? "是" : "否"} |`
  );
}

if (OUT_MD) writeFileSync(path.resolve(OUT_MD), `${lines.join("\n")}\n`, "utf8");
else console.log(lines.join("\n"));
if (OUT_JSON) writeFileSync(path.resolve(OUT_JSON), JSON.stringify(rows, null, 2), "utf8");
console.log(`\n共 ${rows.length} 例，未通过 ${rows.filter((row) => !row.passed).length} 例`);
