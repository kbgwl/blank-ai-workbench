"""真实 HTTP 链路：一次模型调用必须同时写下单次 / 本回合 / 任务三个口径，且缓存真的在命中。

对标 Codex rollout 里的 `token_usage_record`。这个探针不 import 被测代码、不碰数据库，
只看 :8061 的线上接口真的把哪些字段发出来了：

1. 多步任务至少产生 2 条 `model.usage`（一步一条，才谈得上"累计"）；
2. 每条都带 call/turn/task 三个口径，且数值单调（call ⊆ turn ⊆ task）；
3. 表里的行数 = 事件条数（落库与记账是同一个动作，缺一侧就是两套账）；
4. 证据包的求和 = 最后一条事件的任务级（同一批行，不能各算各的）；
5. DeepSeek 真的在填 cached_tokens（>0）：这是"提示词缓存生效"的唯一可核对证据，
   配置里打开了开关不算；
6. 缓存命中率与 cached/prompt 对得上。

退出码 0 只在七项全部成立时给出。跑一个上线前的老任务（PROBE_TASK_ID=）必须红。
"""

import json
import os
import sys
import time

import httpx

BASE = "http://127.0.0.1:8061/api/v1"
PROVIDER = os.environ.get("PROBE_PROVIDER_ID", "cfc2806d-2f8e-4834-8b11-141d1148e80f")
GOAL = (
    "依次做三件事，每件事都要真的调用工具：\n"
    "1) read_file 读取 README.md；\n"
    "2) read_file 读取 pyproject.toml；\n"
    "3) list_files 列出工作区根目录。\n"
    "每读完一个文件用两三句话说明它是什么，并给出这两个文件里各自的项目名。不要修改任何文件。"
)
NEEDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "total_tokens", "calls")

client = httpx.Client(base_url=BASE, timeout=20.0, trust_env=False)

reuse = os.environ.get("PROBE_TASK_ID", "").strip()
if reuse:
    task_id = reuse
    print(f"reuse_task={task_id}")
    status = client.get(f"/tasks/{task_id}").json().get("status", "")
else:
    projects = client.get("/projects").json()
    project_id = projects[0]["id"] if projects else client.post("/projects", json={"name": "usage-probe"}).json()["id"]
    task = client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "用量三级探针", "prompt": GOAL, "goal": GOAL, "provider_id": PROVIDER},
    ).json()
    task_id = task["id"]
    print(f"project={project_id} task={task_id}")
    # POST /run 是阻塞式的，整轮跑完才返回。
    print(f"run={client.post(f'/tasks/{task_id}/run', json={}, timeout=600.0).status_code}")
    deadline = time.time() + 300
    status = ""
    while time.time() < deadline:
        status = client.get(f"/tasks/{task_id}").json().get("status", "")
        if status in {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_answer"}:
            break
        time.sleep(3)
    print(f"status={status}")

events = client.get(f"/tasks/{task_id}/events").json()
usage_events = [row for row in events if (row.get("type") or "") == "model.usage"]
payloads = [row.get("payload") or {} for row in usage_events]
evidence = client.get(f"/tasks/{task_id}/evidence").json()
usage = evidence.get("usage") or {}
out = rf"E:\tmp\realmodel\evidence-usage-{task_id[:8]}.json"
with open(out, "w", encoding="utf-8") as handle:
    json.dump({"usage_events": payloads, "evidence_usage": usage}, handle, ensure_ascii=False, indent=2)

monotonic = all(
    int(((p.get("call") or {}).get("prompt_tokens") or 0)) <= int(((p.get("turn") or {}).get("prompt_tokens") or 0)) <= int(((p.get("task") or {}).get("prompt_tokens") or 0))
    and int(((p.get("turn") or {}).get("cached_tokens") or 0)) <= int(((p.get("task") or {}).get("cached_tokens") or 0))
    for p in payloads
)
levels = usage.get("levels") or {}
task_level = levels.get("task") or {}
last = payloads[-1] if payloads else {}
cached_total = int(usage.get("cached_tokens") or 0)
prompt_total = int(usage.get("prompt_tokens") or 0)

checks = {
    "多步任务真的每步都记了一条 model.usage（>=2）": len(usage_events) >= 2,
    "每条事件都带 call/turn/task 三个口径且字段齐全": bool(payloads)
    and all(all(isinstance(p.get(level), dict) and all(k in p[level] for k in NEEDS) for level in ("call", "turn", "task")) for p in payloads),
    "三个口径数值单调：单次 ⊆ 本回合 ⊆ 任务": bool(payloads) and monotonic,
    "表里的行数 == 事件条数（落库与记账同一步，没有双发也没有漏发）": int(usage.get("calls") or -1) == len(usage_events),
    "证据包求和 == 最后一条事件的任务级（同一批行，不是两套账）": bool(levels)
    and int(task_level.get("prompt_tokens") or -1) == prompt_total
    and int(task_level.get("cached_tokens") or -1) == cached_total
    and int(task_level.get("total_tokens") or -1) == int(usage.get("total_tokens") or -2)
    and int(task_level.get("calls") or -1) == len(usage_events),
    "DeepSeek 真的在填 cached_tokens（缓存生效，不是只开了开关）": cached_total > 0
    and any(int(((p.get("call") or {}).get("cached_tokens") or 0)) > 0 for p in payloads),
    "命中率与 cached/prompt 对得上": prompt_total > 0
    and abs(float(usage.get("cache_hit_ratio") or -1) - round(cached_total / prompt_total, 4)) < 1e-9,
}
print(f"events={len(events)} usage_events={len(usage_events)} evidence_calls={usage.get('calls')}")
print(f"evidence_usage={json.dumps({k: v for k, v in usage.items() if k != 'levels'}, ensure_ascii=False)}")
print(f"levels={json.dumps(levels, ensure_ascii=False)[:600]}")
if last:
    print(f"last_call={json.dumps(last.get('call') or {}, ensure_ascii=False)[:300]}")
    print(f"last_task={json.dumps(last.get('task') or {}, ensure_ascii=False)[:300]}")
for name, passed in checks.items():
    print(f"{'PASS' if passed else 'FAIL'} {name}")
print(f"written={out}")
sys.exit(0 if all(checks.values()) else 1)
