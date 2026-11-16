#!/usr/bin/env node
/**
 * Anthropic 式 UI 评测：任务 -> 多次独立采样 -> 隐藏判据评分 -> pass@k。
 *
 * 口径与 SWE-bench / Terminal-Bench 一类做法一致：
 *   1. 同一提示词、全新空工作区，模型不知道判据内容；
 *   2. 用任务自带的隐藏判据（grader）自动评分，不读模型自述；
 *   3. 多次独立采样，报告每次得分、pass@1 均值与「至少一次全过」；
 *   4. 开跑前先让参考实现过同一套 grader，38/38 才继续（装置自检）；
 *      判据本身的区分力由 grader 的变异电池保证（见仓库 #400 记录）。
 *
 * 用法：
 *   node evals/uibench/run_eval.mjs --trials 2 --out E:/tmp/uibench-eval-20261116
 *   可选：--api http://127.0.0.1:8066/api/v1 --provider <id>
 *         --timeout-min 25 --max-steps 40 --port-base 18980
 */
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};

const TRIALS = Number(opt("trials", "1"));
const API = opt("api", "http://127.0.0.1:8066/api/v1");
const PROVIDER = opt("provider", "25bfbf29-99a8-4bcd-8103-3b646212ce0d");
const TIMEOUT_MS = Number(opt("timeout-min", "25")) * 60_000;
const MAX_STEPS = Number(opt("max-steps", "40"));
const PORT_BASE = Number(opt("port-base", "18980"));
const OUT = path.resolve(opt("out", `E:/tmp/uibench-eval-${Date.now()}`));
const NODE = process.execPath;
const GRADER = path.join(HERE, "grader", "tortureprobe.mjs");
const REF = path.join(HERE, "grader", "ref");
const PROMPT = readFileSync(path.join(HERE, "task", "incident-console.prompt.txt"), "utf8");
const TASK_TITLE = "uibench-incident-console";
const STAMP = new Date().toISOString().replace(/[:.]/g, "-");

mkdirSync(OUT, { recursive: true });

async function api(method, url, body) {
  const resp = await fetch(API + url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = { raw: text };
  }
  return { status: resp.status, json };
}

function listFiles(dir) {
  const out = [];
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const p = path.join(d, name);
      if (statSync(p).isDirectory()) walk(p);
      else out.push(path.relative(dir, p).replaceAll("\\", "/"));
    }
  };
  walk(dir);
  return out.sort();
}

function grade(dir, tag, slot) {
  const probeJson = path.join(OUT, `${tag}.probe.json`);
  const shots = path.join(OUT, "shots", tag);
  const log = path.join(OUT, `${tag}.probe.log`);
  const files = existsSync(dir) ? listFiles(dir) : [];
  if (!files.includes("index.html")) {
    const result = { tag, score: 0, total: null, red: ["没有 index.html"], files, probeJson: null };
    writeFileSync(path.join(OUT, `${tag}.grade.json`), JSON.stringify(result, null, 2), "utf8");
    return result;
  }
  const proc = spawnSync(
    NODE,
    [
      GRADER,
      "--dir",
      dir,
      "--out",
      probeJson,
      "--shots",
      shots,
      "--http-port",
      String(PORT_BASE + slot * 2),
      "--cdp-port",
      String(PORT_BASE + 100 + slot * 2),
    ],
    { encoding: "utf8", timeout: 10 * 60_000 },
  );
  writeFileSync(log, `${proc.stdout ?? ""}\n${proc.stderr ?? ""}`, "utf8");
  if (!existsSync(probeJson)) {
    // grader 崩溃/超时不算产品没做到，单独标出来，别混进逐条判据统计。
    const result = {
      tag,
      score: 0,
      total: null,
      red: ["grader 未产出报告"],
      files,
      probeJson: null,
      graderRc: proc.status,
      gradingError: `probe 无输出（rc=${proc.status}, signal=${proc.signal ?? "-"}）`,
    };
    writeFileSync(path.join(OUT, `${tag}.grade.json`), JSON.stringify(result, null, 2), "utf8");
    return result;
  }
  const probe = JSON.parse(readFileSync(probeJson, "utf8"));
  const criteria = probe.steps.filter((s) => /^\d+\.\d+ /.test(s.name));
  const red = criteria.filter((s) => !s.ok).map((s) => s.name);
  const result = {
    tag,
    score: criteria.length - red.length,
    total: criteria.length,
    red,
    files,
    probeJson,
    graderRc: proc.status,
  };
  writeFileSync(path.join(OUT, `${tag}.grade.json`), JSON.stringify(result, null, 2), "utf8");
  return result;
}

