"""真实 HTTP 链路：上下文压缩必须留下一条能定位边界的记录。

做法：临时把这台隔离实例上供应商的 context_window 调小（跑完恢复原值），
让一条普通多步任务真的触发摘要折叠，然后只看接口发出来的东西：
`context.compacted` 事件里必须能读出"边界在哪、折了几条、水位是多少"。

这个探针不 import 被测代码、不碰数据库，证明的是线上接口真的把这些字段发出来了。
只用 :8061 与它自己的库；退出码 0 只在六项判定全部成立时给出。
"""

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8061").rstrip("/") + "/api/v1"
PROVIDER = os.environ.get("PROBE_PROVIDER_ID", "cfc2806d-2f8e-4834-8b11-141d1148e80f")
SMALL_WINDOW = int(os.environ.get("PROBE_WINDOW", "2000"))
OUT_DIR = os.environ.get("PROBE_OUT_DIR", r"E:\tmp\realmodel")
GOAL = (
    "依次做三件事，每件事都要真的调用工具：\n"
    "1) read_file 读取 README.md；\n"
    "2) read_file 读取 pyproject.toml；\n"
    "3) list_files 列出工作区根目录。\n"
    "每读完一个文件用一句话说明它是什么。不要修改任何文件。"
)

client = httpx.Client(base_url=BASE, timeout=20.0, trust_env=False)

provider = client.get(f"/providers/{PROVIDER}").json()
window_before = provider.get("context_window")
print(f"provider={PROVIDER} context_window_before={window_before}")

try:
    patched = client.patch(f"/providers/{PROVIDER}", json={"context_window": SMALL_WINDOW})
    print(f"patch_window={patched.status_code} -> {patched.json().get('context_window')}")

    projects = client.get("/projects").json()
    project_id = projects[0]["id"] if projects else client.post("/projects", json={"name": "compaction-probe"}).json()["id"]
    reuse = os.environ.get("PROBE_TASK_ID", "").strip()
    if reuse:
        task_id = reuse
        print(f"reuse_task={task_id}")
    else:
        task = client.post(
            f"/projects/{project_id}/tasks",
            json={"title": "压缩边界探针", "prompt": GOAL, "goal": GOAL, "provider_id": PROVIDER},
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
    compacted = [row for row in events if (row.get("type") or "") == "context.compacted"]
    measured = [row for row in events if (row.get("type") or "") == "context.measured"]
    evidence = client.get(f"/tasks/{task_id}/evidence").json()
    context_block = evidence.get("context") or {}
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"evidence-compaction-{task_id[:8]}.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"events": events, "evidence_context": context_block}, handle, ensure_ascii=False, indent=2)

    first = (compacted[0].get("payload") or {}) if compacted else {}
    windows = [row.get("payload") or {} for row in compacted]
    summary_chars = [int(item.get("summary_chars") or 0) for item in windows]
    response_ids = [str(item.get("compaction_response_id") or "") for item in windows]
    boundaries = [int(item.get("boundary") or 0) for item in windows]
    numbers = [int(item.get("window_number") or 0) for item in windows]
    chained = bool(windows) and numbers == list(range(1, len(windows) + 1)) and all(
        str(windows[index].get("window_id") or "")
        and str(windows[index].get("previous_window_id") or "")
        == str(windows[index - 1].get("window_id") or "")
        for index in range(1, len(windows))
    ) and bool(windows[0].get("window_id"))
    advancing = all(boundaries[index] > boundaries[index - 1] for index in range(1, len(boundaries)))
    replacement_ready = all(
        isinstance(item.get("replacement"), dict)
        and bool((item.get("replacement") or {}).get("dropped"))
        and bool((item.get("replacement") or {}).get("items"))
        for item in windows
    )
    evidence_windows = context_block.get("windows") or []
    evidence_ids = [str(item.get("compaction_response_id") or "") for item in evidence_windows]
    # 真实取值带阶段后缀（tokens:fold / tokens:micro / chars:fold），冒号前才是判定依据。
    trigger_base = str(first.get("triggered_by") or "").split(":")[0]
    failed = [row for row in events if (row.get("type") or "").startswith("task.failed")]
    checks = {
        "摘要由模型写出且每次非空（summary_chars > 0）": bool(windows) and all(value > 0 for value in summary_chars),
        "每次压缩都指认了供应商响应 id": bool(windows) and all(response_ids),
        "窗口编号从 1 起、窗口链首尾相接且边界严格前进": chained and advancing,
        "替换记录同时给出丢掉与新增条目": replacement_ready,
        "证据包窗口链与事件是同一组非空响应 id": bool(windows)
        and evidence_ids == response_ids
        and all(evidence_ids),
        "真的触发了一次压缩（有 context.compacted 事件）": bool(compacted),
        "事件里有边界位置 covered": bool(compacted) and int(first.get("covered") or 0) > 0,
        "事件里区分了水位触发与字符预算触发": bool(compacted) and trigger_base in {"tokens", "chars"},
        "水位读数不是编的（token 模式必须带窗口）": (not compacted)
        or trigger_base != "tokens"
        or (int(first.get("context_window") or 0) == SMALL_WINDOW and float(first.get("water_ratio") or 0) > 0),
        "证据包的聚合与事件对得上": bool(compacted)
        and int(context_block.get("compactions") or 0) == len(compacted)
        and int(context_block.get("boundary") or 0) == sum(int((row.get("payload") or {}).get("folded") or 0) for row in compacted),
        # 折叠切点若落在 tool_calls 配对中间，发给供应商的就是孤儿工具结果：
        # 供应商回 400、整条任务失败（2026-10-22 真实链路实测到，故列为判定）。
        "压缩后的历史没有把工具调用配对切散（任务没被供应商拒）": not failed
        and (bool(reuse) or status == "succeeded"),
    }
    print(f"events={len(events)} measured={len(measured)} compacted={len(compacted)}")
    print(f"first_payload={json.dumps(first, ensure_ascii=False)[:420]}")
    print(f"windows={numbers} boundaries={boundaries} summary_chars={summary_chars}")
    print(f"compaction_response_ids={response_ids}")
    print(f"evidence_context={json.dumps(context_block, ensure_ascii=False)}")
    for row in failed:
        print(f"task_failed_payload={json.dumps(row.get('payload') or {}, ensure_ascii=False)[:300]}")
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print(f"written={out}")
    sys.exit(0 if all(checks.values()) else 1)
finally:
    restore = client.patch(f"/providers/{PROVIDER}", json={"context_window": window_before})
    print(f"restore_window={restore.status_code} -> {restore.json().get('context_window') if restore.status_code < 400 else ''}")
