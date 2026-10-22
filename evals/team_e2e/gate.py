"""第二阶段能力门禁：主理人团队管理工具、上下文管家、全文检索的真实运行核对。

看的是外层证据（群状态、工具卡片、审批挂起、system prompt 落盘、搜索结果），
不看模型自述。由 run.mjs 注入环境：TEAM_E2E_API、TEAM_E2E_WORK。
"""

import glob
import json
import os
import sys
import time
from pathlib import Path

import httpx

WORK = Path(os.environ["TEAM_E2E_WORK"])
BASE = os.environ["TEAM_E2E_API"] + "/api/v1"
client = httpx.Client(base_url=BASE, timeout=30, trust_env=False)
checks: list[dict] = []


def check(name: str, ok, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"{'PASS' if ok else 'FAIL'} {name} :: {str(detail)[:300]}")


def state(group_id: str) -> dict:
    resp = client.get(f"/team/groups/{group_id}/state")
    resp.raise_for_status()
    return resp.json()


def settle(group_id: str, timeout: float = 120, waiting_ok: bool = True) -> dict:
    """waiting_ok=False 用于放行之后：waiting_approval 是放行前的旧状态，不能当终点。"""
    terminal = ("completed", "failed", "cancelled", "waiting_approval") if waiting_ok else ("completed", "failed", "cancelled")
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = state(group_id)
        if (last.get("round") or {}).get("status") in terminal:
            return last
        time.sleep(0.5)
    raise AssertionError(f"timeout: {json.dumps(last, ensure_ascii=False)[:800]}")


def events(task_id: str) -> list[dict]:
    return client.get(f"/tasks/{task_id}/events").json()


def scenario(**flags) -> None:
    (WORK / "scenario.json").write_text(json.dumps(flags), encoding="utf-8")


def task_of(snapshot: dict, message_id: int) -> str:
    return next(row["task_id"] for row in snapshot["messages"] if row["id"] == message_id)


