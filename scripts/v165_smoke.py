"""v0.10.0 安装版后端冒烟：不碰真机库，只读/写一个副本。

用法（先在 8061 用安装版自带 Python 起一个 uvicorn，DB 指向副本）::

    python scripts/v165_smoke.py --db E:/tmp/blank-diag/mig-0033-check.db \
        --base http://127.0.0.1:8061 --project <project_id>

检查的是**接口回显 + 落库事实**，不看 Agent 自述：设置保存后能不能读回、
目标有没有关联到 task/goal/conversation、控制词有没有乱开新轮、观测端点
能不能把三者对上。任何一项失败都以非零退出码结束。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request

API = "/api/v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8061")
    parser.add_argument("--db", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks: list[tuple[str, bool, str]] = []

    def call(method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            args.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener.open(req, timeout=20) as resp:
                raw = resp.read().decode()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return exc.code, raw

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail[:180]}" if detail else ""))

    # 1. 设置页回显：非法档位 422，autonomous 存上并读回。
    status, body = call("GET", f"{API}/settings/agent")
    check("GET /settings/agent", status == 200, f"status={status}")
    settings = body if isinstance(body, dict) else {}
    modes = (settings.get("limits") or {}).get("automation_modes") or []
    check("limits 暴露三种自动化模式", set(modes) == {"manual", "assist", "autonomous"}, str(modes))
    original_mode = (settings.get("background") or {}).get("automation_mode", "assist")

    status, _ = call("PUT", f"{API}/settings/agent", {"background": {"automation_mode": "bypassPermissions"}})
    check("非法自动化模式返回 422", status == 422, f"status={status}")

    status, body = call("PUT", f"{API}/settings/agent", {"background": {"automation_mode": "autonomous"}})
    check("autonomous 保存成功", status == 200 and body["background"]["automation_mode"] == "autonomous", f"status={status}")
    status, body = call("GET", f"{API}/settings/agent")
    check("autonomous 读回一致", status == 200 and body["background"]["automation_mode"] == "autonomous")

    # 2. 会话 → 首句目标 → 补充要求 → 控制词。
    status, thread = call("POST", f"{API}/projects/{args.project}/threads", {"title": "v0.10.0 冒烟"})
    check("创建会话", status in (200, 201), f"status={status}")
    tid = thread["id"]

    status, before = call("GET", f"{API}/threads/{tid}/goal")
    check("首句前没有目标", status == 200 and before is None)

    status, t1 = call("POST", f"{API}/threads/{tid}/turns", {"title": "把登录页按钮改成蓝色", "goal": "把登录页按钮改成蓝色", "scenario": "dev"})
    check("自然语言首句创建一轮", status == 201, f"status={status}")
    task1 = t1["id"]
    spec1 = t1.get("goal_spec") or {}
    check("supervisor 判定落库", bool(spec1.get("supervisor")), str(spec1.get("supervisor"))[:120])
    check("dispatch 快照带 goal_id/task_id/conversation_id",
          spec1.get("dispatch", {}).get("goal_id") == spec1.get("goal_id")
          and spec1.get("dispatch", {}).get("task_id") == task1
          and spec1.get("dispatch", {}).get("conversation_id") == tid,
          json.dumps(spec1.get("dispatch", {}), ensure_ascii=False)[:220])
    check("effective policy 落库", spec1.get("policy", {}).get("mode") in ("manual", "assist", "autonomous"),
          json.dumps(spec1.get("policy", {}), ensure_ascii=False)[:180])

    status, goal1 = call("GET", f"{API}/threads/{tid}/goal")
    check("目标关联首轮", status == 200 and goal1 and goal1["current_task_id"] == task1)
    gid1 = goal1["id"] if goal1 else ""

    status, t2 = call("POST", f"{API}/threads/{tid}/turns", {"title": "再把配色改柔和一点", "goal": "再把配色改柔和一点", "scenario": "dev"})
    check("补充要求另起一轮但复用目标", status == 201, f"status={status}")
    status, goal2 = call("GET", f"{API}/threads/{tid}/goal")
    check("目标没有被重建", goal2 and goal2["id"] == gid1)
    check("目标记住了第二轮", goal2 and t2["id"] in goal2.get("task_ids", []))

    status, t3 = call("POST", f"{API}/threads/{tid}/turns", {"title": "继续", "goal": "继续", "scenario": "dev"})
    check("「继续」不新建一轮", status in (200, 201) and t3["id"] in (task1, t2["id"]), f"status={status} id={t3.get('id')}")
    status, t4 = call("POST", f"{API}/threads/{tid}/turns", {"title": "暂停", "goal": "暂停", "scenario": "dev"})
    check("「暂停」作用于已有任务", status in (200, 201) and t4.get("id") in (task1, t2["id"]))

    # 3. §15 观测端点把 goal / task / conversation 对上。
    status, obs = call("GET", f"{API}/observability/goals")
    check("观测端点可用", status == 200, f"status={status}")
    if isinstance(obs, dict):
        observed = [g for g in obs.get("goals", []) if g.get("goal_id") == gid1]
        check("活跃目标列表包含本次目标", bool(observed))
        linked = [t for t in obs.get("background_tasks", []) if t.get("task_id") in (task1, t2["id"])]
        check("后台任务三条索引齐全",
              bool(linked) and linked[0]["goal_id"] == gid1 and linked[0]["conversation_id"] == tid,
              json.dumps(linked[:1], ensure_ascii=False)[:200])
        check("孤儿任务为 0（只数 v0.10.0 任务）", obs.get("orphans") == 0, str(obs.get("orphans")))
        check("worker 健康字段齐全", isinstance(obs.get("worker"), dict) and "healthy" in obs["worker"])

    # 4. 落库事实与事件链。
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    goal_row = con.execute("select status, task_ids from goals where id=?", (gid1,)).fetchone()
    check("goals 表有这一行", goal_row is not None, str(dict(goal_row) if goal_row else None))
    event_types = {
        row[0]
        for row in con.execute(
            "select type from agent_events where task_id in (?,?)", (task1, t2["id"])
        ).fetchall()
    }
    for wanted in ("message.observed", "goal.created", "goal.linked", "task.dispatched"):
        check(f"事件链含 {wanted}", wanted in event_types)
    con.close()

    # 5. 设置复位：冒烟不能把用户的模式留在 autonomous。
    status, _ = call("PUT", f"{API}/settings/agent", {"background": {"automation_mode": original_mode}})
    check("恢复原自动化模式", status == 200, f"status={status} -> {original_mode}")

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
