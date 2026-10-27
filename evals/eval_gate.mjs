#!/usr/bin/env node
// 评测集成功率门禁（任务 #104，原路线图 #91 / 旧 #85）：把 run_evals.mjs 的量出来的成功率接进发布门禁。
// 用法： node evals/eval_gate.mjs [--results evals/results/results-xxx.json] [--config evals/gate_config.json]
//                              [--samples evals/task-samples.json] [--min-success-rate 0.99]
// 退出码语义与 evals/dialect_e2e.py 同一套纪律（那条 `退出码=` 行是本装置唯一的读数，包装命令的 rc 不算证据）：
//   0 = 成功率判据绿，且每一格前提（配置、结果文件、整轮跑完、样本数）都在场；
//   1 = 有格红：量出来的成功率低于阈值；
//   2 = 这一格今天没法判：结果文件缺失 / JSON 读不出（上一轮没跑完写坏也长这样）/
//       样本不足 / 只跑了 --only 子集 / 记录里有 error（那一格没人判）或 unknown。
// 「今天没法判」绝不允许伪装成「通过」——所以 2 与 1 都是非零，只有判据成立且全绿才 0。
// 阈值必须来自实测（写死在 gate_config.json 并附出处），不许拍脑袋。
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const UNJUDGED = "unjudged"; // 今天没法判
const RED = "red"; // 有格红
const PASS = "pass";

function parseArgs(argv) {
  const args = {
    results: null,
    config: path.join(ROOT, "evals", "gate_config.json"),
    samples: path.join(ROOT, "evals", "task-samples.json"),
    thresholdOverride: null,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--results") args.results = argv[++i];
    else if (arg === "--config") args.config = argv[++i];
    else if (arg === "--samples") args.samples = argv[++i];
    else if (arg === "--min-success-rate") args.thresholdOverride = argv[++i];
    else if (arg === "--help") {
      console.log("用法：node evals/eval_gate.mjs [--results FILE] [--config FILE] [--samples FILE] [--min-success-rate X]");
      process.exit(0);
    } else {
      console.log(`[ABORT] 不认识的参数：${arg}`);
      process.exit(2);
    }
  }
  return args;
}