def main() -> int:
    seed = json.loads((WORK / "seed.json").read_text(encoding="utf-8"))
    group_id = seed["group_id"]
    # 上一轮点验可能留下进行中的轮次：不停掉就没法改成员，S0 会以 409 收场
    client.post(f"/team/groups/{group_id}/stop")
    for _ in range(60):
        if (state(group_id).get("round") or {}).get("status") in ("cancelled", "completed", "failed"):
            break
        time.sleep(0.5)
    # 上一轮点验把前端加进过群，这里先回到「只有后端」的起点，断言才有意义
    client.patch(f"/team/groups/{group_id}", json={"member_ids": [seed["backend_bot_id"]]})
    before = state(group_id)
    check("S0 起点成员数为 2", len(before["members"]) == 2, [m["name"] for m in before["members"]])

    scenario(add_member=True)
    client.post(f"/team/groups/{group_id}/messages", json={"content": "把前端拉进群，让他复查 token 解析那个结论"})
    waiting = settle(group_id)
    check(
        "S1 高风险工具先挂起等审批",
        waiting["round"]["status"] == "waiting_approval" and len(waiting["pending"]) == 1,
        {"round": waiting["round"]["status"], "pending": len(waiting["pending"])},
    )
    if waiting["round"]["status"] != "waiting_approval" or not waiting["pending"]:
        return report()
    # 成员改动落在被审批那条消息的任务里，不是最后一条主理人消息
    message_id = waiting["pending"][0]["message_id"]
    admin_task = task_of(waiting, message_id)

    approve = client.post(f"/team/messages/{message_id}/approval", json={"approve": True, "scope": "once"})
    check("S2 审批接口接受单次放行", approve.status_code == 202, approve.text[:200])
    done = settle(group_id, waiting_ok=False)
    check("S3 放行后本轮跑完", done["round"]["status"] == "completed", done["round"]["status"])

    names = [member["name"] for member in done["members"]]
    check("S4 前端真的被主理人加进群", "前端" in names and len(names) == 3, names)

    ev = events(admin_task)
    curated = [item for item in ev if item["type"] == "context.curated"]
    check("S5 运行时留下 context.curated 审计事件", bool(curated), [item["type"] for item in ev][:12])
    add_done = [
        item
        for item in ev
        if item["type"] == "tool.completed" and (item.get("payload") or {}).get("tool") == "team_add_member"
    ]
    check(
        "S6 工具卡片记录 team_add_member 成功",
        bool(add_done) and add_done[0]["payload"]["ok"] is True,
        add_done[0]["payload"].get("summary") if add_done else "no tool event",
    )

    dump_files = sorted(glob.glob(str(WORK / "system" / "admin-*.txt")), key=lambda p: Path(p).stat().st_mtime)
    hit = [Path(p).read_text(encoding="utf-8") for p in dump_files if "上下文管家为本次任务挑选" in Path(p).read_text(encoding="utf-8")]
    check("S7 管家挑的资料真的进了模型 system prompt", bool(hit), f"{len(hit)} 份 system 含管家段落")
    if hit:
        text = hit[-1]
        block = text[text.index("上下文管家为本次任务挑选") :][:1500]
        check("S8 system 段落带来源标注与记忆命中", "来源" in block and "token 解析" in block, block.replace("\n", " ")[:300])

    scenario(add_missing=True)
    client.post(f"/team/groups/{group_id}/messages", json={"content": "把 不存在的成员 拉进群"})
    waiting2 = settle(group_id)
    neg_task = ""
    if waiting2["round"]["status"] == "waiting_approval":
        neg_task = task_of(waiting2, waiting2["pending"][0]["message_id"])
        client.post(
            f"/team/messages/{waiting2['pending'][0]['message_id']}/approval",
            json={"approve": True, "scope": "once"},
        )
        waiting2 = settle(group_id, waiting_ok=False)
    check("S9 负例轮次结束", waiting2["round"]["status"] in ("completed", "failed"), waiting2["round"]["status"])
    names2 = [member["name"] for member in waiting2["members"]]
    check("S10 不存在的 Bot 没有混进成员表", "不存在的成员" not in names2 and len(names2) == 3, names2)
    if neg_task:
        ev2 = events(neg_task)
        bad = [
            item
            for item in ev2
            if item["type"] == "tool.completed" and (item.get("payload") or {}).get("tool") == "team_add_member"
        ]
        check(
            "S11 负例的工具卡片是失败而不是成功",
            bool(bad) and bad[0]["payload"]["ok"] is False,
            bad[0]["payload"].get("summary") if bad else [item["type"] for item in ev2][:10],
        )

    hits = client.get("/team/search", params={"q": "把前端拉进群"}).json()
    check("S12 新一轮发言立刻进了检索索引", bool(hits["results"]), hits["results"][0]["snippet"] if hits["results"] else "无")

    # 主理人自己建 Bot（create 会一并入群）、再派活：两步都是 high 风险，逐个审批后必须真的落库
    for stale in client.get("/team/bots").json():
        if stale["name"] == "测试":
            client.delete(f"/team/bots/{stale['id']}")
    scenario(create_assign=True, done=[], provider_id=seed["provider_id"])
    created = client.post(
        f"/team/groups/{group_id}/messages",
        json={"content": "组建新成员：建一个测试 Bot 拉进群，让它补登录回归测试"},
    ).json()
    start_id = created["accepted"]["message_id"]
    approved: set[str] = set()
    deadline = time.monotonic() + 150
    snap = created
    while time.monotonic() < deadline:
        snap = state(group_id)
        status = (snap.get("round") or {}).get("status")
        if status in ("completed", "failed", "cancelled"):
            break
        if status == "waiting_approval" and snap["pending"]:
            card = snap["pending"][0]
            signature = f"{card['message_id']}:{card['approvals'][0]['tool']}"
            if signature not in approved:
                approved.add(signature)
                client.post(
                    f"/team/messages/{card['message_id']}/approval",
                    json={"approve": True, "scope": "once"},
                )
        time.sleep(0.8)
    check("S13 建 Bot 链跑完", snap["round"]["status"] == "completed", snap["round"]["status"])

    names3 = [member["name"] for member in snap["members"]]
    check("S14 新建的「测试」进了成员表", "测试" in names3, names3)
    tool_ok: dict[str, bool] = {}
    for tid in {row["task_id"] for row in snap["messages"] if row["id"] > start_id and row["task_id"]}:
        for item in events(tid):
            payload = item.get("payload") or {}
            if item["type"] == "tool.completed" and str(payload.get("tool", "")).startswith("team_"):
                tool_ok[str(payload["tool"])] = payload["ok"] is True
    check(
        "S15 建 Bot 与派活都真的执行成功",
        all(tool_ok.get(name) for name in ("team_create_bot", "team_assign_task")),
        tool_ok,
    )
    check(
        "S16 派活留下系统分派提示",
        any("补一条登录回归测试" in (row["content"] or "") for row in snap["messages"] if row["role"] == "system"),
        [row["content"] for row in snap["messages"] if row["role"] == "system"][-3:],
    )
    scenario(create_assign=False, add_member=False, add_missing=False)
    return report()


def report() -> int:
    failed = [item for item in checks if not item["ok"]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("未通过：" + ", ".join(item["name"] for item in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"ASSERT: {exc}", file=sys.stderr)
        raise
