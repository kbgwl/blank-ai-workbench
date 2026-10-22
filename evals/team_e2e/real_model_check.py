"""真实模型下的团队链路点验。

`run.mjs` 那套门禁用的是脚本化 stub：它能证明链路形状对，但证明不了「一组真实的 Bot
在群里会真的动手改文件、高风险操作真的会挂起等人批准」。这两件事只有接上真模型才成立，
而真模型的输出不确定，所以这里的判定全部取「外层可见证据」：磁盘内容、工具卡片、
审批挂起后的成员变化、全文索引命中——不听模型自己说了什么。

用法：
    TEAM_REAL_PROVIDER_ID=<供应商 id> python evals/team_e2e/real_model_check.py
    TEAM_REAL_API=http://127.0.0.1:8061 TEAM_REAL_PROVIDER_ID=... python ...

必须连隔离实例。绝不指向开发库：这里会真的建项目、真的改文件、真的花真模型的钱。
"""

import json
import os
import time
import uuid
from pathlib import Path

import httpx

API = os.environ.get("TEAM_REAL_API", "http://127.0.0.1:8061") + "/api/v1"
PROVIDER_ID = os.environ.get("TEAM_REAL_PROVIDER_ID", "")
WORK = Path(os.environ.get("TEAM_REAL_WORK", "E:/tmp/realmodel/team"))
MARK = f"REALTEAM-{uuid.uuid4().hex[:8]}"
client = httpx.Client(base_url=API, timeout=60, trust_env=False)
checks: list[dict] = []


def check(name: str, ok, detail: str = "") -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(f"{'PASS' if ok else 'FAIL'} {name} :: {str(detail)[:280]}")
    return bool(ok)


def skip(name: str, reason: str) -> None:
    checks.append({"name": name, "ok": None, "detail": reason[:600]})
    print(f"SKIP {name} :: {reason[:280]}")


def post(path: str, body: dict, expected: int = 201) -> dict:
    resp = client.post(path, json=body)
    assert resp.status_code == expected, f"{path}: {resp.status_code} {resp.text}"
    return resp.json()


def patch(path: str, body: dict) -> dict:
    resp = client.patch(path, json=body)
    assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text}"
    return resp.json()


def state(group_id: str) -> dict:
    resp = client.get(f"/team/groups/{group_id}/state")
    assert resp.status_code == 200, f"state: {resp.status_code} {resp.text}"
    return resp.json()


def settle(group_id: str, timeout: float = 240, waiting_ok: bool = True) -> dict:
    terminal = ("completed", "failed", "cancelled", "waiting_approval") if waiting_ok else ("completed", "failed", "cancelled")
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = state(group_id)
        if (last.get("round") or {}).get("status") in terminal:
            return last
        time.sleep(1.0)
    raise AssertionError(f"轮次没有收口：{json.dumps(last, ensure_ascii=False)[:800]}")


def wait_for_pending(group_id: str, timeout: float = 30) -> dict:
    """等审批卡落库。

    轮次状态先翻到 waiting_approval、那条 bot 消息的 `waiting_approval` 后到，
    中间有窗口；这个读模型只返后到的那一份。实测踩过：一次空档就让点验少批一张卡，
    把「模型连挂三次高风险」判成「放行没生效」。
    """
    deadline = time.monotonic() + timeout
    snapshot = state(group_id)
    while not (snapshot.get("pending") or []) and time.monotonic() < deadline:
        time.sleep(1.0)
        snapshot = state(group_id)
    return snapshot


def events(task_id: str) -> list[dict]:
    resp = client.get(f"/tasks/{task_id}/events")
    assert resp.status_code == 200, f"events: {resp.status_code} {resp.text}"
    return resp.json()


def bot_speakers(snapshot: dict, name: str | None = None) -> list[dict]:
    rows = [row for row in snapshot["messages"] if row["role"] == "bot" and row.get("content")]
    return rows if name is None else [row for row in rows if row["bot_name"] == name]


def round_events(snapshot: dict) -> list[dict]:
    """把这一轮里所有 Bot 发言的事件并起来。

    真模型不按规定动作：让它"@ 工程 去改文件"，实测主理人自己把文件改了，
    工程只读了一遍就回"已符合要求"。只盯着成员那条 task 判"有没有真动手"，
    会把一次成功的交付判成失败。谁动的手不是这条判定的问题，动没动才是。
    """
    rows: list[dict] = []
    for row in bot_speakers(snapshot):
        rows += events(row["task_id"])
    return rows


