"""#123 提交钩子点验装置的离线自检：装置本身必须能失败，且不花真模型的钱。

这批把「提交那一刻的三道决定」的真模型点验做成仓库里可复现的装置
（`evals/realmodel/prompt_submit_v123.py` + `judgments.judge_submit_*`）。价值仍按
#120 立下的三条逐条钉：

1. 判据吃的是**真实物**（`fixtures/prompt-submit-v123-green.json`，:8061 带提交钩子
   那一轮与 :8065 无钩子那一轮原样取回的事件与任务行）；
2. **每条判据都能失败**：按名字造损伤，红集合必须与期望逐字相等——期望来自实测；
3. 对照格（没钩子那台）该红的三条**必须红**，把主格的读数与「没有钩子时本来就会
   这样」区分开。

另外钉装置自己的两处形状：夹具钩子对四种 goal 真跑出四样决定（过产品解析器），
以及姿态闸门只认启动器写下的那一个 `true`。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEVICE = REPO / "evals/realmodel"
FIXTURES = DEVICE / "fixtures"
sys.path.insert(0, str(DEVICE))
sys.path.insert(0, str(REPO / "services/api/src"))

import judgments as J  # noqa: E402
import lib  # noqa: E402
import prompt_submit_v123 as D  # noqa: E402
from blank_api.services.hooks import HookRunner, parse_hook_output  # noqa: E402

SPEC = json.loads((FIXTURES / "prompt-submit-v123.json").read_text(encoding="utf-8"))
# 解释器用"正在跑测试的这个"：写死 .venv/Scripts/python.exe 只在 Windows 上存在，
# Linux runner 上那个目录叫 .venv/bin/python——2026-10-29 CI 首次真跑就撞在这里。
_VENV_PY = REPO / "services/api/.venv/Scripts/python.exe"
VENV_PY = _VENV_PY if _VENV_PY.exists() else Path(sys.executable)
# 实物：主格四格（带提交钩子）+ 对照格（没钩子）。原件见 E:/tmp/realmodel/
# prompt-submit-v123-20261027-*.json，两份由同一次真跑写出。
GREEN = json.loads((FIXTURES / "prompt-submit-v123-green.json").read_text(encoding="utf-8"))

JUDGE = {name: judge for name, (_env, judge) in D.POSTURE_ENV.items()}


def row(name: str) -> dict:
    return GREEN["rows"][name]


def events_of(name: str) -> list[dict]:
    return copy.deepcopy(row(name)["events"])


def task_of(name: str) -> dict:
    return copy.deepcopy(row(name)["task"])


def fails_for(name: str, events: list[dict], task: dict) -> list[str]:
    rec = lib.Recorder()
    JUDGE[name](rec, events, task, SPEC)
    return list(rec.fails)


def drop_event(events: list[dict], type_name: str) -> list[dict]:
    return [e for e in events if e["type"] != type_name]


def first_payload(events: list[dict], type_name: str) -> dict:
    for e in events:
        if e["type"] == type_name:
            return e["payload"]
    raise AssertionError(f"实物里没有 {type_name}")


def mutate(events: list[dict], type_name: str, key: str, value) -> list[dict]:
    out = copy.deepcopy(events)
    first_payload(out, type_name)[key] = value
    return out


def with_event(events: list[dict], type_name: str, payload: dict) -> list[dict]:
    """加一条实物里没有的事件——侧门那一格的损伤形态就是「管家又拿到了原文」。"""
    return copy.deepcopy(events) + [{"type": type_name, "payload": payload}]


# ---------------------------------------------------------------- 1 实物绿基线


@pytest.mark.parametrize("name", ["suppress", "blocked", "injected", "plain"])
def test_主格四格的实物读数在判据上全绿(name: str) -> None:
    assert fails_for(name, events_of(name), task_of(name)) == [], name


def test_对照格的三条该红必须真红() -> None:
    rec = lib.Recorder()
    assert J.judge_submit_control(rec, events_of("control"), task_of("control"), SPEC) is True
    assert [name for name in rec.fails if name in J.CONTROL_123_RED] == list(J.CONTROL_123_RED)


def test_同一套对照判据打在带钩子的实物上不该全红() -> None:
    """装置自检：对照格那条路只有在「没钩子」的实物上才红，否则它就是恒红死判据。"""
    rec = lib.Recorder()
    assert J.judge_submit_control(rec, events_of("suppress"), task_of("suppress"), SPEC) is False


def test_对照格红的是上下文与模型读数而不是账() -> None:
    """没钩子时账也绿、事件也绿，红的只有那三条——把「主判据测的是钩子路径」说清楚。"""
    events, task = events_of("control"), task_of("control")
    red = set(fails_for("suppress", events, task))
    assert "123 账面 goal 仍是用户写的那份（抑制的是上下文，不是账）" not in red
    assert "123 事件里不落原文正文（只留长度与指纹）" not in red


# ---------------------------------------------------------------- 2 每条判据都能失败


BATTERY = [
    # (格, 用例名, 损伤后的事件/任务行, 期望红集合)
    # 第四列全部来自实测（跑一次占位符版本，读装置打印的红集合，逐字抄回），
    # 不是从判据名字推的：一张靠推理登记的表会把写宽的判据一起放过去。
    ("suppress", "删掉抑制事件", drop_event(events_of("suppress"), "hook.prompt_substituted"),
     ["123 抑制格留下 prompt_substituted 且 suppressed=true",
      "123 抑制事件的原文读数对得上账（长度 + 12 位指纹）",
      "123 替代正文长度逐字等于夹具文本"]),
    ("suppress", "抑制位写成字符串 true",
     mutate(events_of("suppress"), "hook.prompt_substituted", "suppressed", "true"),
     ["123 抑制格留下 prompt_substituted 且 suppressed=true"]),
    ("suppress", "原文长度少一个字",
     mutate(events_of("suppress"), "hook.prompt_substituted", "original_chars",
            len(SPEC["suppress_goal"]) - 1),
     ["123 抑制事件的原文读数对得上账（长度 + 12 位指纹）"]),
    ("suppress", "指纹写成 11 位",
     mutate(events_of("suppress"), "hook.prompt_substituted", "original_sha", "0123456789a"),
     ["123 抑制事件的原文读数对得上账（长度 + 12 位指纹）"]),
    ("suppress", "替代正文长度对不上",
     mutate(events_of("suppress"), "hook.prompt_substituted", "replacement_chars",
            len(SPEC["suppress_replacement"]) - 1),
     ["123 替代正文长度逐字等于夹具文本"]),
    ("suppress", "事件里落了原文正文",
     mutate(events_of("suppress"), "hook.prompt_substituted", "goal", SPEC["suppress_goal"]),
     ["123 事件里不落原文正文（只留长度与指纹）"]),
    ("suppress", "账面 goal 被改写",
     {**task_of("suppress"), "goal": SPEC["suppress_replacement"]},
     ["123 抑制事件的原文读数对得上账（长度 + 12 位指纹）",
      "123 账面 goal 仍是用户写的那份（抑制的是上下文，不是账）"]),
    ("suppress", "模型答的是原文口令",
     {**task_of("suppress"), "result_summary": f"口令是{SPEC['suppress_marker']}"},
     ["123 模型读的是替代正文", "123 模型读数里不含原文口令"]),
    ("suppress", "模型没答替代正文",
     {**task_of("suppress"), "result_summary": "我没有可回答的内容"},
     ["123 模型读的是替代正文"]),
    ("suppress", "管家候选被原文关键词带出来",
     with_event(events_of("suppress"), "context.curated",
                {"text": f"工作区里名字相关的文件：\n- {SPEC['side_door_file']}",
                 "files": [SPEC["side_door_file"]]}),
     ["123 上下文管家的候选没被原文关键词带出来"]),
    ("blocked", "删掉拦下事件", drop_event(events_of("blocked"), "hook.prompt_blocked"),
     ["123 拦下格留下 prompt_blocked 且带着阻断原因",
      "123 拦下事件只登记长度与指纹，不落原文"]),
    ("blocked", "阻断原因被换掉",
     mutate(events_of("blocked"), "hook.prompt_blocked", "reason", "另一个原因"),
     ["123 拦下格留下 prompt_blocked 且带着阻断原因"]),
    ("blocked", "拦下之后仍然调了模型",
     events_of("blocked") + [{"type": "model.usage", "payload": {"prompt_tokens": 10}}],
     ["123 拦下时一次模型调用都没发起（无用量、无 task.started、零步）"]),
    ("blocked", "拦下之后仍报了步数",
     {**task_of("blocked"), "steps_used": 3},
     ["123 拦下时一次模型调用都没发起（无用量、无 task.started、零步）"]),
    ("blocked", "拦下被记成成功",
     {**task_of("blocked"), "status": "succeeded", "failure_reason": ""},
     ["123 任务以失败收场且原因指向钩子"]),
    ("blocked", "失败原因不指向钩子",
     {**task_of("blocked"), "failure_reason": "模型超时"},
     ["123 任务以失败收场且原因指向钩子"]),
    ("injected", "删掉补上下文事件",
     drop_event(events_of("injected"), "hook.prompt_injected"),
     ["123 补上下文格留下 prompt_injected 且 original_kept=true",
      "123 补的正文长度逐字等于夹具文本"]),
    ("injected", "补充正文长度对不上",
     mutate(events_of("injected"), "hook.prompt_injected", "chars", len(SPEC["inject_text"]) - 1),
     ["123 补的正文长度逐字等于夹具文本"]),
    ("injected", "original_kept 写成 false",
     mutate(events_of("injected"), "hook.prompt_injected", "original_kept", False),
     ["123 补上下文格留下 prompt_injected 且 original_kept=true"]),
    ("injected", "没要求抑制却冒出抑制事件",
     events_of("injected") + [{"type": "hook.prompt_substituted",
                               "payload": {"suppressed": True}}],
     ["123 没要求抑制时不许冒出抑制事件"]),
    ("injected", "事件里落了补充正文",
     mutate(events_of("injected"), "hook.prompt_injected", "text", SPEC["inject_text"]),
     ["123 补充正文不进事件（只带长度与来源）"]),
    ("injected", "模型没读到钩子补充",
     {**task_of("injected"), "result_summary": f"口令{SPEC['inject_marker']}"},
     ["123 模型既读得到原文口令、也读得到钩子补充"]),
    ("plain", "无关提交没有钩子完成事件",
     drop_event(events_of("plain"), "hook.completed"),
     ["123 无关提交上钩子仍然跑过并留审计"]),
    ("plain", "无关提交产生了改写事件",
     events_of("plain") + [{"type": "hook.prompt_injected", "payload": {"chars": 3}}],
     ["123 无关提交不产生任何改写事件"]),
    ("plain", "无关提交的原文没进模型",
     {**task_of("plain"), "result_summary": "没有可读到的口令"},
     ["123 无关提交的原文照送（模型读得到口令）"]),
]


@pytest.mark.parametrize("cell,damage_label,damaged,expected", BATTERY)
def test_电池每条损伤都红且只红该红的那几条(cell, damage_label, damaged, expected) -> None:
    """`expected=None` 是占位符——先实测、再把实测逐字登记进来。

    登记之后这张表就是判据的死亡名单：任何一条损伤不再变红，说明那条判据被写宽或删掉了。
    """
    events, task = events_of(cell), task_of(cell)
    if isinstance(damaged, dict):
        task = damaged
    else:
        events = damaged
    red = fails_for(cell, events, task)
    if expected is None:
        raise AssertionError(f"未登记期望红集合（{cell}/{damage_label}）实测={json.dumps(red, ensure_ascii=False)}")
    assert red == expected, f"{cell}/{damage_label} 红集合漂移：实测={red} 登记={expected}"


# ---------------------------------------------------------------- 3 夹具钩子：四样决定


def run_hook(goal: str) -> subprocess.CompletedProcess:
    stdin = json.dumps({"event": "user_prompt_submit", "target": "", "goal": goal},
                       ensure_ascii=False).encode("utf-8")
    return subprocess.run([str(VENV_PY), "-X", "utf8", str(DEVICE / "hooks" / "prompt_submit.py")],
                          input=stdin, capture_output=True, timeout=30)


def test_夹具对四种目标给出四样决定() -> None:
    """过产品自己的解析器，不是装置自拼的读法（#120 立的规矩）。"""
    cases = {
        "suppress": SPEC["suppress_goal"], "blocked": SPEC["block_goal"],
        "injected": SPEC["inject_goal"], "plain": SPEC["plain_goal"],
    }
    reads: dict[str, dict] = {}
    for name, goal in cases.items():
        proc = run_hook(goal)
        text = proc.stdout.decode("utf-8", errors="replace")
        reads[name] = {"exit": proc.returncode,
                       "parsed": parse_hook_output(text),
                       "stderr": proc.stderr.decode("utf-8", errors="replace")}

    assert reads["suppress"]["exit"] == 0
    assert reads["suppress"]["parsed"]["suppress_original_prompt"] is True
    assert reads["suppress"]["parsed"]["additional_context"] == SPEC["suppress_replacement"]

    assert reads["blocked"]["exit"] == 2, reads["blocked"]
    assert reads["blocked"]["stderr"] == SPEC["block_reason"]
    assert reads["blocked"]["parsed"] == {}

    assert reads["injected"]["parsed"]["additional_context"] == SPEC["inject_text"]
    assert "suppress_original_prompt" not in reads["injected"]["parsed"]

    assert reads["plain"]["parsed"] == {}, reads["plain"]


