"""铺出真实运行链路需要的团队数据，并核对外层可见的证据。

由 run.mjs 注入环境：TEAM_E2E_API、TEAM_E2E_STUB_PORT、TEAM_E2E_WORK。
只能连隔离库上的测试后端，绝不能指向开发库。
"""

import json
import os
import time
from pathlib import Path

import httpx

WORK = Path(os.environ["TEAM_E2E_WORK"])
BASE = os.environ["TEAM_E2E_API"] + "/api/v1"
STUB_PORT = os.environ.get("TEAM_E2E_STUB_PORT", "8097")
client = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
checks: list[dict] = []


def check(name: str, ok, detail: str = "") -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"{'PASS' if ok else 'FAIL'} {name} :: {str(detail)[:300]}")
    return bool(ok)


def post(path: str, body: dict, expected: int = 201) -> dict:
    resp = client.post(path, json=body)
    assert resp.status_code == expected, f"{path}: {resp.status_code} {resp.text}"
    return resp.json()


def get(path: str, **params) -> dict | list:
    resp = client.get(path, params=params)
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"
    return resp.json()


def wait_round(group_id: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get(f"/team/groups/{group_id}/state")
        if (last.get("round") or {}).get("status") in ("completed", "failed", "cancelled"):
            return last
        time.sleep(0.4)
    raise AssertionError(f"round timeout: {json.dumps(last, ensure_ascii=False)[:1500]}")


def main() -> int:
    (WORK / "ws").mkdir(parents=True, exist_ok=True)
    (WORK / "ws" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    get("/team/bootstrap")  # 首访会建出主理人 Bot
    project = post("/projects", {"name": "team-e2e"})
    workspace = post(
        f"/projects/{project['id']}/workspaces",
        {"path": str(WORK / "ws").replace("\\", "/")},
    )
    provider = post(
        "/providers",
        {
            "name": "stub-team-provider",
            "kind": "openai_compatible",
            "base_url": f"http://127.0.0.1:{STUB_PORT}/v1",
            "model": "stub-team",
            "api_key": "sk-mocksecret123456",
            "supports_tools": True,
        },
    )
    backend_bot = post("/team/bots", {"name": "后端", "role": "服务端", "provider_id": provider["id"]})
    frontend_bot = post("/team/bots", {"name": "前端", "role": "页面", "provider_id": provider["id"]})
    created = post(
        "/team/groups",
        {
            "project_id": project["id"],
            "name": "交付群",
            "topic": "登录接口联调",
            "member_ids": [backend_bot["id"]],
            "token_budget": 60000,
        },
    )
    group = created["group"]
    state = post(f"/team/groups/{group['id']}/messages", {"content": "@后端 汇报登录接口状态"})
    accepted = state.get("accepted") or {}
    final = wait_round(group["id"])

    check("P1 首轮跑完", final["round"]["status"] == "completed", final["round"]["status"])
    bots = [row for row in final["messages"] if row["role"] == "bot"]
    admin_message = next((row for row in bots if row["bot_name"] == "小白管家" and row["content"]), None)
    backend_message = next((row for row in bots if row["bot_name"] == "后端" and row["content"]), None)
    check("P2 主理人交接给了后端", bool(admin_message) and "后端" in admin_message["content"], admin_message and admin_message["content"])
    check(
        "P3 后端给出了带唯一标记的结论消息",
        bool(backend_message) and "token 解析" in backend_message["content"] and "种子结论" in backend_message["content"],
        backend_message and backend_message["content"],
    )
    if not (admin_message and backend_message):
        failed = [item for item in checks if not item["ok"]]
        print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
        return 1

    events = get(f"/tasks/{admin_message['task_id']}/events")
    types = [event["type"] for event in events]
    tool_done = [
        event
        for event in events
        if event["type"] == "tool.completed" and (event.get("payload") or {}).get("tool") == "team_status"
    ]
    check(
        "P4 工具卡片记录 team_status 成功",
        bool(tool_done) and "团队「交付群」" in tool_done[0]["payload"]["summary"],
        tool_done[0]["payload"].get("summary") if tool_done else types[:12],
    )
    started = next((event for event in events if event["type"] == "task.started"), None)
    check("P5 运行时把团队状态工具挂进了本轮", bool(started) and "team_status" in started["payload"]["tools"], started and started["payload"].get("tools"))

    memory_files = sorted((WORK / "memory").glob("*.md"))
    memory_text = memory_files[0].read_text(encoding="utf-8") if memory_files else ""
    check(
        "P6 结论写进长期记忆并带来源",
        bool(memory_files) and "token 解析" in memory_text and "来源 团队·交付群" in memory_text,
        memory_text[:200],
    )

    hits = get("/team/search", q="种子结论")
    top = (hits["results"] or [{}])[0]
    check(
        "P7 全文索引首条命中就是那条结论",
        bool(hits["results"]) and top.get("message_id") == backend_message["id"] and top.get("group_id") == group["id"],
        top.get("snippet") or "无",
    )

    seed = {
        "project_id": project["id"],
        "workspace_id": workspace["id"],
        "provider_id": provider["id"],
        "group_id": group["id"],
        "admin_bot_id": next(bot["id"] for bot in get("/team/bots") if bot["is_admin"]),
        "backend_bot_id": backend_bot["id"],
        "frontend_bot_id": frontend_bot["id"],
        "user_message_id": accepted.get("message_id"),
        "admin_message_id": admin_message["id"],
        "admin_task_id": admin_message["task_id"],
        "backend_message_id": backend_message["id"],
    }
    (WORK / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [item for item in checks if not item["ok"]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("未通过：" + ", ".join(item["name"] for item in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