def ensure_bot(name: str, role: str, provider_id: str, *, max_steps: int = 8) -> dict:
    """按名字取现成的 Bot，没有才建。

    这个点验要能反复跑：真模型跑一轮就要花真钱，第一次还多半会因为判据本身要修而重跑。
    `team_bots.name` 是 UNIQUE，直连 POST 第二次就撞约束，于是"重跑一次"变成"先清库"。
    """
    for row in client.get("/team/bots").json():
        if row["name"] == name:
            return row
    return post("/team/bots", {"name": name, "role": role, "provider_id": provider_id, "max_steps": max_steps})


def main() -> int:
    if not PROVIDER_ID:
        print("缺 TEAM_REAL_PROVIDER_ID：真实模型点验必须显式指定供应商，不给默认值。")
        return 2
    WORK.mkdir(parents=True, exist_ok=True)
    ws = WORK / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "clock.py"
    original = "print('hello')\n"
    target.write_text(original, encoding="utf-8")

    get_bootstrap = client.get("/team/bootstrap")
    assert get_bootstrap.status_code == 200, get_bootstrap.text
    project = post("/projects", {"name": f"real-team-{time.strftime('%H%M%S')}"})
    post(f"/projects/{project['id']}/workspaces", {"path": str(ws).replace("\\", "/")})
    worker = ensure_bot("工程", "实现与验证", PROVIDER_ID, max_steps=8)
    ensure_bot("评审", "复核结论", PROVIDER_ID)
    group = post(
        "/team/groups",
        {
            "project_id": project["id"],
            "name": "真实模型群",
            "topic": "把 clock.py 改成打印当前时间",
            "member_ids": [worker["id"]],
            "token_budget": 200000,
        },
    )["group"]

    # 装置自检：标记串必须先搜不到，否则「搜到了」这条判定根本不可能失败。
    before_hits = client.get("/team/search", params={"q": MARK}).json()["results"]
    if not check("R0 标记串在跑之前搜不到（装置自检）", not before_hits, f"{len(before_hits)} 条命中"):
        return 1

    post(f"/team/groups/{group['id']}/messages", {"content": (
        f"@工程 打开 clock.py，把它改成打印当前时间（用 datetime），改完在结论里"
        f"原样写出这个标记串 {MARK}，不要加引号。"
    )})
    first = settle(group["id"])
    check("R1 真实模型跑完一轮", first["round"]["status"] == "completed", first["round"]["status"])

    worker_rows = bot_speakers(first, "工程")
    admin_rows = bot_speakers(first, "小白管家")
    check("R2 主理人与成员各落成一条带 task_id 的发言", bool(admin_rows and worker_rows)
          and bool(admin_rows[0].get("task_id")) and bool(worker_rows[0].get("task_id")),
          [len(admin_rows), len(worker_rows)])
    if not worker_rows:
        return _finish(first)

    all_events = round_events(first)
    done = [e for e in all_events if e["type"] == "tool.completed" and (e.get("payload") or {}).get("ok")]
    tools = sorted({(e["payload"] or {}).get("tool", "") for e in done})
    check("R3 本轮真的调成了写文件工具", bool({"apply_patch", "write_file"} & set(tools)), tools)
    changed = [e for e in all_events if e["type"] == "file.changed"]
    now_text = target.read_text(encoding="utf-8")
    # 磁盘是唯一裁判。模型自报"改好了"不算，事件里没记但文件变了也不算丢——
    # 上一版只盯着被 @ 的那个成员的 task，实测主理人自己改了文件、成员只读了一遍，
    # 于是把一次成功的交付判成失败。
    check("R4 磁盘上的文件真的变了（不是模型自报）",
          now_text != original and "datetime" in now_text,
          {"content": now_text[:160], "file.changed 事件": len(changed)})

    hits = client.get("/team/search", params={"q": MARK}).json()["results"]
    check("R5 全文索引能命中成员的真实产出", bool(hits) and hits[0].get("group_id") == group["id"],
          (hits[0].get("snippet") if hits else "无命中"))

    post(f"/team/groups/{group['id']}/messages", {"content": "把评审拉进群，让它复核 clock.py 的改动"})
    # 真模型选哪条路不可预期：实测它先试 team_create_bot，撞上重名后（修好之前直接抛
    # IntegrityError 并把整轮钉死在 waiting_approval）才转去 team_add_member。
    # 所以放行要循环点，而不是只批一次；成员名单最终是否真的变了才是判据。
    waiting = settle(group["id"])
    pending = waiting.get("pending") or []
    check("R6 改成员的高风险工具挂起等审批", waiting["round"]["status"] == "waiting_approval" and bool(pending),
          {"round": waiting["round"]["status"], "pending": [c.get("approvals") for c in pending][:1]})
    names: list[str] = []
    approved = 0
    parked: list[str] = []
    # 放行要循环点，且每次放行后都可能再挂起：真模型走的是 team_create_bot（重名被
    # 拦住，返回可执行提示）→ team_add_member → team_assign_task，三个高风险调用、
    # 三次挂起。上一版按「批一次就该收口」写，把第二次合法的挂起判成故障，4 分钟白等。
    for _ in range(6):
        if waiting["round"]["status"] != "waiting_approval":
            break
        waiting = wait_for_pending(group["id"])
        # 空档里轮次也可能自己收口（例如最后一次放行后没人再挂起）。那时不该记
        # 「点不到卡」——那是把「已经做完了」判成故障。
        if waiting["round"]["status"] != "waiting_approval":
            break
        pending = waiting.get("pending") or []
        if not pending:
            check("R7.x 挂起后确实有可点的审批卡", False, "等 30 秒仍读不到 pending")
            break
        item = pending[0]
        tools = [str(a.get("tool") or "") for a in item.get("approvals") or []]
        parked += tools
        resp = client.post(
            f"/team/messages/{item['message_id']}/approval", json={"approve": True, "scope": "once"}
        )
        approved += 1
        # 409 有两种，得分开报：卡过期是点验自己拖过了 TTL，不是链路坏；
        # 混成一句「受理失败」就把装置的问题算到产品头上。
        if resp.status_code == 409 and "approval_expired" in resp.text:
            check(f"R7.{approved} 审批接口受理", False, f"{tools} 卡已过期（审批 TTL 5 分钟），本轮只能重跑")
            break
        check(f"R7.{approved} 审批接口受理", resp.status_code == 202, f"{tools} -> {resp.status_code}")
        waiting = settle(group["id"])
    converged = waiting["round"]["status"] in ("completed", "failed", "cancelled")
    check("R7.x 连续放行后轮次确实收口", converged,
          {"末态": waiting["round"]["status"], "放行次数": approved, "挂起过的工具": parked})
    names = [m["name"] for m in waiting["members"]]
    check("R8 放行后成员名单真的变了", any("评审" in n for n in names), {"成员": names, "放行次数": approved})

    curated = []
    for row in bot_speakers(first):
        curated += [e for e in events(row["task_id"]) if e["type"] == "context.curated"]
    if curated:
        check("R9 上下文管家挑选进了提示", True, (curated[0].get("payload") or {}).get("summary", "")[:200])
    else:
        skip("R9 上下文管家挑选进了提示",
             "本轮上下文没到压缩水位（这是真模型的正常行为，不是缺陷）；挑选逻辑由 stub 门禁 S11–S13 覆盖")

    return _finish(first)


def _finish(snapshot: dict) -> int:
    failed = [c for c in checks if c["ok"] is False]
    skipped = [c for c in checks if c["ok"] is None]
    passed = [c for c in checks if c["ok"] is True]
    (WORK / "real-model-check.json").write_text(
        json.dumps({"passed": len(passed), "failed": [c["name"] for c in failed],
                    "skipped": [c["name"] for c in skipped], "checks": checks,
                    "round_status": snapshot.get("round", {}).get("status")},
                   ensure_ascii=False, indent=2),
        encoding="utf8",
    )
    print(f"\n判定 {len(passed)}/{len([c for c in checks if c['ok'] is not None])} 通过，跳过 {len(skipped)} 项")
    if failed:
        print("未通过：" + "、".join(c["name"] for c in failed))
    return 1 if failed else (3 if skipped else 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as crash:  # noqa: BLE001 - 中途炸了也要留一份能看的结论
        # 上一版就是栽在这里：settle() 超时抛 AssertionError，整份报告随之消失，
        # 只剩一坨 traceback——排查时等于什么都没记下来。
        check("R10 点验脚本自己跑完了", False, repr(crash)[:400])
        raise SystemExit(_finish({}))
