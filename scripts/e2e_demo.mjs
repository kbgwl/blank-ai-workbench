// v0.4/v0.5 端到端演示：建项目 → 绑工作区 → 建任务 → 运行 → 轮询事件 → 取 Diff 与证据
// 用法：node scripts/e2e_demo.mjs [providerId]
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const API = process.env.BLANK_API_BASE ?? "http://localhost:8000/api/v1";
const WS = resolve(process.cwd(), "data/demo-ws");
const providerId = process.argv[2] ?? "";

mkdirSync(WS, { recursive: true });
writeFileSync(resolve(WS, "calc.py"), "def add(a, b):\n    return a - b\n");
writeFileSync(resolve(WS, "test_calc.py"), "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n");

async function req(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

const project = await req("POST", "/projects", {
  name: "演示工作区 demo-ws",
  description: `端到端验证 · ${new Date().toISOString()}`,
});
await req("POST", `/projects/${project.id}/workspaces`, { path: WS, label: "demo" });

const task = await req("POST", `/projects/${project.id}/tasks`, {
  title: "修复 add 函数并跑通测试",
  goal: [
    "calc.py 里的 add 错误地用了减法，请改为加法，然后运行测试验证 test_calc.py 通过。",
    "目标文件 calc.py 的最终内容：",
    "```python",
    "def add(a, b):",
    "    return a + b",
    "```",
  ].join("\n"),
  acceptance_criteria: ["pytest test_calc.py 全部通过", "add(2, 3) 返回 5"],
  max_steps: 6,
  ...(providerId ? { provider_id: providerId } : {}),
});
console.log(`task=${task.id} status=${task.status}`);

let run;
try {
  run = await req("POST", `/tasks/${task.id}/run`, { approve_high_risk: false });
} catch (error) {
  run = { error: String(error).slice(0, 300) };
}
console.log(`run -> ${JSON.stringify(run).slice(0, 300)}`);

let seq = 0;
let done = false;
const deadline = Date.now() + 240_000;
while (!done && Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 2500));
  const events = await req("GET", `/tasks/${task.id}/events?after_seq=${seq}`);
  for (const ev of events) {
    seq = Math.max(seq, ev.seq);
    const p = JSON.stringify(ev.payload).slice(0, 220);
    console.log(`#${ev.seq} [${ev.node}] ${ev.type} ${p}`);
  }
  const current = await req("GET", `/tasks/${task.id}`);
  done = ["succeeded", "failed", "cancelled", "waiting_approval", "paused"].includes(current.status);
  if (done) console.log(`\nstatus=${current.status} steps=${current.steps_used} failure=${current.failure_reason}`);
}

const evidence = await req("GET", `/tasks/${task.id}/evidence`);
console.log(`\nevidence files=${JSON.stringify(evidence.file_changes ?? evidence.files ?? null).slice(0, 400)}`);
console.log(`validation=${JSON.stringify(evidence.validation ?? null).slice(0, 400)}`);
const diff = await req("GET", `/tasks/${task.id}/diff`);
console.log(`\ndiff:\n${JSON.stringify(diff).slice(0, 1200)}`);