/** 挑最新一份 results-*.json：文件名里的时间戳是定长的，按名字字典序就是按时间序。 */
function latestResults(dir) {
  if (!existsSync(dir)) return null;
  const names = readdirSync(dir).filter((n) => /^results-.*\.json$/.test(n)).sort();
  if (!names.length) return null;
  return path.join(dir, names[names.length - 1]);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const checks = [];
  const add = (id, state, detail) => checks.push({ id, state, detail });

  // —— 前提格 1：门禁自己得先有标准。config 读不出 = 这一格今天没法判，不是红，更不是绿。
  let config = null;
  let configError = null;
  try {
    config = JSON.parse(readFileSync(args.config, "utf8"));
  } catch (error) {
    configError = `${args.config}: ${error.code === "ENOENT" ? "文件不存在" : String(error).slice(0, 120)}`;
  }
  const threshold = args.thresholdOverride === null ? config?.min_success_rate : Number(args.thresholdOverride);
  const minSamples = config?.min_samples;
  const thresholdValid = typeof threshold === "number" && Number.isFinite(threshold) && threshold >= 0 && threshold <= 1;
  const samplesValid = typeof minSamples === "number" && Number.isFinite(minSamples) && minSamples > 0;
  if (configError) add("GATE-CONFIG", UNJUDGED, `门禁配置读不出：${configError}——没有标准，这一格今天没法判`);
  else if (!thresholdValid) add("GATE-CONFIG", UNJUDGED, `配置里 min_success_rate 不是 0..1 的数：${JSON.stringify(config?.min_success_rate)}`);
  else if (!samplesValid) add("GATE-CONFIG", UNJUDGED, `配置里 min_samples 不是正数：${JSON.stringify(config?.min_samples)}`);
  else if (args.thresholdOverride !== null)
    add("GATE-CONFIG", PASS, `阈值 ${threshold}（CLI --min-success-rate 覆盖；本轮读数不是 gate_config 的实测阈值，只用于验证判据能红）`);
  else add("GATE-CONFIG", PASS, `阈值 min_success_rate=${threshold}，min_samples=${minSamples}（出处：${config.min_success_rate_source ?? "未注明"}）`);

  // —— 前提格 2：结果文件在场且读得出。半截 JSON = 上一轮没跑完被掐断的形状，落 2。
  const resultsPath = args.results ?? latestResults(path.join(ROOT, "evals", "results"));
  let result = null;
  let resultsError = null;
  if (!resultsPath) add("RESULTS-FILE", UNJUDGED, "evals/results/ 里没有任何 results-*.json：这一格今天没跑过");
  else if (!existsSync(resultsPath)) add("RESULTS-FILE", UNJUDGED, `结果文件不存在：${resultsPath}`);
  else {
    try {
      result = JSON.parse(readFileSync(resultsPath, "utf8"));
      add("RESULTS-FILE", PASS, resultsPath);
    } catch (error) {
      resultsError = String(error).slice(0, 120);
      add("RESULTS-FILE", UNJUDGED, `结果文件读不出 JSON（上一轮没跑完/写坏就是这个形状，不许按「通过」念）：${resultsPath}: ${resultsError}`);
    }
  }

  // —— 前提格 3：结构自洽——summary 与 records 得互相咬合，否则这份读数不可信。
  const summary = result?.summary;
  const records = Array.isArray(result?.records) ? result.records : null;
  if (result && (!summary || typeof summary.success_rate !== "number" || !records))
    add("RESULTS-INTACT", UNJUDGED, "结果文件缺 summary.success_rate 或 records——结构不完整，今天没法判");
  else if (result && records.length !== summary.total)
    add("RESULTS-INTACT", UNJUDGED, `records(${records.length}) 与 summary.total(${summary.total}) 对不上——半截写入，今天没法判`);
  else if (result) add("RESULTS-INTACT", PASS, `records=${records.length} total=${summary.total} success_rate=${summary.success_rate}`);

  // —— 前提格 4：这一轮是整轮。--only 子集 / args 缺失都只能落 2；样本不足同理。
  if (result) {
    const only = result.args?.only;
    if (only === undefined) add("FULL-RUN", UNJUDGED, "结果里没有 args.only——无法证明这一轮是整轮，今天没法判");
    else if (Array.isArray(only) && only.length > 0) add("FULL-RUN", UNJUDGED, `这一轮只跑了 --only ${only.join(",")} 子集——整轮判据不成立，今天没法判`);
    else if (summary.total < minSamples) add("FULL-RUN", UNJUDGED, `样本不足：total=${summary.total} < min_samples=${minSamples}——「跳过」不许冒充「通过」，今天没法判`);
    else add("FULL-RUN", PASS, `整轮 ${summary.total} 例（>= min_samples=${minSamples}）`);

    // —— 前提格 5：样例库逐一对账——少一个样例就是上一轮没跑完，落 2。
    let catalog = null;
    try {
      catalog = JSON.parse(readFileSync(args.samples, "utf8"));
    } catch (error) {
      add("SAMPLES-COVERAGE", UNJUDGED, `样例库读不出：${args.samples}: ${String(error).slice(0, 80)}`);
    }
    if (catalog) {
      const wanted = new Set((catalog.samples ?? []).map((s) => s.id));
      const got = new Set(records.map((r) => r.id));
      const missing = [...wanted].filter((id) => !got.has(id));
      if (missing.length) add("SAMPLES-COVERAGE", UNJUDGED, `结果里缺 ${missing.length} 个样例（${missing.slice(0, 8).join(",")}）——上一轮没跑完`);
      else add("SAMPLES-COVERAGE", PASS, `样例库 ${wanted.size} 例在结果里全部有记录`);
    }

    // —— 前提格 6：每格都得有判定。status=error 是 runner/API 断了（没人判），unknown 是没跑完，
    //    都不许进分母后被「成功率达标」掩盖——一律落 2。
    const undecidable = records.filter((r) => !r.status || ["error", "unknown"].includes(r.status));
    if (undecidable.length)
      add("RECORD-JUDGED", UNJUDGED, `${undecidable.length} 条记录没判出结果（${undecidable.slice(0, 6).map((r) => `${r.id}:${r.status ?? "?"}`).join(", ")}）——这一格今天没法判`);
    else add("RECORD-JUDGED", PASS, `${records.length} 条记录全部有判定`);
  }

  // —— 判据格：成功率阈值。这是唯一「红」的来源；前提格全在场才有资格念它。
  let rateLine = null;
  const preconditionsOk = checks.every((c) => c.state !== UNJUDGED);
  if (preconditionsOk && summary) {
    if (summary.success_rate >= threshold) {
      add("SUCCESS-RATE", PASS, `success_rate=${summary.success_rate} >= 阈值 ${threshold}`);
      rateLine = `成功率 ${summary.success_rate}（阈值 ${threshold}）绿`;
    } else {
      add("SUCCESS-RATE", RED, `success_rate=${summary.success_rate} < 阈值 ${threshold}——红；失败明细：${JSON.stringify(summary.failures ?? []).slice(0, 400)}`);
      rateLine = `成功率 ${summary.success_rate}（阈值 ${threshold}）红`;
    }
  }

  console.log("===== 评测集成功率门禁 =====");
  console.log(`读数对象：${resultsPath ?? "（无结果文件）"}`);
  let rc = 0;
  if (checks.some((c) => c.state === RED)) rc = 1;
  else if (checks.some((c) => c.state === UNJUDGED)) rc = 2;
  for (const c of checks) console.log(`[${c.state === PASS ? "PASS" : c.state === RED ? "RED " : "SKIP"}] ${c.id}: ${c.detail}`);
  if (rateLine) console.log(`判据：${rateLine}`);
  else console.log("判据：前提格有缺席，本轮不念成功率这句话——「今天没法判」不许伪装成「通过」");
  console.log(`\n退出码=${rc}（0=成功率判据绿且每一格前提在场；1=有格红；2=这一格今天没法判）`);
  return rc;
}

process.exit(main());