@pytest.mark.asyncio
async def test_夹具进得了产品自己的钩子运行器() -> None:
    """四格共用一份 `BLANK_HOOKS`：产品侧要真把这枚夹具当同步阻断型钩子跑起来。"""
    raw = json.dumps({"user_prompt_submit": [{
        "command": f'"{VENV_PY.as_posix()}" "{(DEVICE / "hooks" / "prompt_submit.py").as_posix()}"'}]},
        ensure_ascii=False)
    runner = HookRunner.from_env(raw)
    assert list(runner.hooks) == ["user_prompt_submit"], runner.hooks
    results = await runner.run("user_prompt_submit", "", {"goal": SPEC["suppress_goal"]})
    assert results and results[0].suppress_original is True and results[0].denied is False
    blocked = await runner.run("user_prompt_submit", "", {"goal": SPEC["block_goal"]})
    assert blocked and blocked[0].denied is True, blocked[0].as_dict()
    plain = await runner.run("user_prompt_submit", "", {"goal": SPEC["plain_goal"]})
    assert plain and plain[0].decision == "" and plain[0].additional_context == ""


def test_锚与正文只写一处夹具里不许出现第二份抄本() -> None:
    source = (DEVICE / "hooks" / "prompt_submit.py").read_text(encoding="utf-8")
    for key in ("suppress_marker", "block_marker", "inject_marker", "plain_marker",
                "suppress_replacement", "inject_text", "block_reason"):
        assert SPEC[key] not in source, f"夹具里硬抄了一份 {key}，改夹具时判据会跟着说谎"