async function runTrial(index) {
  const ws = path.join(OUT, `trial-${index}`, "workspace");
  mkdirSync(ws, { recursive: true });
  const project = await api("POST", "/projects", { name: `uibench-${STAMP}-${index}` });
  if (project.status !== 201) throw new Error(`project ${project.status}`);
  const bind = await api("POST", `/projects/${project.json.id}/workspaces`, { path: ws });
  if (bind.status !== 201) throw new Error(`bind ${bind.status}`);
  const task = await api("POST", `/projects/${project.json.id}/tasks`, {
    title: TASK_TITLE,
    goal: PROMPT,
    max_steps: MAX_STEPS,
  });
  if (task.status !== 201) throw new Error(`task ${task.status} ${JSON.stringify(task.json).slice(0, 300)}`);
  const id = task.json.id;

  const started = Date.now();
  let terminal = null;
  let approvals = 0;
  // /run 在服务端是同步执行的：请求头要等整个任务收场才返回（实测被 undici 的
  // 300s 头部超时打断，采样直接记成 harness_error）。所以只发出、不等待，
  // 终态一律靠下面的轮询判定；起跑失败的证据由服务端写进 task.failed 事件。
  api("POST", `/tasks/${id}/run`, { provider_id: PROVIDER }).catch(() => {});
  while (Date.now() - started < TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, 3000));
    const cur = await api("GET", `/tasks/${id}`);
    const status = cur.json?.status;
    if (status === "waiting_approval") {
      approvals += 1;
      await api("POST", `/tasks/${id}/approval`, {
        approve: true,
        provider_id: PROVIDER,
        scope: "once",
      }).catch(() => {});
      continue;
    }
    if (status && !["queued", "running"].includes(status)) {
      terminal = cur.json;
      break;
    }
  }
  const elapsedMs = Date.now() - started;
  // 事件接口上限 2000，传更大值会 422；采样只用尾部事件，够用。
  const ev = await api("GET", `/tasks/${id}/events?limit=2000`);
  const items = ev.json?.items ?? ev.json?.events ?? (Array.isArray(ev.json) ? ev.json : []);
  writeFileSync(path.join(OUT, `trial-${index}.events.json`), JSON.stringify(items, null, 2), "utf8");
  const done = [...items].reverse().find((e) => e?.type === "task.completed");
  const meta = {
    index,
    taskId: id,
    status: terminal?.status ?? "runner_timeout",
    elapsedMs,
    approvals,
    eventCount: items.length,
    delivery: done?.payload?.summary ?? terminal?.result_summary ?? "",
  };
  writeFileSync(path.join(OUT, `trial-${index}.run.json`), JSON.stringify(meta, null, 2), "utf8");
  return meta;
}

const startedAt = new Date().toISOString();
console.log(`[eval] grader 自检：参考实现`);
const control = grade(REF, "control", 0);
if (control.total !== 38 || control.score !== 38) {
  console.error(`[eval] 参考实现未达 38/38（${control.score}/${control.total}），装置读数作废`);
  writeFileSync(
    path.join(OUT, "eval.json"),
    JSON.stringify({ startedAt, aborted: "control_failed", control }, null, 2),
    "utf8",
  );
  process.exit(2);
}
console.log(`[eval] 参考实现 38/38，判据可用`);

const trials = [];
for (let i = 1; i <= TRIALS; i++) {
  console.log(`[eval] 第 ${i}/${TRIALS} 次采样…`);
  let meta;
  try {
    meta = await runTrial(i);
  } catch (error) {
    meta = { index: i, status: "harness_error", error: String(error).slice(0, 800) };
    // 装置自己的失败要落盘，否则汇总里只剩一个 harness_error，没法复查。
    writeFileSync(path.join(OUT, `trial-${i}.run.json`), JSON.stringify(meta, null, 2), "utf8");
  }
  const graded = grade(path.join(OUT, `trial-${i}`, "workspace"), `trial-${i}`, i);
  trials.push({ ...meta, ...graded });
  console.log(`[eval] 第 ${i} 次：${graded.score}/${graded.total ?? "?"}，状态 ${meta.status}`);
}

const total = control.total;
// 装置自身失败（没抓到交付物、grader 崩溃）不是模型读数，算进 pass@1 会把装置问题
// 记成产品问题；单独计数，均值只按判据真正跑起来的那些采样算。
const graded = trials.filter((t) => t.total === total);
const scores = graded.map((t) => t.score);
const passAt1 = scores.length ? scores.reduce((a, b) => a + b, 0) / (scores.length * total) : null;
const allPass = graded.some((t) => t.score === t.total);
const names = new Map();
for (const t of graded) for (const name of t.red) names.set(name, (names.get(name) ?? 0) + 1);
const perCriterion = [...names.entries()]
  .map(([name, failed]) => ({ name, failed, passed: graded.length - failed }))
  .sort((a, b) => b.failed - a.failed || a.name.localeCompare(b.name));

const summary = {
  startedAt,
  finishedAt: new Date().toISOString(),
  api: API,
  providerId: PROVIDER,
  promptSha256: createHash("sha256").update(PROMPT, "utf8").digest("hex"),
  trials: trials.map((t) => ({
    index: t.index,
    taskId: t.taskId,
    status: t.status,
    error: t.error,
    elapsedMs: t.elapsedMs,
    approvals: t.approvals,
    score: t.score,
    total: t.total,
    red: t.red,
    files: t.files,
  })),
  control: { score: control.score, total: control.total },
  effectiveTrials: graded.length,
  passAt1,
  allPassInAtLeastOneTrial: allPass,
  perCriterion,
};
writeFileSync(path.join(OUT, "eval.json"), JSON.stringify(summary, null, 2), "utf8");

console.log(`\n== 汇总 ==`);
console.log(`参考对照：${control.score}/${control.total}`);
for (const t of summary.trials) {
  console.log(`第 ${t.index} 次：${t.score}/${t.total ?? "?"}（${t.status}，${Math.round(t.elapsedMs / 1000)}s）红：${t.red.join(" | ") || "无"}${t.error ? `\n  装置错误：${t.error}` : ""}`);
}
console.log(`有效采样：${graded.length}/${TRIALS}${graded.length < TRIALS ? "（其余为装置自身失败，不计入 pass@1）" : ""}`);
console.log(`pass@1 均值：${passAt1 === null ? "无有效采样" : `${(passAt1 * 100).toFixed(1)}%`}`);
console.log(`至少一次全过（pass@${graded.length || TRIALS}）：${allPass ? "是" : "否"}`);
console.log(`json: ${path.join(OUT, "eval.json")}`);
