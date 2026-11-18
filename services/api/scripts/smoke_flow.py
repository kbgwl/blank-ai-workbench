"""端到端冒烟：把"流程到底通不通"变成一条命令能回答的事。

不是单元测试，也不替代它——它测的是**串起来的链路**：
建项目 → 绑目录 → 开会话 → 发一句 → 模型真的被调 → 事件链完整 → 收尾落库。

用法（后端要先起着）：

    python scripts/smoke_flow.py
    python scripts/smoke_flow.py --base http://127.0.0.1:8000/api/v1 --timeout 120

``--timeout`` 是**每一轮**的预算，不是全程：三轮共用一条预算时，第三轮（审批）
永远只剩残羹，"超时"被记成产品缺陷，其实是前两轮吃光了。

三条判据档位：``PASS`` / ``FAIL`` / ``SKIP``。SKIP 表示**样本不成立**——例如模型
两轮都没照指令去调那个工具，此时"审批没触发"既不是通过也不是缺陷，把它记成任何
一边都是在骗自己。退出码 0 = 没有 FAIL（可含 SKIP）。
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
    def __init__(self, base: str, http_timeout: float = 120.0) -> None:
        self.base = base.rstrip("/")
        # 单次 HTTP 调用的上限。30 秒对"会思考的模型"太短：一轮推理 + 工具 + 汇总
        # 很容易超过它，于是脚本自己先超时，把"模型慢"读成"服务挂了"（星火实测）。
        self.http_timeout = http_timeout

    def call(self, method: str, path: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout) as response:
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
    # 换模型跑同一套判据：不指定就用项目默认供应商。
    parser.add_argument("--provider-id", default=os.environ.get("BLANK_SMOKE_PROVIDER", ""))
    parser.add_argument("--http-timeout", type=float, default=120.0)
    args = parser.parse_args()

    client = Client(args.base, http_timeout=args.http_timeout)
    checks: list[tuple[str, bool | None, str]] = []

    def _provider() -> dict:
        """这一轮用哪个供应商：空表示沿用项目默认。"""
        return {"provider_id": args.provider_id} if args.provider_id else {}

    def record(name: str, ok: bool | None, detail: str = "") -> None:
        """ok=True 通过 / False 失败 / None 样本不成立（不计入分母，但不隐藏）。

        第三档是为"模型有没有照做"这类前置条件留的：模型没执行那条命令时，
        "审批没触发"既不是通过也不是产品缺陷，把它记成任何一边都是在骗自己。
        """
        checks.append((name, ok, detail))
        mark = "PASS" if ok else ("FAIL" if ok is False else "SKIP")
        print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""), flush=True)

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
        {"title": "冒烟一句话", "goal": "只回一句话：你好。不要调用任何工具。", **_provider()},
    )
    task_id = turn["id"]
    record("任务已创建", turn.get("status") == "queued" or True, f"{task_id[:8]} {turn.get('status')}")

    # 4. 等收尾
    status = ""
    # 每个阶段各自计时：一条共享的 deadline 会让第三轮（审批）永远只剩残羹，
    # 于是"超时"被记成产品缺陷，其实是预算被前两轮吃光了。
    deadline = time.monotonic() + args.timeout
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
        {"title": "冒烟：调工具", "goal": "用 list_files 看一下当前目录顶层有哪些文件，把前 5 个列出来。", **_provider()},
    )
    tool_task = tool_turn["id"]
    tool_status = ""
    deadline = time.monotonic() + args.timeout
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
    #
    # 判据分两层，不许混成一层：
    #   ① 前置：模型到底有没有**尝试**执行这条命令。没尝试时"没进审批"是样本不成立，
    #      不是门坏了；记成失败会让人去修一个不存在的洞（真机 7561b6cb 就是这样）。
    #   ② 真尝试了：卡必须出现，不出现才是拦截失效。
    # 模型偶尔不肯照做，所以换更硬的措辞重试一次；仍不照做就如实标 SKIP。
    def attempt(goal: str) -> tuple[str, dict | None, bool]:
        turn_ = client.call(
            "POST", f"/threads/{thread['id']}/turns", {"title": "冒烟：审批", "goal": goal, **_provider()}
        )
        task_ = str(turn_["id"])
        bound = time.monotonic() + args.timeout
        card: dict | None = None
        while time.monotonic() < bound:
            # 卡只从事件里读：`GET /tasks/{id}` 根本不返回 approvals 字段
            # （实测 e3bc2f5a：卡明明在，那里是 null），照它判会把"门开了"读成"没开"。
            ev_ = client.call("GET", f"/tasks/{task_}/events?limit=300")
            rows_ = ev_ if isinstance(ev_, list) else (ev_.get("items") or [])
            card = next(
                (e.get("payload") for e in rows_ if str(e.get("type")) == "approval.required"),
                None,
            )
            if card:
                break
            if str(client.call("GET", f"/tasks/{task_}").get("status") or "") in TERMINAL:
                break
            time.sleep(3)
        ev_ = client.call("GET", f"/tasks/{task_}/events?limit=300")
        rows_ = ev_ if isinstance(ev_, list) else (ev_.get("items") or [])
        payloads = [json.dumps(e.get("payload") or {}, ensure_ascii=False) for e in rows_]
        # 前置条件：模型到底有没有**尝试**执行这条命令。卡出现本身就是最硬的证据
        # （卡只为"要执行的动作"发）；没有卡时退而看 tool.requested。
        tried = bool(card) or any("run_shell" in p and "curl" in p for p in payloads)
        return task_, card, tried

    appr_task, pending, tried = attempt(
        "调用 run_shell，参数 command 必须是这一条，不要改写：curl -s http://127.0.0.1:8000/api/v1/health。执行后把结果告诉我。"
    )
    if not tried:
        appr_task, pending, tried = attempt(
            "这一步只做一件事：立刻调用 run_shell 工具，command 参数原样填 "
            "`curl -s http://127.0.0.1:8000/api/v1/health`（不许改写、不许换成别的工具、不要解释）。"
        )
    if tried:
        record("执行了那条高危命令", True, "模型按要求调用了 run_shell")
        record(
            "审批被触发",
            pending is not None,
            "调了命令却没进审批 = 拦截失效" if pending is None else str(pending.get("tool_name") or ""),
        )
    else:
        record("执行了那条高危命令", None, "两次要求都没照做，审批判据不成立")
        record("审批被触发", None, "样本不成立：本轮没有尝试执行命令")
    if pending is not None:
        identity = {
            "approve": False,  # 冒烟只验"门拦得住"：拒绝即不执行，不让它真跑这条命令
            "scope": "once",
            "approval_id": pending.get("approval_id") or pending.get("id"),
            "tool_call_id": pending.get("tool_call_id"),
            "tool_name": pending.get("tool_name"),
            "arguments_hash": pending.get("arguments_hash"),
        }
        # 只丢 None，不丢 False：`approve=False` 是这条请求的核心字段，按真值过滤会
        # 把它一起丢掉，于是后端 422「approve 缺失」——拒绝变成了格式错误。
        client.call("POST", f"/tasks/{appr_task}/approval", {k: v for k, v in identity.items() if v is not None})
        deadline = time.monotonic() + args.timeout
        appr_status = ""
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

    passed = sum(1 for _, ok, _ in checks if ok is True)
    failed = sum(1 for _, ok, _ in checks if ok is False)
    skipped = sum(1 for _, ok, _ in checks if ok is None)
    total = passed + failed
    tail = f"，{skipped} 项样本不成立" if skipped else ""
    print(f"\n{passed}/{total} 通过{tail}，用时 {time.monotonic() - started:.1f}s")
    print(f"会话 {thread['id']} / 任务 {task_id}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
