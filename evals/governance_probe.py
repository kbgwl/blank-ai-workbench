"""真实 HTTP 链路取一份带治理快照的证据包，供前端投影校验使用。

只用隔离实例 :8061 与它自己的库，不碰用户常驻的 :8000/:3000。
"""

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8061/api/v1"
PROVIDER = "cfc2806d-2f8e-4834-8b11-141d1148e80f"

client = httpx.Client(base_url=BASE, timeout=20.0)

projects = client.get("/projects").json()
project_id = projects[0]["id"] if projects else client.post("/projects", json={"name": "governance-probe"}).json()["id"]
print(f"project={project_id}")

task = client.post(
    f"/projects/{project_id}/tasks",
    json={
        "title": "读 README 第一行",
        "prompt": "读一下工作区里的 README.md，只告诉我它的第一行写了什么。不要修改任何文件。",
        "goal": "读一下工作区里的 README.md，只告诉我它的第一行写了什么。不要修改任何文件。",
        "provider_id": PROVIDER,
    },
).json()
task_id = task["id"]
print(f"task={task_id}")

run = client.post(f"/tasks/{task_id}/run", json={})
print(f"run={run.status_code}")

deadline = time.time() + 240
status = ""
while time.time() < deadline:
    detail = client.get(f"/tasks/{task_id}").json()
    status = detail.get("status", "")
    if status in {"succeeded", "failed", "cancelled", "waiting_approval", "waiting_answer"}:
        break
    time.sleep(3)
print(f"status={status}")

evidence = client.get(f"/tasks/{task_id}/evidence").json()
out = r"E:\tmp\realmodel\evidence-governance.json"
with open(out, "w", encoding="utf-8") as handle:
    json.dump(evidence, handle, ensure_ascii=False, indent=2)

block = evidence.get("governance") or {}
first = block.get("first") or {}
print(f"snapshots={block.get('snapshots')} modes={block.get('modes')} models={block.get('models')}")
print(f"sandbox={json.dumps(first.get('sandbox'), ensure_ascii=False)}")
print(f"feature_sources={sorted({v.get('source') for v in (first.get('features') or {}).values()})}")
print(f"written={out}")
sys.exit(0 if block.get("snapshots") else 1)