# ---------------------------------------------------------------- 4 姿态：花钱之前先钉死


def test_提交钩子姿态只认启动器写下的那个_true() -> None:
    assert D.submit_posture_abort(None, "http://127.0.0.1:8061")
    assert D.submit_posture_abort({"posture": "hooks"}, "http://127.0.0.1:8061")
    assert D.submit_posture_abort({"posture": "hooks", "submit_hook": "true"}, "http://127.0.0.1:8061")
    assert D.submit_posture_abort({"posture": "no-hooks", "submit_hook": False}, "http://127.0.0.1:8061")
    assert D.submit_posture_abort({"posture": "hooks", "submit_hook": True}, "http://127.0.0.1:8061") == ""


def test_默认姿态与_119_那批同形且加开关才多一格() -> None:
    import hooks_config as HC

    base = json.loads(HC.hooks_json(str(VENV_PY), DEVICE / "hooks"))
    with_submit = json.loads(HC.hooks_json(str(VENV_PY), DEVICE / "hooks", with_submit=True))
    assert set(base) == {"post_tool_use", "worktree_create", "worktree_remove"}
    assert set(with_submit) == set(base) | {"user_prompt_submit"}
    assert HC.check(json.dumps(base), str(VENV_PY), DEVICE / "hooks") == []
    assert HC.check(json.dumps(with_submit), str(VENV_PY), DEVICE / "hooks", with_submit=True) == []


