"""#123 提交钩子三道决定的真模型点验（只打隔离实例，默认主格 :8061、对照格 :8065）。

四格真跑在主格那台**带提交钩子**的实例上，一格跑在**没钩子**的对照实例上：

    抑制格 —— 钩子要求 `suppressOriginalPrompt` 并给了替代正文。判「模型读的是替代
              正文、读数里不含原文口令、账面 goal 仍是原文、上下文管家的候选没被原文
              关键词带出来（侧门）」。
    拦下格 —— 钩子退出码 2。判「一次模型调用都没发起」（无 `model.usage`、无
              `task.started`、零步）与「失败原因指向钩子」。
    补上下文格 —— 只给 `additionalContext`。判「原文与补充同时进模型」。
    无关格 —— goal 里没有任何锚。判「钩子跑过并留审计，但不产生任何改写事件」。
    对照格 —— 抑制格那条 goal 打在没钩子的实例上，期望该红的三条**必须红**
              （`judgments.CONTROL_123_RED`）；它不红 = 主判据什么都没测到 = 整次作废（rc 2）。

锚、正文、口令与侧门文件名都只在 `fixtures/prompt-submit-v123.json` 里写一次，
夹具钩子（`hooks/prompt_submit.py`）与判据读同一份，两处不许各抄。

用法（凭据一律在外面，仓库不落一个字节）：

    export BLANK_REALMODEL_PROVIDER=<provider id>
    RM_BASE_MAIN=http://127.0.0.1:8061 RM_HOME_MAIN=E:/tmp/realmodel \
    RM_BASE_CTL=http://127.0.0.1:8065 RM_HOME_CTL=E:/tmp/realmodel-ctl \
    services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/prompt_submit_v123.py

主格那台实例必须是 `launch-instance.sh --with-hooks --with-submit-hook` 起起来的：
留痕文件里 `submit_hook` 不是 true 就直接 rc 2，不拿真模型的钱去试姿态（#120 栽过一次）。

跑过的那几轮可以按任务 id 复用事件重算，不再花钱：

    RM123_TASK_SUPPRESS=... RM123_TASK_BLOCKED=... RM123_TASK_INJECTED=... \
    RM123_TASK_PLAIN=... RM123_TASK_CTL=... 同上

读数（每格的事件 + 任务行 + 用了哪台实例）会整份写进
`<home>/prompt-submit-v123-<stamp>.json`，文档里的每条实测都指向那一份。
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

SPEC_FILE = Path(__file__).resolve().parent / "fixtures" / "prompt-submit-v123.json"
# 四格各一个复用变量：一格一份任务 id，不像 #119 那样一格一份（这里主格本身就有四份）。
POSTURE_ENV = {
    "suppress": ("RM123_TASK_SUPPRESS", J.judge_submit_suppress),
    "blocked": ("RM123_TASK_BLOCKED", J.judge_submit_blocked),
    "injected": ("RM123_TASK_INJECTED", J.judge_submit_injected),
    "plain": ("RM123_TASK_PLAIN", J.judge_submit_plain),
}
GOAL_KEY = {"suppress": "suppress_goal", "blocked": "block_goal",
            "injected": "inject_goal", "plain": "plain_goal"}
# 拦下格不进模型，等不到那么久；其余是一句话的回答。
BUDGET = {"blocked": 90.0, "suppress": 240.0, "injected": 240.0, "plain": 240.0, "control": 240.0}


def spec() -> dict:
    return json.loads(SPEC_FILE.read_text(encoding="utf-8"))


def workspace(cfg: lib.Config, spec_row: dict) -> Path:
    """工作区由装置自己铺：侧门判据靠那枚与原文口令同名的文件存在。"""
    ws = cfg.home / "ws-v123"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / spec_row["side_door_file"]).write_text("侧门探针：文件名与原文口令同词。\n", encoding="utf-8")
    (ws / "note.txt").write_text("无关文件，只为让候选列表非空。\n", encoding="utf-8")
    return ws


def run_task(client: lib.Client, cfg: lib.Config, ws: Path, title: str, goal: str,
             budget: float) -> str:
    stamp = time.strftime("%m%d%H%M%S")
    project = client.post("/projects", {"name": f"v123-{title}-{stamp}"})
    client.post(f"/projects/{project['id']}/workspaces", {"path": str(ws), "label": "v123"})
    thread = client.post(f"/projects/{project['id']}/threads", {"title": f"v123 {title} {stamp}"})
    turn = client.post(f"/threads/{thread['id']}/turns", {"title": title, "goal": goal,
                                                          "provider_id": cfg.provider_id, "max_steps": 4})
    task_id = turn["id"]
    client.post(f"/tasks/{task_id}/start", {"provider_id": cfg.provider_id})
    status = client.wait_terminal(task_id, budget)
    print(f"[跑完] {title} status={status} task={task_id} @ {cfg.base}")
    return task_id


def judge_cell(cell: str, rec: lib.Recorder, spec_row: dict, rows: dict) -> bool | None:
    """按格取数并判；返回对照格「该红的红没红」，主格返回 None。

    对照格用**自己那份判定表**：它本来就要红好几条（没钩子时那些读数都不成立），
    把它的红混进主格的红集合里，等于让装置在姿态正确时也报判据红——主格的红
    只能来自主格。对照格只交一件事：`CONTROL_123_RED` 那三条到底红没红。
    """
    if cell == "main":
        for name, (_env, judge) in POSTURE_ENV.items():
            task = rows[name]
            judge(rec, task["events"], task["task"], spec_row)
        return None
    task = rows["control"]
    return J.judge_submit_control(rec, task["events"], task["task"], spec_row)


def submit_posture_abort(posture: dict | None, base: str) -> str:
    """主格那台的姿态必须自证「提交钩子真的挂上了」，否则一分钱都别花。

    只认启动器写下的那一个 `true`：留痕文件不在（实例是手起的）、值读成字符串
    "true"、缺键，全都不算——#120 那次带着坏配置跑完三格、钱花了什么都没测到。
    """
    got = (posture or {}).get("submit_hook")
    if got is not True:
        where = "留痕文件不在（实例是手起的？姿态未知）" if posture is None else f"读到 submit_hook={got!r}"
        return (f"主格 {base} 的{where}，提交钩子没挂上：四格判据必红，那是白花钱。"
                "用 evals/realmodel/launch-instance.sh --with-hooks --with-submit-hook 重起这台实例")
    return ""


def main() -> int:
    rec = lib.Recorder()
    rows: dict[str, dict] = {}
    spec_row = spec()
    reuse = {name: os.environ.get(env, "").strip() for name, (env, _) in POSTURE_ENV.items()}
    reuse["control"] = os.environ.get("RM123_TASK_CTL", "").strip()

    provider = lib.config_from_env().provider_id
    if not provider and not all(reuse.values()):
        skip = "没给 BLANK_REALMODEL_PROVIDER，且五格没有全部给可复用的任务 id"
        rec.report("读数（没跑判据）")
        lib.print_gate(skip=skip, rc=lib.decide_exit_code(skip=skip))
        return lib.decide_exit_code(skip=skip)

    main_cfg, ctl_cfg = lib.cell_config("main"), lib.cell_config("control")
    clients = {"main": lib.Client(main_cfg), "control": lib.Client(ctl_cfg)}
    offline = [name for name, cli in clients.items() if not cli.reachable()]
    if offline:
        rec.report("读数（没跑判据）")
        rc = lib.decide_exit_code(abort="连不上：" + "、".join(clients[name].cfg.base for name in offline))
        lib.print_gate(abort=f"实例连不上 {offline}", rc=rc)
        return rc

    # 两道姿态闸：先让 lib 按格核对 hooks / no-hooks，再单独钉「主格必须挂着提交钩子」。
    abort = lib.premise_error(cells=["main", "control"],
                              reuse={"main": reuse["suppress"], "control": reuse["control"]},
                              targets={"main": main_cfg.target(), "control": ctl_cfg.target()})
    main_posture = main_cfg.posture()
    if not abort:
        abort = submit_posture_abort(main_posture, main_cfg.base)
    if abort:
        rec.report("读数（没跑判据）")
        rc = lib.decide_exit_code(abort=abort)
        lib.print_gate(abort=abort, rc=rc)
        return rc

    print(f"[姿态] 主格 {main_cfg.base} -> {main_cfg.posture_file.name}: {main_posture}")
    print(f"[姿态] 对照格 {ctl_cfg.base} -> {ctl_cfg.posture_file.name}: {ctl_cfg.posture()}")
    main_ws, ctl_ws = workspace(main_cfg, spec_row), workspace(ctl_cfg, spec_row)
    titles = {"suppress": "抑制格", "blocked": "拦下格", "injected": "补上下文格", "plain": "无关格"}
    for name in POSTURE_ENV:
        goal = spec_row[GOAL_KEY[name]]
        if reuse[name]:
            task_id = reuse[name]
            print(f"[来源] {titles[name]} 复用 task={task_id}（旧读数，不是此刻的实物）")
        else:
            task_id = run_task(clients["main"], main_cfg, main_ws, titles[name], goal, BUDGET[name])
        rows[name] = {"id": task_id, "events": clients["main"].events(task_id),
                      "task": clients["main"].get(f"/tasks/{task_id}"), "base": main_cfg.base}
    if reuse["control"]:
        ctl_id = reuse["control"]
        print(f"[来源] 对照格 复用 task={ctl_id}（旧读数，不是此刻的实物）")
    else:
        ctl_id = run_task(clients["control"], ctl_cfg, ctl_ws, "对照格",
                          spec_row["suppress_goal"], BUDGET["control"])
    rows["control"] = {"id": ctl_id, "events": clients["control"].events(ctl_id),
                       "task": clients["control"].get(f"/tasks/{ctl_id}"), "base": ctl_cfg.base}

    control_rec = lib.Recorder()
    control_red = judge_cell("control", control_rec, spec_row, rows)
    judge_cell("main", rec, spec_row, rows)

    rc = lib.decide_exit_code(fails=rec.fails, control_red=control_red)
    rec.report("主格四格读数")
    control_rec.report("对照格读数（没钩子那台；下面这几条按设计就该红）")
    print(f"[对照] 该红的三条 {json.dumps(J.CONTROL_123_RED, ensure_ascii=False)} 红了={control_red}")
    if control_red is False:
        print("[ABORT] 对照格没红：主判据在任何一次运行里都成立，等于没测到东西")
    lib.print_gate(rc=rc)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifact = main_cfg.home / f"prompt-submit-v123-{stamp}.json"
    artifact.write_text(json.dumps({
        "main_posture": main_posture,
        "control_posture": ctl_cfg.posture(),
        "control_red": control_red,
        "rc": rc,
        "red": rec.fails,
        "reads": rec.reads,
        "control_reads": control_rec.reads,
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[工件] {artifact}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
