"""真实 HTTP 链路：子代理花的钱必须进同一个任务账本，而且要能看出父子分叉。

#32 把三级口径（单次 / 本回合 / 任务）接在了主代理上，但留了一条缺口：子代理走的是
另一条循环，钱真花了却不落库、不发事件，于是"任务级"永远比实际花费少。#33 把它接上，
这个探针只看 :8061 的线上接口真的发出来了什么（不 import 被测代码、不碰数据库）：

1. 任务真的派出了子代理（`subagent.started` + `subagent.completed`）；
2. 有 `depth>=1` 的 `model.usage` 事件——子代理自己落的那笔账（这就是 #33 的核心）；
3. 子代理的事件带 `agent_path`（形如 `root/sub-N`），事后能还原是哪条线程花的；
4. 每条事件三级齐全且数值单调（单次 ⊆ 本回合 ⊆ 任务）；
5. **线上出现真分叉**：至少一条事件 turn ≠ task（#32 只能在一个会话里跑，两者数值相等，
   三级各自的意义没在真实形状上证过）；
6. 证据包的行数 == 事件条数（父子同池，没有双发也没有漏发）；
7. 证据包求和 == 最后一条事件的任务级（同一批行，不是两套账）；
8. 负对照：一个没有子代理的历史任务里，`depth>=1` 的用量事件必须为 0，
   且它自己得有用量事件——对照必须是"能失败"的，不是空表点头。

退出码 0 只在八项全部成立时给出。
"""

import json
import os
import sys
import time

import httpx

BASE = "http://127.0.0.1:8061/api/v1"
PROVIDER = os.environ.get("PROBE_PROVIDER_ID", "cfc2806d-2f8e-4834-8b11-141d1148e80f")
GOAL = (
    "这件事必须通过子代理完成，你自己不要读文件：\n"
    "1) 调用 spawn_agent 工具，goal 写「用 read_file 读取 README.md，并用两三句话复述它的内容」，"
    "isolate 设为 false，max_steps 设为 3；\n"
    "2) 拿到子代理的结论后，用一句话说明这个项目是什么。\n"
    "不要修改任何文件，也不要调用除 spawn_agent 之外的工具。"
)
LEVEL_KEYS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "calls")

client = httpx.Client(base_url=BASE, timeout=20.0, trust_env=False)


def usage_payloads(task_id: str) -> tuple[list[dict], list[dict]]:
    events = client.get(f"/tasks/{task_id}/events").json()
    rows = [row for row in events if (row.get("type") or "") == "model.usage"]
    return events, [row.get("payload") or {} for row in rows]


def level(payload: dict, name: str) -> dict:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


reuse = os.environ.get("PROBE_TASK_ID", "").strip()
if reuse:
    task_id = reuse
    print(f"reuse_task={task_id}")
    status = client.get(f"/tasks/{task_id}").json().get("status", "")
else:
    projects = client.get("/projects").json()
    project_id = projects[0]["id"] if projects else client.post("/projects", json={"name": "subagent-usage"}).json()["id"]
    task = client.post(
        f"/projects/{project_id}/tasks",
        json={"title": "子代理用量入账探针", "prompt": GOAL, "goal": GOAL, "provider_id": PROVIDER},
    ).json()
    task_id = task["id"]
    print(f"project={project_id} task={task_id}")
    print(f"run={client.post(f'/tasks/{task_id}/run', json={}, timeout=600.0).status_code}")
    deadline = time.time() + 300
    status = ""
    while time.time() < deadline:
        status = client.get(f"/tasks/{task_id}").json().get("status", "")
        if status in {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_answer"}:
            break
        time.sleep(3)
    print(f"status={status}")

events, payloads = usage_payloads(task_id)
types = [row.get("type") or "" for row in events]
child_events = [p for p in payloads if int(p.get("depth") or 0) >= 1]
evidence = client.get(f"/tasks/{task_id}/evidence").json()
usage = evidence.get("usage") or {}
levels = usage.get("levels") or {}
out = rf"E:\tmp\realmodel\evidence-subagent-usage-{task_id[:8]}.json"
with open(out, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "subagent_events": [row for row in events if (row.get("type") or "").startswith("subagent")],
            "usage_events": payloads,
            "evidence_usage": usage,
        },
        handle,
        ensure_ascii=False,
        indent=2,
    )

monotonic = all(
    int(level(p, "call").get("prompt_tokens") or 0)
    <= int(level(p, "turn").get("prompt_tokens") or 0)
    <= int(level(p, "task").get("prompt_tokens") or 0)
    and int(level(p, "turn").get("cached_tokens") or 0) <= int(level(p, "task").get("cached_tokens") or 0)
    for p in payloads
)
complete_levels = bool(payloads) and all(
    all(isinstance(p.get(name), dict) and all(k in p[name] for k in LEVEL_KEYS) for name in ("call", "turn", "task"))
    for p in payloads
)
diverged = any(
    int(level(p, "turn").get("prompt_tokens") or 0) != int(level(p, "task").get("prompt_tokens") or 0)
    for p in payloads
)
task_level = levels.get("task") or {}

# 负对照：找一个没有子代理、但有用量事件的历史任务
control_id, control_children, control_usage = "", -1, 0
for row in client.get("/tasks", params={"limit": 60}).json():
    candidate = row.get("id") or ""
    if not candidate or candidate == task_id:
        continue
    candidate_events, candidate_payloads = usage_payloads(candidate)
    if any((item.get("type") or "").startswith("subagent.") for item in candidate_events):
        continue
    if not candidate_payloads:
        continue
    control_id = candidate
    control_children = sum(1 for p in candidate_payloads if int(p.get("depth") or 0) >= 1)
    control_usage = len(candidate_payloads)
    break

checks = {
    "任务真的派出了子代理（started + completed）": "subagent.started" in types and "subagent.completed" in types,
    "子代理的调用进了账（有 depth>=1 的 model.usage）": bool(child_events),
    "子代理事件带得出线程身份（agent_path 形如 root/sub-N 且 role=subagent）": bool(child_events)
    and all(
        str(p.get("agent_path") or "").startswith("root/")
        and p.get("agent_path") != "root"
        and p.get("role") == "subagent"
        for p in child_events
    ),
    "每条事件三级齐全且单调（单次 ⊆ 本回合 ⊆ 任务）": complete_levels and monotonic,
    "线上出现真分叉：至少一条事件 turn != task": diverged,
    "证据包行数 == 事件条数（父子同池，无双发无漏发）": int(usage.get("calls") or -1) == len(payloads),
    "证据包求和 == 最后一条事件的任务级（同一批行）": bool(task_level)
    and int(task_level.get("prompt_tokens") or -1) == int(usage.get("prompt_tokens") or -2)
    and int(task_level.get("calls") or -1) == len(payloads)
    and int(task_level.get("cached_tokens") or -1) == int(usage.get("cached_tokens") or -2),
    "负对照：没有子代理的历史任务里 depth>=1 的用量事件为 0（且它自己有用量事件）": control_usage > 0
    and control_children == 0,
}
print(f"events={len(events)} usage_events={len(payloads)} child_usage_events={len(child_events)}")
print(f"evidence_usage={json.dumps({k: v for k, v in usage.items() if k != 'levels'}, ensure_ascii=False)}")
if child_events:
    print(f"first_child={json.dumps(child_events[0], ensure_ascii=False)[:520]}")
for name, passed in checks.items():
    print(f"{'PASS' if passed else 'FAIL'} {name}")
print(f"control_task={control_id} control_usage={control_usage} control_children={control_children}")
print(f"written={out}")
sys.exit(0 if all(checks.values()) else 1)