def test_姿态自检拒掉三种坏配置() -> None:
    import hooks_config as HC

    base = json.loads(HC.hooks_json(str(VENV_PY), DEVICE / "hooks"))
    with_submit = json.loads(HC.hooks_json(str(VENV_PY), DEVICE / "hooks", with_submit=True))
    # 说要提交钩子、配置里却没有
    assert any("缺事件 user_prompt_submit" in line for line in
               HC.check(json.dumps(base), str(VENV_PY), DEVICE / "hooks", with_submit=True)), \
        HC.check(json.dumps(base), str(VENV_PY), DEVICE / "hooks", with_submit=True)
    # 没开开关却挂着提交钩子（那台实例的姿态不是装置以为的那一份）
    assert any("姿态下允许出现" in line for line in
               HC.check(json.dumps(with_submit), str(VENV_PY), DEVICE / "hooks")), \
        HC.check(json.dumps(with_submit), str(VENV_PY), DEVICE / "hooks")
    # 指向不存在的夹具
    missing = copy.deepcopy(with_submit)
    missing["user_prompt_submit"][0]["command"] = '"python" "hooks/nope.py"'
    assert any("指向的夹具不存在" in line for line in
               HC.check(json.dumps(missing), str(VENV_PY), DEVICE / "hooks", with_submit=True))


