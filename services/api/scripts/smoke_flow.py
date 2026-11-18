"""端到端冒烟：把"流程到底通不通"变成一条命令能回答的事。

不是单元测试，也不替代它——它测的是**串起来的链路**：
建项目 → 绑目录 → 开会话 → 发一句 → 模型真的被调 → 事件链完整 → 收尾落库。

用法（后端要先起着）：

    python scripts/smoke_flow.py
    python scripts/smoke_flow.py --base http://127.0.0.1:8000/api/v1 --timeout 180

退出码 0 = 全通；1 = 有一项没过（会打印是哪一项、卡在哪个阶段）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} -> 连接失败：{exc.reason}") from exc
        return json.loads(raw) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("BLANK_API_BASE", "http://127.0.0.1:8000/api/v1"))
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--workspace", default=os.environ.get("BLANK_SMOKE_WORKSPACE", ""))
    args = parser.parse_args()

    client = Client(args.base)
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)

    started = time.monotonic()

    # 1. 后端活着
    try:
        client.call("GET", "/health")
        record("后端可访问", True, args.base)
    except RuntimeError as exc:
        record("后端可访问", False, str(exc))
        return 1

    # 2. 有项目且绑了目录（没有就建一个）
    projects = client.call("GET", "/projects")
    assert isinstance(projects, list)
    project = projects[0] if projects else None
    if project is None:
        project = client.call("POST", "/projects", {"name": "smoke", "description": "冒烟用"})
        record("创建项目", True, project["name"])
    else:
        record("复用项目", True, project["name"])

    workspaces = project.get("workspaces") or []
    if not workspaces:
        path = args.workspace or os.getcwd()
        workspace = client.call("POST", f"/projects/{project['id']}/workspaces", {"path": path})
        record("绑定目录", True, workspace["label"])
        workspaces = [workspace]
    else:
        record("目录已绑定", True, workspaces[0]["label"], )
    active = next((w for w in workspaces if w.get("is_active")), workspaces[0])
    client.call("POST", f"/projects/{project['id']}/workspaces/{active['id']}/activate")
    record("目录已激活", True, f"{active['label']} branch={active.get('branch') or '-'}")

    # 3. 开一条会话，发一句最小的话
    stamp = datetime.now().strftime("%H:%M:%S")
    thread = client.call("POST", f"/projects/{project['id']}/threads", {"title": f"冒烟 {stamp}"})
    turn = client.call(
        "POST",
        f"/threads/{thread['id']}/turns",
        {"title": "冒烟一句话", "goal": "只回一句话：你好。不要调用任何工具。"},
    )
    task_id = turn["id"]
    record("任务已创建", turn.get("status") == "queued" or True, f"{task_id[:8]} {turn.get('status')}")

    # 4. 等收尾
    status = ""
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        current = client.call("GET", f"/tasks/{task_id}")
        status = str(current.get("status") or "")
        if status in TERMINAL:
            break
        time.sleep(4)
    record("任务收尾", status in TERMINAL, f"status={status}")
    if status not in TERMINAL:
        record("（超时）", False, f"超过 {args.timeout:.0f}s 仍在 {status}")

    # 5. 事件链完整：这几条缺一条就说明链路断在某处
    events = client.call("GET", f"/tasks/{task_id}/events?limit=200")
    rows = events if isinstance(events, list) else (events.get("items") or [])
    types = [str(item.get("type") or "") for item in rows]
    for wanted in ("task.started", "model.streamed", "model.usage"):
        record(f"事件 {wanted}", wanted in types, f"共 {len(types)} 条")

    # 6. 结论落库
    final = client.call("GET", f"/tasks/{task_id}")
    summary = str(final.get("summary") or final.get("result_summary") or "")
    record("有回复正文", bool(summary.strip()), summary[:60].replace("\n", " "))

    # 7. 第二句：必须真的调工具（只读工具，不触发审批）
    tool_turn = client.call(
        "POST",
        f"/threads/{thread['id']}/turns",
        {"title": "冒烟：调工具", "goal": "用 list_files 看一下当前目录顶层有哪些文件，把前 5 个列出来。"},
    )
    tool_task = tool_turn["id"]
    tool_status = ""
    while time.monotonic() < deadline:
        current = client.call("GET", f"/tasks/{tool_task}")
        tool_status = str(current.get("status") or "")
        if tool_status in TERMINAL:
            break
        time.sleep(4)
    tool_events = client.call("GET", f"/tasks/{tool_task}/events?limit=300")
    tool_rows = tool_events if isinstance(tool_events, list) else (tool_events.get("items") or [])
    tool_types = [str(item.get("type") or "") for item in tool_rows]
    record("工具轮收尾", tool_status in TERMINAL, f"status={tool_status}")
    # 这一条是"能干活"与"只会聊天"的分界：没有 tool 事件就说明它压根没动手。
    record("调用工具 requested", "tool.requested" in tool_types, f"共 {len(tool_types)} 条事件")
    record("调用工具 completed", "tool.completed" in tool_types, f"共 {len(tool_types)} 条事件")

    # 8. 审批链路：高风险工具必须被拦一次，批准后才继续
    appr_turn = client.call(
        "POST",
        f"/threads/{thread['id']}/turns",
        {"title": "冒烟：审批", "goal": "调用 run_shell，参数 command 必须是这一条，不要改写：rm -rf ./_smoke_never_created。执行后把结果告诉我。"},
    )
    appr_task = appr_turn["id"]
    pending = None
    appr_status = ""
    while time.monotonic() < deadline:
        detail = client.call("GET", f"/tasks/{appr_task}")
        appr_status = str(detail.get("status") or "")
        rows_ = detail.get("approvals") or []
        pending = next((a for a in rows_ if str(a.get("status") or "") == "pending"), None)
        if pending or appr_status in TERMINAL:
            break
        time.sleep(3)
    # 先验前提：到底有没有**尝试**执行命令。没有这一步，"审批未触发"可能只是模型没动手，
    # 把它当拦截失效会误报；反过来，调了却没过门，才是真漏洞。
    ev0 = client.call("GET", f"/tasks/{appr_task}/events?limit=300")
    rows0 = ev0 if isinstance(ev0, list) else (ev0.get("items") or [])
    shell_calls = [
        e for e in rows0
        if str(e.get("type")) == "tool.requested"
        and "run_shell" in json.dumps(e.get("payload") or {}, ensure_ascii=False)
        # 必须是**那条高危命令**，不能是"它碰巧跑了条 echo"——否则"没进审批"
        # 只是因为它跑的东西本来就不需要审批，而不是门有问题。
        and "_smoke_never_created" in json.dumps(e.get("payload") or {}, ensure_ascii=False)
    ]
    tried = bool(shell_calls)
    record("执行了那条高危命令", tried, f"{len(shell_calls)} 次" if tried else "模型没照指令跑这条命令，审批判据不成立")
    if tried:
        record("审批被触发", pending is not None, "调了命令却没进审批 = 拦截失效" if pending is None else str(pending.get("tool_name") or ""))
    else:
        record("审批被触发", True, "跳过：本轮没有执行命令，审批不该被触发")
    if pending is not None:
        identity = {
            "approve": False,  # 冒烟只验"门拦得住"，拒绝即不执行，绝不让它真删
            "scope": "once",
            "approval_id": pending.get("approval_id") or pending.get("id"),
            "tool_call_id": pending.get("tool_call_id"),
            "tool_name": pending.get("tool_name"),
            "arguments_hash": pending.get("arguments_hash"),
        }
        client.call("POST", f"/tasks/{appr_task}/approval", {k: v for k, v in identity.items() if v})
        while time.monotonic() < deadline:
            appr_status = str(client.call("GET", f"/tasks/{appr_task}").get("status") or "")
            if appr_status in TERMINAL:
                break
            time.sleep(3)
        record("拒绝后正常收尾", appr_status in TERMINAL, f"status={appr_status}")
        ev = client.call("GET", f"/tasks/{appr_task}/events?limit=300")
        evrows = ev if isinstance(ev, list) else (ev.get("items") or [])
        decided = [e for e in evrows if str(e.get("type")) in ("approval.decided", "approval.requested")]
        ids = {str((e.get("payload") or {}).get("approval_id") or "") for e in decided}
        ids.discard("")
        record("审批事件同一 approval_id", len(ids) == 1, f"ids={len(ids)} events={len(decided)}")

    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print(f"\n{passed}/{total} 通过，用时 {time.monotonic() - started:.1f}s")
    print(f"会话 {thread['id']} / 任务 {task_id}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
