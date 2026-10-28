"""#123 团队入口的真模型点验：成员任务的 goal 也过提交钩子（主格带钩子，对照格不带）。

产品侧把提交钩子接在 `AgentRuntime._submit_prompt` 上，而群聊的成员任务是**第二条入口**
（`services/team.py` 直接把用户发言当成员任务的 goal）。离线 mock 用例钉住了接线，
这一支补的是真模型上的实物：群发一条带锚的发言 → 每条成员任务都留下钩子事件 →
机器人的发言与账面 goal 各自对得上。

四格（`--mode`，默认 `suppress`）：

    suppress —— 钩子要求 `suppressOriginalPrompt` 并给替代正文。判「每条成员任务都被
                改写、账面 goal 逐字不动、机器人答替代正文口令且不含原文口令」。
    ask      —— 钩子回 `decision:"ask"`。提交这一步没有确认界面，成员任务与收尾任务
                都该被拦下：一次模型调用都不发起，群里念的是拒绝原因。
    multi    —— 两位成员同一轮被点名，收尾轮是主理人的第二次提交。结构判据与钩子
                无关，用来保证上面两格的判据真的有多份样本可判。
    budget   —— 预算耗尽中途截断：系统消息、任务数、轮状态与用量账四项。

用法：

    export BLANK_REALMODEL_PROVIDER=<provider id>
    RM_BASE_MAIN=http://127.0.0.1:8061 RM_HOME_MAIN=E:/tmp/realmodel \\
    services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/team_submit_v123.py --mode ask

对照格（同一份 goal 打在**没钩子**的那台实例上，该红的那几条必须红）：

    RM_BASE_CONTROL=http://127.0.0.1:8065 RM_HOME_CONTROL=E:/tmp/realmodel-ctl \\
    ... team_submit_v123.py --mode ask --control

主格那台必须是 `launch-instance.sh --with-hooks --with-submit-hook` 起的；对照格那台
留痕里必须是 `no-hooks`。姿态不对就直接 rc 2，不拿真模型的钱去试。

跑过的这一轮可以按 id 复用，不再花钱：

    RM123_TEAM_PROJECT=<project id> RM123_TEAM_GROUP=<group id> ...（suppress 格）
    RM123_TEAM_ASK_PROJECT=... RM123_TEAM_ASK_GROUP=... ...（其余格按模式名）
    RM123_TEAM_ASK_CTL_PROJECT=... RM123_TEAM_ASK_CTL_GROUP=...（对照格）

工件写进 `<home>/team-submit-v123-<mode>[-control]-<stamp>.json`。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judgments as J  # noqa: E402
import lib  # noqa: E402
from prompt_submit_v123 import submit_posture_abort  # noqa: E402

SPEC_FILE = Path(__file__).resolve().parent / "fixtures" / "prompt-submit-v123.json"
ROUND_TIMEOUT = 480.0
TERMINAL = ("completed", "failed", "cancelled", "waiting_approval")
MODES = ("suppress", "ask", "multi", "budget")
# 预算格要小到中途就停：主理人开场一条发言的用量就在 4k 上下，5k 是群接口的下限，
# 于是第二、三位发言之间必然截断——收尾轮轮不到，账也停在那里。
MODE_BUDGET = {"suppress": 60_000, "ask": 60_000, "multi": 60_000, "budget": 5_000}
MODE_MEMBERS = {"suppress": 1, "ask": 1, "multi": 2, "budget": 2}
MODE_SPEAKERS = {"suppress": 3, "ask": 3, "multi": 5, "budget": 4}
# 四格共用的 goal：只有 ask 换锚，其余都走抑制锚——钩子对每条任务都有确定的作用。
MODE_GOAL = {"suppress": "suppress_goal", "ask": "ask_goal",
             "multi": "suppress_goal", "budget": "suppress_goal"}
CONTROL_FOR = {"ask": J.CONTROL_123_TEAM_ASK_RED}
CONTROL_DEFAULT = J.CONTROL_123_TEAM_RED


def spec() -> dict:
    return json.loads(SPEC_FILE.read_text(encoding="utf-8"))


def parse_args(argv: list[str]) -> tuple[str, bool]:
    """`--mode <x>` / `--mode=<x>` / `--control`；其余一律 rc 2，不猜。"""
    mode, control = "suppress", False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--control":
            control = True
        elif item.startswith("--mode="):
            mode = item.split("=", 1)[1]
        elif item == "--mode":
            index += 1
            if index >= len(argv):
                raise SystemExit("[ABORT] --mode 后面要跟模式名：" + " / ".join(MODES))
            mode = argv[index]
        else:
            raise SystemExit(f"[ABORT] 不认识的参数 {item}（可用：--mode <{'|'.join(MODES)}> --control）")
        index += 1
    if mode not in MODES:
        raise SystemExit(f"[ABORT] 未知模式 {mode}（可用：{' / '.join(MODES)}）")
    return mode, control


def control_red_exact(rec: lib.Recorder, mode: str = "suppress") -> bool:
    """对照格的红集合要**精确等于**登记的那一组：多了少了都说明这份对照读数不可引用。"""
    return set(rec.fails) == set(CONTROL_FOR.get(mode, CONTROL_DEFAULT))


def control_posture_abort(posture: dict | None, base: str) -> str:
    """对照格那台必须留痕 `no-hooks`；留痕缺失或坏 JSON 都不许当成「大概没钩子」。"""
    got = (posture or {}).get("posture")
    if got == "no-hooks":
        return ""
    return f"对照格要没钩子的实例，但 {base} 的留痕读到 {got!r}"


def compose_goal(base: str, names: list[str]) -> str:
    """点名让成员进队列：不点名时只有主理人一条任务，多成员/收尾根本没发生。"""
    if not names:
        return base
    return base + "\n" + " ".join(f"@{name}" for name in names) + " 请各自用一句话确认。"


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


def run_team(client: lib.Client, cfg: lib.Config, spec_row: dict, mode: str) -> tuple[str, str, dict, int]:
    stamp = time.strftime("%m%d%H%M%S")
    ws = cfg.home / "ws-v123-team"
    ws.mkdir(parents=True, exist_ok=True)
    project = client.post("/projects", {"name": f"v123-team-{mode}-{stamp}"})
    client.post(f"/projects/{project['id']}/workspaces", {"path": str(ws), "label": "v123-team"})
    # 一台实例最多 8 个 Bot，几格跑下来会满：先复用已有的非主理人成员，
    # 不够才新建。名字在整座实例里唯一，只拼秒数会撞 409。
    rows = client.get("/team/bots")
    existing = [row for row in (rows if isinstance(rows, list) else rows.get("bots") or [])
                if not row.get("is_admin") and row.get("enabled", True)]
    names = [str(row["name"]) for row in existing[:MODE_MEMBERS[mode]]]
    member_ids = [str(row["id"]) for row in existing[:MODE_MEMBERS[mode]]]
    while len(names) < MODE_MEMBERS[mode]:
        name = f"成员{stamp[-4:]}{uuid.uuid4().hex[:4]}"
        created = client.post(
            "/team/bots", {"name": name, "role": "侦察", "provider_id": cfg.provider_id}
        )
        names.append(name)
        member_ids.append(created["id"])
    group = client.post(
        "/team/groups",
        {
            "project_id": project["id"],
            "name": f"提交钩子群 {mode} {stamp}",
            "topic": "验证提交钩子作用在成员任务上",
            "member_ids": member_ids,
            "max_speakers": MODE_SPEAKERS[mode],
            "token_budget": MODE_BUDGET[mode],
        },
    )["group"]
    goal = compose_goal(spec_row[MODE_GOAL[mode]], names)
    client.post(f"/team/groups/{group['id']}/messages", {"content": goal})
    state = wait_round(client, group["id"], ROUND_TIMEOUT)
    print(f"[跑完] 群 {group['id']} 轮 {(state.get('round') or {}).get('status')} "
          f"tasks? members={MODE_MEMBERS[mode]} goal_chars={len(goal)}")
    # 名册 = 主理人开场 + 被点名成员 + 主理人收尾；预算格用它判断「没有多派任务」。
    roster = 1 + len(names) + 1
    return project["id"], group["id"], state, roster


def collect(client: lib.Client, project_id: str) -> list[dict]:
    tasks = client.get(f"/tasks?project_id={project_id}&limit=100")
    return [
        {"id": task["id"], "task": task, "events": client.events(task["id"])}
        for task in tasks
    ]


def reuse_ids(mode: str, control: bool) -> tuple[str, str]:
    tag = mode.upper()
    if control:
        return (f"RM123_TEAM_{tag}_CTL_PROJECT", f"RM123_TEAM_{tag}_CTL_GROUP")
    if mode == "suppress":
        return ("RM123_TEAM_PROJECT", "RM123_TEAM_GROUP")
    return (f"RM123_TEAM_{tag}_PROJECT", f"RM123_TEAM_{tag}_GROUP")


def judge_mode(rec: lib.Recorder, mode: str, tasks: list[dict], messages: list[dict],
               round_row: dict, spec_row: dict, roster: int) -> None:
    """按格分派：ask 只判拒的那一组；其余判提交钩子，再叠加该模式的结构判据。"""
    if mode == "ask":
        J.judge_team_ask(rec, tasks, messages, spec_row)
        return
    J.judge_team_submit(rec, tasks, messages, spec_row)
    if mode == "multi":
        J.judge_team_multi(rec, tasks, messages, round_row)
    elif mode == "budget":
        J.judge_team_budget(rec, tasks, messages, round_row, roster, MODE_BUDGET[mode])


def main() -> int:
    mode, control = parse_args(sys.argv[1:])
    rec = lib.Recorder()
    spec_row = spec()
    cfg = lib.cell_config("control" if control else "main")
    client = lib.Client(cfg)
    if not client.reachable():
        skip = f"实例连不上：{cfg.base}"
        lib.print_gate(abort=skip, rc=lib.decide_exit_code(abort=skip))
        return lib.decide_exit_code(abort=skip)
    if control:
        # 对照格要的是**没有钩子**的那台：这几条判据在那台上必须红，否则主格绿了也说明不了什么。
        abort = control_posture_abort(cfg.posture(), cfg.base)
    else:
        abort = submit_posture_abort(cfg.posture(), cfg.base)
    if abort:
        rec.report("读数（没跑判据）")
        lib.print_gate(abort=abort, rc=lib.decide_exit_code(abort=abort))
        return lib.decide_exit_code(abort=abort)

    project_env, group_env = reuse_ids(mode, control)
    project_id = os.environ.get(project_env, "").strip()
    group_id = os.environ.get(group_env, "").strip()
    roster = 0
    if project_id and group_id:
        print(f"[来源] 复用 project={project_id} group={group_id}（旧读数，不是此刻的实物）")
        state = client.get(f"/team/groups/{group_id}/state")
    else:
        if not cfg.provider_id:
            skip = f"没给 BLANK_REALMODEL_PROVIDER，也没有可复用的 {project_env}/{group_env}"
            lib.print_gate(skip=skip, rc=lib.decide_exit_code(skip=skip))
            return lib.decide_exit_code(skip=skip)
        project_id, group_id, state, roster = run_team(client, cfg, spec_row, mode)
    if not roster:
        # 复用路径没有真跑的名册读数：按模式登记的人数算（预算格靠它判「没有多派」）。
        roster = 1 + MODE_MEMBERS[mode] + 1

    tasks = collect(client, project_id)
    messages = list(state.get("messages") or [])
    round_row = state.get("round") or {}
    judge_mode(rec, mode, tasks, messages, round_row, spec_row, roster)
    control_red = None
    if control:
        control_red = control_red_exact(rec, mode)
        rc = lib.decide_exit_code(control_red=control_red)
    else:
        rc = lib.decide_exit_code(fails=rec.fails)
    rec.report(f"团队入口提交钩子读数（mode={mode}，对照格：下面这几条按设计就该红）" if control
               else f"团队入口提交钩子读数（mode={mode}）")
    if control:
        expected = CONTROL_FOR.get(mode, CONTROL_DEFAULT)
        print(f"[对照] 该红的 {len(expected)} 条 {json.dumps(list(expected), ensure_ascii=False)} "
              f"红了={control_red}")
        print(f"        实测红集合={json.dumps(sorted(rec.fails), ensure_ascii=False)}")
    lib.print_gate(rc=rc)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifact = cfg.home / (
        f"team-submit-v123-{mode}{'-control' if control else ''}-{stamp}.json")
    artifact.write_text(
        json.dumps(
            {
                "posture": cfg.posture(),
                "mode": mode,
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