# ---------------------------------------------------------------- 5 团队入口（#131）

# 实物：四格 × 主/对照，同一次真跑取回的任务行与群消息。原件路径记在
# 夹具 `provenance[mode][side]["artifact"]` 里，这里只留判据要吃的那几段。
TEAM = json.loads((FIXTURES / "team-submit-v123-green.json").read_text(encoding="utf-8"))
import team_submit_v123 as T  # noqa: E402

TEAM_MODES = ("suppress", "ask", "multi", "budget")
# 名册 = 主理人开场 + 两位成员 + 主理人收尾。预算格用「成员任务数 < 名册」判「没有多派」。
TEAM_ROSTER = 4
TEAM_BUDGET = T.MODE_BUDGET["budget"]


def team_rows(mode: str, side: str = "main") -> tuple[list[dict], list[dict], dict]:
    row = TEAM["rows"][mode][side]
    state = row["state"]
    return (copy.deepcopy(row["tasks"]), copy.deepcopy(state.get("messages") or []),
            copy.deepcopy(state.get("round") or {}))


def team_fails(mode: str, side: str = "main", *, tasks=None, messages=None,
               round_row=None, roster: int = TEAM_ROSTER) -> list[str]:
    """走装置自己的分派（`judge_mode`）：自检读的与真跑读出的是同一条判据链。"""
    base_tasks, base_messages, base_round = team_rows(mode, side)
    rec = lib.Recorder()
    T.judge_mode(
        rec,
        mode,
        base_tasks if tasks is None else tasks,
        base_messages if messages is None else messages,
        base_round if round_row is None else round_row,
        SPEC,
        roster,
    )
    return list(rec.fails)


@pytest.mark.parametrize("mode", TEAM_MODES)
def test_团队入口四格的主格实物全绿(mode: str) -> None:
    assert team_fails(mode) == [], mode


@pytest.mark.parametrize("mode", TEAM_MODES)
def test_团队入口四格对照格的红集合精确等于登记的那一组(mode: str) -> None:
    tasks, messages, round_row = team_rows(mode, "control")
    rec = lib.Recorder()
    T.judge_mode(rec, mode, tasks, messages, round_row, SPEC, TEAM_ROSTER)
    expected = T.CONTROL_FOR.get(mode, T.CONTROL_DEFAULT)
    assert T.control_red_exact(rec, mode) is True, list(rec.fails)
    assert list(rec.fails) == list(expected)


@pytest.mark.parametrize("mode", TEAM_MODES)
def test_团队入口同一套对照判据打在带钩子的实物上不该全红(mode: str) -> None:
    """反向格：对照族的红只在「没钩子」那台上出现，否则它就是恒红死判据。"""
    rec = lib.Recorder()
    tasks, messages, round_row = team_rows(mode)
    T.judge_mode(rec, mode, tasks, messages, round_row, SPEC, TEAM_ROSTER)
    assert T.control_red_exact(rec, mode) is False


def test_团队入口四格的来源与对照读数都记在夹具里() -> None:
    """夹具自己得说清这八份实物各是哪台实例、跑出什么——不然读数无法追溯。"""
    for mode in TEAM_MODES:
        for side in ("main", "control"):
            prop = TEAM["provenance"][mode][side]
            assert prop["rc"] == 0, (mode, side, prop)
            assert Path(prop["artifact"]).name.startswith(f"team-submit-v123-{mode}"), prop
            if side == "main":
                assert prop["red"] == [], (mode, prop["red"])
                assert prop["control_red"] is None
            else:
                assert prop["control_red"] is True
                assert prop["red"] == list(T.CONTROL_FOR.get(mode, T.CONTROL_DEFAULT)), (mode, prop)


