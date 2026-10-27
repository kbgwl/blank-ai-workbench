"""#116/#117/#118/#119 的真模型三格点验装置（只打隔离实例，默认 :8061）。

三格各自的职责：

    主格   —— 带钩子配置跑两轮 + 一次回退，判 #116 句柄链、#117 回退点、
              #118 钩子副本、#119 唤醒投递。
    编码格 —— 唤醒正文逐字不许变形（#119 修的那个 GBK 缺陷就红在这里过）。
    对照格 —— 同一套判据打在**没配 worktree_create 钩子**的实例上，
              期望「mode=hook」那条**必须红**；它不红说明主判据什么都没测到，
              整次点验作废（rc 2）。

用法（凭据一律在外面，仓库不落一个字节）：

    export BLANK_REALMODEL_PROVIDER=<provider id>
    export BLANK_REALMODEL_HOME=E:/tmp/realmodel
    export BLANK_REALMODEL_BASE=http://127.0.0.1:8061
    services/api/.venv/Scripts/python.exe -X utf8 evals/realmodel/async_hooks_v119.py

已经跑过的那几轮可以按任务 id 复用事件重算，不再花模型的钱：

    RM_TASK_MAIN=... RM_TASK_ENC=... RM_TASK_CTL=... 同上

复用模式下回退格不重算（那一轮已经退过，重算会把「已回退」读成缺陷），
读数里会明写哪几条没判。判据本体在 `judgments.py`，可离线喂事件自检。
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

MARKER = "KBGAI-V119-REALMODEL-MARKER-4C7D"
GOAL_1 = (
    "请按顺序真的调用三组工具，不要合并、不要跳过：\n"
    f"1) 用 read_file 读取 note.txt，复述其中的标记串（形如 {MARKER}）；\n"
    "2) 用 spawn_agent 派一个子代理，参数 background=true、isolate=true、label=v119，"
    "子任务目标写「在隔离副本里用 read_file 读 FROM-HOOK.txt，回报文件内容与它是否存在」；"
    "拿到返回的 agent_id 后立刻继续，不要等待；\n"
    "3) 用 task_output 取那个 agent_id 的结论（block=true，timeout=120），"
    "回报它的 status 与 summary。\n"
    "不要写文件，不要执行任何命令。最后用一句话汇报三步的结果。"
)
GOAL_2 = "第二轮：不要调用任何工具，只回答一句「第二轮已就位」。"
GOAL_CTL = (
    "用 spawn_agent 派一个子代理（background=false、isolate=true、label=v119ctl），"
    "子任务目标写「用 read_file 读 note.txt 并复述其中的标记串」。"
    "拿到子代理结论后直接汇报，不要自己读文件、不要执行命令。"
)


def hook_text() -> str:
    return (Path(__file__).resolve().parent / "fixtures" / "wake-text.txt").read_text(
        encoding="utf-8").rstrip("\r\n")


def seed_workspace(cfg: lib.Config) -> None:
    """隔离实例的工作区由装置自己铺，不靠人手抄 note.txt。"""
    ws = cfg.workspace
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "note.txt").write_text(
        f"这是真模型点验的标记文件。\n标记串：{MARKER}\n", encoding="utf-8")


# 复用任务 id 的变量名只在这里出现一次。
# 上一版有两套：姿态闸门按格子名拼（RM_TASK_CONTROL），各格自己读（RM_TASK_CTL），
# 于是「复用对照格、只花主格+编码格的钱」在闸门眼里是「要新跑对照格」，
# 直接 rc 2 拦死——拦的方向是对的（没白花钱），但装置自己把合法跑法判成了失效。
TASK_ENV = {"main": "RM_TASK_MAIN", "encoding": "RM_TASK_ENC", "control": "RM_TASK_CTL"}


def reused(cell: str) -> str:
    return os.environ.get(TASK_ENV[cell], "").strip()


def reuse_map() -> dict[str, str]:
    return {cell: reused(cell) for cell in TASK_ENV}


def run_main(client: lib.Client, cfg: lib.Config, rec: lib.Recorder, notes: list[str]) -> None:
    reuse = reused("main")
    if reuse:
        task1 = reuse
        notes.append("回退格（117 五条）本轮没判：复用的是已经回退过的那一轮")
    else:
        stamp = time.strftime("%m%d%H%M%S")
        project = client.post("/projects", {"name": f"v119-realmodel-{stamp}",
                                            "description": "点验 #116-#119"})
        client.post(f"/projects/{project['id']}/workspaces", {"path": str(cfg.workspace), "label": "v119"})
        thread = client.post(f"/projects/{project['id']}/threads", {"title": f"v119 真模型点验 {stamp}"})
        tid = thread["id"]
        turn1 = client.post(f"/threads/{tid}/turns", {"title": "工具链点验", "goal": GOAL_1,
                                                      "provider_id": cfg.provider_id, "max_steps": 12})
        task1 = turn1["id"]
        client.post(f"/tasks/{task1}/start", {"provider_id": cfg.provider_id})
        print(f"turn1 status={client.wait_terminal(task1, 600)} task1={task1} thread={tid}")

        turn2 = client.post(f"/threads/{tid}/turns", {"title": "第二轮", "goal": GOAL_2,
                                                      "provider_id": cfg.provider_id, "max_steps": 4})
        task2 = turn2["id"]
        client.post(f"/tasks/{task2}/start", {"provider_id": cfg.provider_id})
        print(f"turn2 status={client.wait_terminal(task2, 300)} task2={task2}")

        events1 = client.events(task1)
        evidence = json.dumps(client.get(f"/tasks/{task1}/evidence"), ensure_ascii=False)
        J.judge_wakeup(rec, events1)
        J.judge_worktree_hook(rec, events1, hook_log_lines(cfg))
        J.judge_subagent(rec, events1, evidence)

        points = client.get(f"/threads/{tid}/rewind-points")
        rewind = client.post(f"/threads/{tid}/rewind", {"turn": 1})
        after = client.get(f"/threads/{tid}")
        J.judge_rewind(rec, points, rewind, after.get("turns") or [], task1,
                       [e for e in client.events(task1)])
        return

    events1 = client.events(task1)
    evidence = json.dumps(client.get(f"/tasks/{task1}/evidence"), ensure_ascii=False)
    J.judge_wakeup(rec, events1)
    J.judge_worktree_hook(rec, events1, hook_log_lines(cfg))
    J.judge_subagent(rec, events1, evidence)


def hook_log_lines(cfg: lib.Config) -> list[str]:
    return cfg.hook_log.read_text(encoding="utf-8").splitlines() if cfg.hook_log.exists() else []


def run_encoding(client: lib.Client, cfg: lib.Config, rec: lib.Recorder) -> None:
    reuse = reused("encoding")
    if not reuse:
        stamp = time.strftime("%m%d%H%M%S")
        project = client.post("/projects", {"name": f"v119-enc-{stamp}"})
        client.post(f"/projects/{project['id']}/workspaces", {"path": str(cfg.workspace), "label": "enc"})
        thread = client.post(f"/projects/{project['id']}/threads", {"title": f"编码点验 {stamp}"})
        turn = client.post(f"/threads/{thread['id']}/turns", {"title": "唤醒正文不许变形",
                                                              "goal": "用 read_file 读取 note.txt，"
                                                              "复述其中的标记串即可，不要做别的事。",
                                                              "provider_id": cfg.provider_id, "max_steps": 4})
        reuse = turn["id"]
        client.post(f"/tasks/{reuse}/start", {"provider_id": cfg.provider_id})
        client.wait_terminal(reuse, 300)
    J.judge_encoding(rec, client.events(reuse), hook_text())
    print(f"encoding task={reuse}")


def run_control(client: lib.Client, cfg: lib.Config, rec: lib.Recorder) -> bool:
    reuse = reused("control")
    if not reuse:
        stamp = time.strftime("%m%d%H%M%S")
        project = client.post("/projects", {"name": f"v119-ctl-{stamp}"})
        client.post(f"/projects/{project['id']}/workspaces", {"path": str(cfg.workspace), "label": "ctl"})
        thread = client.post(f"/projects/{project['id']}/threads", {"title": f"对照格 {stamp}"})
        turn = client.post(f"/threads/{thread['id']}/turns", {"title": "对照：无钩子的隔离副本",
                                                              "goal": GOAL_CTL,
                                                              "provider_id": cfg.provider_id, "max_steps": 8})
        reuse = turn["id"]
        client.post(f"/tasks/{reuse}/start", {"provider_id": cfg.provider_id})
        client.wait_terminal(reuse, 420)
    control_red = J.judge_control(rec, client.events(reuse))
    print(f"control task={reuse} 该红的那条红了={control_red}")
    return control_red


def main() -> int:
    cfg = lib.config_from_env()
    cells = [c.strip() for c in os.environ.get("RM_CELL", "main,encoding,control").split(",") if c.strip()]
    rec = lib.Recorder()
    notes: list[str] = []

    reuse = reuse_map()
    # RM_CELL 写错格子名不能静默少跑一格：那一格的判据根本不会进读数表。
    unknown = [c for c in cells if c not in TASK_ENV]
    skip = ""
    abort = f"RM_CELL 里有不认识的格子 {unknown}（可选：{sorted(TASK_ENV)}）" if unknown else ""
    if not abort and not cfg.provider_id and not all(reuse[cell] for cell in cells):
        skip = "没给 BLANK_REALMODEL_PROVIDER，也没有可复用的任务 id"
    client = lib.Client(cfg)
    if not skip and not abort and not client.reachable():
        abort = f"{cfg.base} 连不上（实例没起？端口不对？）"
    if not skip and not abort:
        # 姿态没钉住就别花钱：#120 第一次真跑就是带着坏配置跑完三格，
        # 11 条钩子判据全红，红的是姿态，不是产品。
        posture_abort = lib.premise_error(cells=cells, reuse=reuse, posture=cfg.posture())
        if posture_abort:
            abort = posture_abort

    if skip or abort:
        rec.report("读数（没跑判据）")
        rc = lib.decide_exit_code(skip=skip, abort=abort)
        lib.print_gate(skip=skip, abort=abort, rc=rc)
        return rc

    print(f"[姿态] {cfg.posture_file.name} -> {cfg.posture()}")
    seed_workspace(cfg)
    if "main" in cells:
        run_main(client, cfg, rec, notes)
    if "encoding" in cells:
        run_encoding(client, cfg, rec)
    control_red = None
    if "control" in cells:
        control_red = run_control(client, cfg, rec)

    fails = [name for name in rec.fails if name != J.CONTROL_RED_NAME]
    rc = lib.decide_exit_code(fails=fails, control_red=control_red)
    rec.report("三格读数")
    for note in notes:
        print(f"[没判] {note}")
    if control_red is False:
        print("[ABORT] 对照格没红：主判据在任何一次运行里都成立，等于没测到东西")
    lib.print_gate(rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
