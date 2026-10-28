"""#123 团队入口的真模型点验：成员任务的 goal 也过提交钩子（只打带钩子的那台实例）。

产品侧把提交钩子接在 `AgentRuntime._submit_prompt` 上，而群聊的成员任务是**第二条入口**
（`services/team.py` 直接把用户发言当成员任务的 goal）。离线 mock 用例钉住了接线，
这一支补的是真模型上的实物：群发一条带抑制锚的发言 → 成员任务的 goal 被钩子改写 →
机器人发言里出现替代正文的口令、没有原文口令、账面 goal 仍是原文。

用法：

    export BLANK_REALMODEL_PROVIDER=<provider id>
    RM_BASE_MAIN=http://127.0.0.1:8061 RM_HOME_MAIN=E:/tmp/realmodel \\
    services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/team_submit_v123.py

实例必须是 `launch-instance.sh --with-hooks --with-submit-hook` 起的：留痕里
`submit_hook` 不是 `true` 就直接 rc 2，不拿真模型的钱去试姿态。

跑过的这一轮可以按 id 复用，不再花钱：

    RM123_TEAM_PROJECT=<project id> RM123_TEAM_GROUP=<group id> ... 同上

工件写进 `<home>/team-submit-v123-<stamp>.json`（群状态、各任务的事件、判定表）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judgments as J  # noqa: E402
import lib  # noqa: E402
from prompt_submit_v123 import submit_posture_abort  # noqa: E402

SPEC_FILE = Path(__file__).resolve().parent / "fixtures" / "prompt-submit-v123.json"
ROUND_TIMEOUT = 420.0
TERMINAL = ("completed", "failed", "cancelled", "waiting_approval")


def spec() -> dict:
    return json.loads(SPEC_FILE.read_text(encoding="utf-8"))


def control_red_exact(rec: lib.Recorder) -> bool:
    """对照格的红集合要**精确等于**登记的那三条：多了少了都说明这份对照读数不可引用。"""
    return set(rec.fails) == set(J.CONTROL_123_TEAM_RED)


def control_posture_abort(posture: dict | None, base: str) -> str:
    """对照格那台必须留痕 `no-hooks`；留痕缺失或坏 JSON 都不许当成「大概没钩子」。"""
    got = (posture or {}).get("posture")
    if got == "no-hooks":
        return ""
    return f"对照格要没钩子的实例，但 {base} 的留痕读到 {got!r}"


def wait_round(client: lib.Client, group_id: str, budget: float) -> dict:
    """等这一轮落地；返回最后一帧群状态（超时也照样返回，让判据读实物而不是猜）。"""
    deadline = time.time() + budget
    state: dict = {}
    while time.time() < deadline:
        state = client.get(f"/team/groups/{group_id}/state")
        status = str((state.get("round") or {}).get("status") or "")
        if status in TERMINAL:
            return state
        time.sleep(3)
    return state


def run_team(client: lib.Client, cfg: lib.Config, spec_row: dict) -> tuple[str, str, dict]:
    stamp = time.strftime("%m%d%H%M%S")
    ws = cfg.home / "ws-v123-team"
    ws.mkdir(parents=True, exist_ok=True)
    project = client.post("/projects", {"name": f"v123-team-{stamp}"})
    client.post(f"/projects/{project['id']}/workspaces", {"path": str(ws), "label": "v123-team"})
    client.post(
        "/team/bots",
        {"name": f"成员{stamp[-4:]}", "role": "侦察", "provider_id": cfg.provider_id},
    )
    bots = client.get("/team/bots")
    rows = bots.get("bots") if isinstance(bots, dict) else bots
    member = next(item for item in rows if str(item.get("name") or "") == f"成员{stamp[-4:]}")
    group = client.post(
        "/team/groups",
        {
            "project_id": project["id"],
            "name": f"提交钩子群 {stamp}",
            "topic": "验证提交钩子作用在成员任务上",
            "member_ids": [member["id"]],
            "max_speakers": 3,
            "token_budget": 60_000,
        },
    )["group"]
    client.post(f"/team/groups/{group['id']}/messages", {"content": spec_row["suppress_goal"]})
    state = wait_round(client, group["id"], ROUND_TIMEOUT)
    print(f"[跑完] 群 {group['id']} 轮 {(state.get('round') or {}).get('status')}")
    return project["id"], group["id"], state


def collect(client: lib.Client, project_id: str) -> list[dict]:
    tasks = client.get(f"/tasks?project_id={project_id}&limit=100")
    return [
        {"id": task["id"], "task": task, "events": client.events(task["id"])}
        for task in tasks
    ]


def main() -> int:
    rec = lib.Recorder()
    spec_row = spec()
    cfg = lib.cell_config("main")
    client = lib.Client(cfg)
    control = "--control" in sys.argv[1:]
    if not client.reachable():
        skip = f"实例连不上：{cfg.base}"
        lib.print_gate(abort=skip, rc=lib.decide_exit_code(abort=skip))
        return lib.decide_exit_code(abort=skip)
    if control:
        # 对照格要的是**没有钩子**的那台：这几条判据在那台上必须红，否则主格绿了也说明不了什么。
        abort = control_posture_abort(cfg.posture(), cfg.base)
        if abort:
            rec.report("读数（没跑判据）")
            lib.print_gate(abort=abort, rc=lib.decide_exit_code(abort=abort))
            return lib.decide_exit_code(abort=abort)
    else:
        abort = submit_posture_abort(cfg.posture(), cfg.base)
        if abort:
            rec.report("读数（没跑判据）")
            lib.print_gate(abort=abort, rc=lib.decide_exit_code(abort=abort))
            return lib.decide_exit_code(abort=abort)

    project_id = os.environ.get("RM123_TEAM_PROJECT", "").strip()
    group_id = os.environ.get("RM123_TEAM_GROUP", "").strip()
    if project_id and group_id:
        print(f"[来源] 复用 project={project_id} group={group_id}（旧读数，不是此刻的实物）")
        state = client.get(f"/team/groups/{group_id}/state")
    else:
        if not cfg.provider_id:
            skip = "没给 BLANK_REALMODEL_PROVIDER，也没有可复用的 RM123_TEAM_PROJECT/GROUP"
            lib.print_gate(skip=skip, rc=lib.decide_exit_code(skip=skip))
            return lib.decide_exit_code(skip=skip)
        project_id, group_id, state = run_team(client, cfg, spec_row)

    tasks = collect(client, project_id)
    messages = list(state.get("messages") or [])
    J.judge_team_submit(rec, tasks, messages, spec_row)
    control_red = None
    if control:
        control_red = control_red_exact(rec)
        rc = lib.decide_exit_code(control_red=control_red)
    else:
        rc = lib.decide_exit_code(fails=rec.fails)
    rec.report("团队入口提交钩子读数（对照格：下面这几条按设计就该红）" if control
               else "团队入口提交钩子读数")
    if control:
        print(f"[对照] 该红的三条 {json.dumps(J.CONTROL_123_TEAM_RED, ensure_ascii=False)} 红了={control_red}")
        print(f"        实测红集合={json.dumps(sorted(rec.fails), ensure_ascii=False)}")
    lib.print_gate(rc=rc)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifact = cfg.home / f"team-submit-v123-{'control-' if control else ''}{stamp}.json"
    artifact.write_text(
        json.dumps(
            {
                "posture": cfg.posture(),
                "control": control,
                "control_red": control_red,
                "project": project_id,
                "group": group_id,
                "state": state,
                "tasks": tasks,
                "rc": rc,
                "red": rec.fails,
                "reads": rec.reads,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[工件] {artifact}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