def test_团队入口对照格的姿态闸门只放行_no_hooks() -> None:
    """留痕缺失、坏 JSON 读成空、带钩子的那台——三种都不许当「大概没钩子」放过去。"""
    assert T.control_posture_abort({"posture": "no-hooks"}, "http://127.0.0.1:8065") == ""
    assert T.control_posture_abort(None, "http://127.0.0.1:8065")
    assert T.control_posture_abort({}, "http://127.0.0.1:8065")
    assert T.control_posture_abort({"posture": "hooks", "submit_hook": True}, "http://127.0.0.1:8061")
    assert T.control_posture_abort({"posture": "hooks", "submit_hook": "true"}, "http://127.0.0.1:8061")


def _last_user(messages: list[dict]) -> str:
    texts = [str(item.get("content") or "") for item in messages if item.get("role") == "user"]
    return texts[-1] if texts else ""


def _started_event(task: dict) -> dict:
    for event in task.get("events") or []:
        if event["type"] == "team.turn.started":
            return event
    raise AssertionError(f"任务 {task.get('id')} 没有 team.turn.started")


def _member_tasks(tasks: list[dict]) -> list[dict]:
    return [task for task in tasks
            if any(e["type"] == "team.turn.started" for e in task.get("events") or [])]


def _set_bots(messages: list[dict], text: str) -> None:
    for item in messages:
        if item.get("role") == "bot":
            item["content"] = text


def team_damage(mode: str, kind: str) -> tuple[list[dict], list[dict], dict]:
    """按名字造一处损伤；标签就是契约的一部分，拼错的名字必须炸。"""
    tasks, messages, round_row = team_rows(mode)
    members = _member_tasks(tasks)
    if kind == "任务行整个丢了":
        tasks = []
    elif kind == "删掉抑制事件":
        for task in tasks:
            task["events"] = [e for e in task.get("events") or []
                              if e["type"] != "hook.prompt_substituted"]
    elif kind == "抑制位写成字符串 true":
        for task in members:
            for event in task["events"]:
                if event["type"] == "hook.prompt_substituted":
                    event["payload"]["suppressed"] = "true"
    elif kind == "账面 goal 被换成替代正文":
        members[0]["task"]["goal"] = SPEC["suppress_replacement"]
    elif kind == "机器人没答替代正文口令":
        _set_bots(messages, "我读到的是一段改写后的任务说明。")
    elif kind == "机器人发言里带上了原文口令":
        bot = next(item for item in messages if item.get("role") == "bot")
        bot["content"] = f"口令是{SPEC['suppress_marker']}"
    elif kind == "删掉阻断事件":
        for task in tasks:
            task["events"] = [e for e in task.get("events") or [] if e["type"] != "hook.prompt_blocked"]
    elif kind == "阻断决定从 ask 改成 deny":
        for task in members:
            for event in task["events"]:
                if event["type"] == "hook.prompt_blocked":
                    event["payload"]["decision"] = "deny"
    elif kind == "拦下之后还是发起了模型调用":
        members[0]["events"].append({"type": "model.usage", "payload": {"total_tokens": 1}})
    elif kind == "失败改成成功收场":
        members[0]["task"]["status"] = "succeeded"
        members[0]["task"]["failure_reason"] = ""
    elif kind == "群里冒充模型答复":
        _set_bots(messages, SPEC["ask_answer"])
    elif kind == "收尾轮丢了":
        # 摘掉收尾标记并把账面改成原文：只剩结构判据能发现「这一轮没收尾」。
        for task in members:
            started = _started_event(task)
            if started["payload"].get("wrap_up"):
                started["payload"]["wrap_up"] = False
                task["task"]["goal"] = _last_user(messages)
    elif kind == "同一轮只剩一位成员":
        first = _started_event(members[0])["payload"]
        for task in members[1:]:
            payload = _started_event(task)["payload"]
            payload["bot_id"] = first.get("bot_id")
            payload["bot_name"] = first.get("bot_name")
    elif kind == "多出一次收尾":
        extra = next((task for task in members
                      if not _started_event(task)["payload"].get("wrap_up")), members[0])
        _started_event(extra)["payload"]["wrap_up"] = True
        extra["task"]["goal"] = J.WRAP_UP_PREFIX + _last_user(messages)
    elif kind == "群轮以失败收场":
        round_row["status"] = "failed"
    elif kind == "预算截断的系统消息丢了":
        messages[:] = [m for m in messages if "达到预算上限" not in str(m.get("content") or "")]
    elif kind == "本轮没有达到预算线":
        round_row["tokens"] = TEAM_BUDGET - 1
    elif kind == "轮合计与逐条用量对不上":
        bot = next(item for item in messages if item.get("role") == "bot")
        usage = dict((bot.get("meta") or {}).get("usage") or {})
        usage["total_tokens"] = int(usage.get("total_tokens") or 0) + 1
        bot.setdefault("meta", {})["usage"] = usage
    elif kind == "截断之后又派了一条收尾":
        task = members[-1]
        _started_event(task)["payload"]["wrap_up"] = True
        task["task"]["goal"] = J.WRAP_UP_PREFIX + _last_user(messages)
    else:
        raise AssertionError(f"未知损伤 {kind}")
    return tasks, messages, round_row


