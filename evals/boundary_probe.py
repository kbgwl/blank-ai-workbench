"""真实 HTTP 链路：命令跑在宿主上时，证据包、事件与审计账必须都能说出来。

对标 Codex 的每次调用都带执行边界。这个探针只走 HTTP，不碰数据库、不 import 被测代码，
所以它证明的是"线上接口真的把这些字段发出来了"，而不是单测里的自说自话。

只用隔离实例 :8061 与它自己的库；跑完把 shell 开关恢复原值。
退出码 0 只在六条断言全部成立时给出。
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8061/api/v1"
PROVIDER = "cfc2806d-2f8e-4834-8b11-141d1148e80f"
AUDIT = Path(os.environ.get("PROBE_AUDIT_DIR", r"E:\tmp\realmodel\audit"))
# 实测教训：`find /c /v ""` 在 cmd 下空参数被吞掉后转为读 stdin，30s 到点被强杀——
# 边界字段照样落进证据，但"命令真的跑完了"这件事无法自证，所以换一条必然立刻结束的命令。
GOAL = (
    "分两步做完即可，不要多做：\n"
    "1) 调用 list_files 工具列出工作区根目录的文件名；\n"
    "2) 必须调用 run_shell 工具执行命令 `echo boundary-probe && ver`（Windows cmd 语法，会立刻返回），"
    "timeout_seconds 用 15。把两个工具的原始返回都贴出来。第 2 步不许用其他工具代替。"
)

# POST /run 是阻塞式的（routes/tasks.py 里直接 await AgentRuntime.run），
# 整条图跑完才返回，所以这一发的读超时必须比整轮时长长；其余请求保持短超时。
# trust_env=False：本机注册表里有系统代理，默认会把 127.0.0.1 也交给代理，表现为满屏 502 而服务端零日志。
client = httpx.Client(base_url=BASE, timeout=20.0, trust_env=False)

before = client.get("/settings/agent").json().get("safety") or {}
print(f"safety_before={json.dumps(before, ensure_ascii=False)}")
if not before.get("shell_enabled") and not os.environ.get("PROBE_TASK_ID"):
    # 负对照只读老任务，不开 shell——避免改动这台实例的安全读数。
    put = client.put("/settings/agent", json={"safety": {"shell_enabled": True, "confirm_all_commands": False}})
    print(f"enable_shell={put.status_code}")

try:
    reuse = os.environ.get("PROBE_TASK_ID", "").strip()
    if reuse:
        # 负对照用：直接读一个已有任务，判定装置必须能对"没跑完/没有只读调用"的数据变红。
        task_id = reuse
        print(f"reuse_task={task_id}")
    else:
        projects = client.get("/projects").json()
        project_id = projects[0]["id"] if projects else client.post("/projects", json={"name": "boundary-probe"}).json()["id"]
        task = client.post(
            f"/projects/{project_id}/tasks",
            json={"title": "宿主执行边界探针", "prompt": GOAL, "goal": GOAL, "provider_id": PROVIDER},
        ).json()
        task_id = task["id"]
        print(f"project={project_id} task={task_id}")

        run = client.post(f"/tasks/{task_id}/run", json={}, timeout=600.0)
        print(f"run={run.status_code}")

        deadline = time.time() + 300
        status = ""
        while time.time() < deadline:
            detail = client.get(f"/tasks/{task_id}").json()
            status = detail.get("status", "")
            if status in {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_answer"}:
                break
            time.sleep(3)
        print(f"status={status}")

    evidence = client.get(f"/tasks/{task_id}/evidence").json()
    # 文件名带任务号：负对照重跑不会把刚才那份正证据盖掉。
    out = rf"E:\tmp\realmodel\evidence-boundary-{task_id[:8]}.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)

    tools = evidence.get("tools") or []
    commands = [row for row in tools if "sandbox_backend" in row]
    events = [
        item
        for item in (evidence.get("timeline") or [])
        if item.get("type") == "tool.completed" and "sandbox_backend" in (item.get("payload") or {})
    ]
    readonly = [row for row in tools if "sandbox_backend" not in row]
    audit_rows = []
    audit_path = AUDIT / "tool-calls.jsonl"
    if audit_path.exists():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("task_id") == task_id:
                    audit_rows.append(entry)

    checks = {
        "命令类调用在工具清单里带边界": bool(commands) and all(row["sandbox_backend"] is None for row in commands),
        # 判定要能失败：只看"有没有行"的话，超时/被拒的行也算过。这里钉住命令的真实回显。
        "命令真的跑完了（状态 ok 且回显带哨兵串）": any(
            row["status"] == "ok" and "boundary-probe" in str(row.get("summary") or "") for row in commands
        ),
        "事件时间线里带边界": bool(events),
        "只读调用不带边界这一项": bool(readonly),
        "审计账按任务落了且能还原": bool(audit_rows) and any(
            (row.get("detail") or {}).get("host_execution") is True for row in audit_rows
        ),
        "审计账里主机路径已掩码": bool(audit_rows)
        and all(str(row.get("workspace", "")).startswith("<workspace:") for row in audit_rows),
    }
    print(f"commands={[row['name'] for row in commands]} readonly={[row['name'] for row in readonly]}")
    print(f"statuses={[row['status'] for row in commands]}")
    chain = ["{0}:{1}".format(row["tool"], row["status"]) for row in audit_rows]
    print("audit_lines={0} chain={1}".format(len(audit_rows), chain))
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print(f"written={out}")
    sys.exit(0 if all(checks.values()) else 1)
finally:
    restore = {key: value for key, value in before.items() if key in ("shell_enabled", "confirm_all_commands")}
    if restore and not os.environ.get("PROBE_TASK_ID"):
        print(f"restore_safety={client.put('/settings/agent', json={'safety': restore}).status_code}")