TEAM_BATTERY = [
    # (格, 损伤名, 期望红集合)。期望全部来自实测：先跑占位符版本，把打印的红集合逐字抄回。
    ('suppress', '任务行整个丢了', [
        "123 团队入口：至少一条成员任务进了群轮",
        "123 团队入口：每条成员任务都留下 suppressed=true 的抑制事件",
        "123 团队入口：成员任务的账面 goal 逐字等于这一次提交的正文（收尾轮带前缀）",
    ]),
    ('suppress', '删掉抑制事件', [
        "123 团队入口：每条成员任务都留下 suppressed=true 的抑制事件",
    ]),
    ('suppress', '抑制位写成字符串 true', [
        "123 团队入口：每条成员任务都留下 suppressed=true 的抑制事件",
    ]),
    ('suppress', '账面 goal 被换成替代正文', [
        "123 团队入口：成员任务的账面 goal 逐字等于这一次提交的正文（收尾轮带前缀）",
    ]),
    ('suppress', '机器人没答替代正文口令', [
        "123 团队入口：机器人发言里出现替代正文的口令",
    ]),
    ('suppress', '机器人发言里带上了原文口令', [
        "123 团队入口：机器人发言里没有原文口令",
    ]),
    ('ask', '任务行整个丢了', [
        "123 团队入口 ask：至少一条成员任务进了群轮",
        "123 团队入口 ask：每条成员任务都留下 decision=ask 的阻断事件",
        "123 团队入口 ask：拦下时一条模型调用都没发起（无用量、无 task.started、零步）",
        "123 团队入口 ask：任务以失败收场且原因写明按拒绝处理",
        "123 团队入口 ask：账面 goal 逐字等于这一次提交的正文（收尾轮带前缀）",
    ]),
    ('ask', '删掉阻断事件', [
        "123 团队入口 ask：每条成员任务都留下 decision=ask 的阻断事件",
    ]),
    ('ask', '阻断决定从 ask 改成 deny', [
        "123 团队入口 ask：每条成员任务都留下 decision=ask 的阻断事件",
    ]),
    ('ask', '拦下之后还是发起了模型调用', [
        "123 团队入口 ask：拦下时一条模型调用都没发起（无用量、无 task.started、零步）",
    ]),
    ('ask', '失败改成成功收场', [
        "123 团队入口 ask：任务以失败收场且原因写明按拒绝处理",
    ]),
    ('ask', '群里冒充模型答复', [
        "123 团队入口 ask：群里念得出拒绝原因、没有冒充模型答复",
    ]),
    ('ask', '账面 goal 被换成替代正文', [
        "123 团队入口 ask：账面 goal 逐字等于这一次提交的正文（收尾轮带前缀）",
    ]),
    ('multi', '任务行整个丢了', [
        "123 团队入口：至少一条成员任务进了群轮",
        "123 团队入口：每条成员任务都留下 suppressed=true 的抑制事件",
        "123 团队入口：成员任务的账面 goal 逐字等于这一次提交的正文（收尾轮带前缀）",
        "123 团队多成员：同一轮里至少两位不同成员各有一条任务",
        "123 团队多成员：收尾轮存在且只有一条",
    ]),
    ('multi', '同一轮只剩一位成员', [
        "123 团队多成员：同一轮里至少两位不同成员各有一条任务",
    ]),
    ('multi', '收尾轮丢了', [
        "123 团队多成员：收尾轮存在且只有一条",
    ]),
    ('multi', '多出一次收尾', [
        "123 团队多成员：收尾轮存在且只有一条",
    ]),
    ('multi', '群轮以失败收场', [
        "123 团队多成员：群轮以 completed 收场",
    ]),
    ('budget', '预算截断的系统消息丢了', [
        "123 团队预算：这一轮确实在预算上限停下来",
    ]),
    ('budget', '本轮没有达到预算线', [
        "123 团队预算：这一轮确实在预算上限停下来",
        "123 团队预算：轮合计等于各条发言用量之和",
    ]),
    ('budget', '轮合计与逐条用量对不上', [
        "123 团队预算：轮合计等于各条发言用量之和",
    ]),
    ('budget', '截断之后又派了一条收尾', [
        "123 团队预算：停下来的那一步没有给后续成员派任务",
    ]),
    ('budget', '群轮以失败收场', [
        "123 团队预算：轮以 completed 收场",
    ]),
]


def test_损伤名拼错时炸而不是静默跳过() -> None:
    with pytest.raises(AssertionError):
        team_damage("suppress", "这个名字不存在")


@pytest.mark.parametrize("mode,damage_label,expected", TEAM_BATTERY)
def test_团队入口每条损伤都红且只红该红的那几条(mode: str, damage_label: str, expected) -> None:
    tasks, messages, round_row = team_damage(mode, damage_label)
    red = team_fails(mode, tasks=tasks, messages=messages, round_row=round_row)
    if expected is None:
        raise AssertionError(
            f"未登记期望红集合（{mode}/{damage_label}）实测={json.dumps(red, ensure_ascii=False)}")
    assert red == expected, f"{mode}/{damage_label} 红集合漂移：实测={red} 登记={expected}"


def test_驱动参数只认四种模式与对照开关() -> None:
    """模式名是契约的一部分：拼错的参数不许悄悄落回默认格（那会拿真模型的钱跑错题）。"""
    assert T.parse_args([]) == ("suppress", False)
    assert T.parse_args(["--mode", "ask"]) == ("ask", False)
    assert T.parse_args(["--mode=budget", "--control"]) == ("budget", True)
    for bad in (["--mode", "nope"], ["--mode"], ["--wat"], ["--control=true"]):
        with pytest.raises(SystemExit):
            T.parse_args(bad)


def test_对照红集合按模式分派且必须精确相等() -> None:
    assert set(T.CONTROL_FOR["ask"]) == set(J.CONTROL_123_TEAM_ASK_RED)
    assert T.CONTROL_FOR.get("suppress", T.CONTROL_DEFAULT) is J.CONTROL_123_TEAM_RED
    for mode in ("suppress", "multi", "budget"):
        assert T.CONTROL_FOR.get(mode, T.CONTROL_DEFAULT) is J.CONTROL_123_TEAM_RED, mode
    rec = lib.Recorder()
    for name in J.CONTROL_123_TEAM_ASK_RED:
        rec.check(name, False, "")
    assert T.control_red_exact(rec, "ask") is True
    rec.check("多出来的一条", False, "")
    assert T.control_red_exact(rec, "ask") is False


def test_点名把成员放进用户发言() -> None:
    """不点名时队列里只有主理人：多成员与收尾根本不会发生，判据也就没有多份样本。"""
    assert T.compose_goal("正文", []) == "正文"
    goal = T.compose_goal("正文", ["甲", "乙"])
    assert goal.startswith("正文") and "@甲" in goal and "@乙" in goal


def test_收尾前缀与产品里的那一句同字() -> None:
    """装置按「前缀 + 原文」算收尾轮账面；产品改了前缀而这里没改，判据会静默变宽。"""
    import blank_api.services.team as team

    source = Path(team.__file__).read_text(encoding="utf-8")
    assert f'f"{J.WRAP_UP_PREFIX}{{user_text}}"' in source, "team.py 的收尾前缀与装置登记的不一致"
