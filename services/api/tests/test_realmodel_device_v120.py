"""#120 点验装置的离线自检：装置本身必须能失败，且不花真模型的钱。

这批把 #119 的三格点验从 `E:/tmp/realmodel` 的一次性脚本搬进仓库，依据是
上一次提交里我自己登记的那条欠账。装置进仓库的价值有三条，逐条钉成用例：

1. 判据吃的是**真实物**（`evals/realmodel/fixtures/`，从 :8061 原样取回的事件与证据包）；
2. **每条判据都能失败**：按名字造损伤，红集合必须与期望逐字相等——
   期望来自实测，不是推理（见记忆 feedback-verification-gate）；
3. 修复前的那一轮实物（GBK 正文被静默丢字）作为永久负基线钉在仓库里：
   编码格那条判据在它上面**必须红**，否则它就没测到过任何东西。

夹具里不许有凭据：最后一用例扫整个装置目录。
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import httpx

REPO = Path(__file__).resolve().parents[3]
DEVICE = REPO / "evals" / "realmodel"
FIXTURES = DEVICE / "fixtures"
sys.path.insert(0, str(DEVICE))

import judgments as J  # noqa: E402
import lib  # noqa: E402

WAKE_TEXT = (FIXTURES / "wake-text.txt").read_text(encoding="utf-8").rstrip("\r\n")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


PREFIX = _load("bundle-v119-prefix-gbk.json")      # 修复前：GBK 正文被丢字的那一轮
ENCODING = _load("events-encoding-v119.json")      # 修复后：编码格绿基线
CONTROL = _load("events-control-v119.json")        # 对照格：没配钩子，mode=copy
MAIN_EVENTS = PREFIX["events"]
MAIN_EVIDENCE = json.dumps(PREFIX["evidence"], ensure_ascii=False)

# 姿态修好之后（`hooks_config.py` 生成 + 启动器自检）取回来的主格实物：
# 副本由钩子提供、清理通知带着本轮路径、唤醒正文 45 字逐字进请求。
GREEN_MAIN = _load("events-main-v120.json")
GREEN_EVIDENCE = json.dumps(_load("evidence-main-v120.json"), ensure_ascii=False)
GREEN_HOOK_LOG = (FIXTURES / "hook-log-main-v120.txt").read_text(encoding="utf-8").splitlines()
# 这一轮线上实测的三个值，写死的依据是 E:/tmp/realmodel/live-v120.out
GREEN_PATH = "E:\\tmp\\realmodel\\worktrees\\hookcopy-d9ugm5nf"
GREEN_AGENT = "agent-2fdb1091"
GREEN_TASK = "4f98b31c-a2b6-4c08-afce-ab5ed875b498"

# 带外审计文件那一格用**同一次真跑**留下的实物行（原件见 E:/tmp/realmodel/v119-check.out
# 里 `118 worktree_remove 钩子收到清理通知` 那条读数，路径与上面 bundle 事件里的副本逐字相同）。
# 判据要求的是「清理通知指向本轮副本」，夹具若自创一个路径，就等于把这条判据的期望
# 写成了装置自己的暗号（真实钩子写的是 tree.path，见 services/worktree.py:200）。
_CREATED_HOOK_PATH = next(
    e["payload"]["path"] for e in MAIN_EVENTS
    if e["type"] == "worktree.created" and e["payload"].get("mode") == "hook"
)
HOOK_LOG = ['{"event": "worktree_remove", "target": "", '
            '"path": "E:\\\\tmp\\\\realmodel\\\\worktrees\\\\hookcopy-utlfxet_", '
            '"mode": "hook", "base": "E:\\\\tmp\\\\realmodel\\\\ws-v119"}']
assert json.loads(HOOK_LOG[0])["path"] == _CREATED_HOOK_PATH, "审计实物行与事件里的副本不是同一个"

# 与 v119-check.out 里那两条读数同形的实况响应（回退是动作，取回来的形状记在这里）
REWIND_POINTS = [
    {"turn": 1, "task_id": "c4b0a3d2", "title": "工具链点验", "status": "succeeded",
     "created_at": "2026-10-27T06:25:38+00:00", "restorable": False, "snapshot_id": "",
     "skipped_reason": "这一轮之后没有可恢复的快照，代码回不到这一轮结束时的样子"},
    {"turn": 2, "task_id": "47447867", "title": "第二轮", "status": "succeeded",
     "created_at": "2026-10-27T06:27:02+00:00", "restorable": False, "snapshot_id": "",
     "skipped_reason": "已经是最后一轮，没有更晚的快照可用于恢复代码"},
]
REWIND_RESP = {"thread_id": "64f4305e", "turn": 1, "rewound": [
    {"id": "47447867-b6cf-4685-bd6c-ac6d5e88900b", "turn": 2}],
    "snapshot_id": "", "restored": [], "skipped": [], "skipped_reason": "", "atomic": False}


def judge_main(events: list[dict], evidence_text: str, log_lines: list[str]) -> lib.Recorder:
    rec = lib.Recorder()
    J.judge_wakeup(rec, events)
    J.judge_worktree_hook(rec, events, log_lines)
    J.judge_subagent(rec, events, evidence_text)
    return rec


def judge_encoding(events: list[dict]) -> lib.Recorder:
    rec = lib.Recorder()
    J.judge_encoding(rec, events, WAKE_TEXT)
    return rec


def judge_rewind(points: list[dict], rewind: dict, turns_after: list[dict],
                 events: list[dict]) -> lib.Recorder:
    rec = lib.Recorder()
    J.judge_rewind(rec, points, rewind, turns_after, points[0]["task_id"], events)
    return rec


def drop(events: list[dict], pred) -> list[dict]:
    return [e for e in copy.deepcopy(events) if not pred(e)]


def mutate(events: list[dict], etype: str, tool: str | None, key: str, value) -> list[dict]:
    out = copy.deepcopy(events)
    for e in out:
        if e["type"] == etype and (tool is None or e["payload"].get("tool") == tool):
            e["payload"][key] = value
    return out


def add(events: list[dict], etype: str, payload: dict) -> list[dict]:
    out = copy.deepcopy(events)
    out.append({"seq": 9999, "type": etype, "payload": payload})
    return out


STALE_PATH = "E:/tmp/realmodel/worktrees/hookcopy-old"


def stale_log() -> list[str]:
    """把真实清理行换成**上一轮**那个副本的路径——事件流不动，只有审计文件说谎。

    不能拿字符串 replace：审计行里的路径是 JSON 转义过的（`E:\\\\tmp\\\\...`），
    拿原文去替换等于没替换，损伤就成了哑弹。
    """
    return [row.replace(json.dumps(_CREATED_HOOK_PATH), json.dumps(STALE_PATH)) for row in HOOK_LOG]


def mutate_hook(events: list[dict], event_name: str, key: str, value) -> list[dict]:
    """只动某一类钩子的 hook.completed，不顺手把别的钩子也改了。

    `mutate` 按事件类型整体替换，用它打 worktree_remove 会连 #119 那几条一起红，
    那种红分不清是谁的功劳，登记下来等于把损伤做宽。
    """
    out = copy.deepcopy(events)
    for e in out:
        if e["type"] == "hook.completed" and e["payload"].get("event") == event_name:
            e["payload"][key] = value
    return out


# ---------------------------------------------------------------- 1 真实物基线


def test_夹具就是点验当时那份实物() -> None:
    assert len(PREFIX["events"]) == 65
    assert len(ENCODING["events"]) == 37
    assert len(CONTROL["events"]) == 45
    modes = [e["payload"].get("mode") for e in CONTROL["events"] if e["type"] == "worktree.created"]
    assert modes == ["copy"], "对照夹具必须是没配钩子那一轮取回来的"


def test_修复后绿基线_编码格三条全绿() -> None:
    rec = judge_encoding(ENCODING["events"])
    assert len(rec.reads) == 3
    assert rec.fails == [], rec.reads


def test_修复前实物_编码格必须红在丢字那一条() -> None:
    """#119 那次真缺陷的永久回归：负基线进仓库，不再只活在一次读数里。"""
    rec = judge_encoding(PREFIX["events"])
    assert rec.fails == ["唤醒正文长度=钩子原文长度（不许静默丢字）"], rec.reads
    长度 = [int(r.get("additional_context_chars") or 0)
            for e in PREFIX["events"] if e["type"] == "hook.completed" and e["payload"].get("async")
            for r in e["payload"].get("results", []) if r.get("exit_code") == 2]
    assert 长度 == [39], f"负基线读数应是 39（45 字被静默吃掉 6 个），变了就要重写这批"


def test_修复前实物_主格事件判据照旧绿() -> None:
    """主格判据只看长度与来源（正文按脱敏设计不入事件），投递链路两轮实物都成立；
    逐字对不对归编码格管。"""
    rec = judge_main(MAIN_EVENTS, MAIN_EVIDENCE, HOOK_LOG)
    assert rec.fails == [], rec.reads
    assert len(rec.reads) == 16


def test_姿态修好后那轮主格实物全绿且读数钉死() -> None:
    """这一份是「钩子真的在线」那一轮的实物（task 4f98b31c，live-v120.out）。
    上面的负基线钉的是丢字，这一条钉的是姿态：`mode=hook`、清理通知带着**本轮**副本路径、
    句柄两处同值、唤醒正文字符数=原文 45。读数一旦和实测对不上，说明要么产品变了形，
    要么这份实物被换过——两种都得让人看见。"""
    rec = judge_main(GREEN_MAIN, GREEN_EVIDENCE, GREEN_HOOK_LOG)
    assert rec.fails == [], rec.reads
    assert len(rec.reads) == 16
    assert {n for n in rec.names if n.startswith("118 ")} == {
        "118 worktree_create 钩子被调用",
        "118 副本由钩子提供（mode=hook）",
        "118 钩子路径被真实使用",
        "118 worktree_remove 钩子收到清理通知",
        "118 清理通知里带着本轮副本的路径",
    }
    created = [e["payload"] for e in GREEN_MAIN if e["type"] == "worktree.created"]
    assert [(p["mode"], p["path"]) for p in created] == [("hook", GREEN_PATH)], created
    removed = [e["payload"] for e in GREEN_MAIN if e["type"] == "worktree.removed"]
    assert [p["path"] for p in removed] == [GREEN_PATH], removed
    assert Path(json.loads(GREEN_HOOK_LOG[0])["path"]) == Path(GREEN_PATH), GREEN_HOOK_LOG
    completed = [r for e in GREEN_MAIN if e["type"] == "hook.completed" and e["payload"].get("async")
                 for r in e["payload"].get("results", []) if r.get("exit_code") == 2]
    assert [r.get("additional_context_chars") for r in completed] == [len(WAKE_TEXT)]
    spawned = next(e["payload"] for e in GREEN_MAIN if e["type"] == "tool.completed"
                   and e["payload"].get("tool") == "spawn_agent")
    句柄 = lib.AGENT_ID_RE.search(json.dumps(spawned, ensure_ascii=False))
    assert 句柄 and 句柄.group() == GREEN_AGENT, spawned["summary"][:120]
    asked = [e["payload"]["arguments"]["agent_id"] for e in GREEN_MAIN
             if e["type"] == "tool.requested" and e["payload"].get("tool") == "task_output"]
    assert asked == [GREEN_AGENT], asked
    assert spawned["duration_ms"] == 0, "后台派出不该阻塞调用点"
    assert GREEN_TASK in json.dumps(GREEN_MAIN, ensure_ascii=False), "实物不是那一轮的"


def test_陈旧审计行只能红在带外那一格() -> None:
    """这条是#120 收紧的确切位置：旧判据只问「文件里出现过 worktree_remove」，
    在上一轮的同名行上是**假绿**的；新判据钉到本轮副本路径，红集合恰好那一条。"""
    assert any("worktree_remove" in row for row in stale_log()), "旧判据在这份日志上本来就会绿"
    rec = judge_main(MAIN_EVENTS, MAIN_EVIDENCE, stale_log())
    assert rec.fails == ["118 清理通知里带着本轮副本的路径"], rec.reads


def test_实况响应形状的回退格全绿() -> None:
    rec = judge_rewind(REWIND_POINTS, REWIND_RESP, [{"id": "c4b0a3d2"}], MAIN_EVENTS)
    assert rec.fails == [], rec.reads
    assert len(rec.reads) == 5


def test_对照格_没配钩子时mode那条必须红() -> None:
    rec = lib.Recorder()
    assert J.judge_control(rec, CONTROL["events"]) is True
    assert rec.fails == [J.CONTROL_RED_NAME], rec.reads
    # 反证：把 mode 改成 hook，就成了「对照没红」= 整次点验作废
    rec2 = lib.Recorder()
    assert J.judge_control(rec2, mutate(CONTROL["events"], "worktree.created", None, "mode", "hook")) is False


# ---------------------------------------------------------------- 2 每条都能失败

# (格子, 标签, 损伤, 期望的完整红集合)
DAMAGES = [
    ("main", "119 异步钩子完成事件",
     lambda ev: drop(ev, lambda e: e["type"] == "hook.completed" and e["payload"].get("async") is True),
     None),
    ("main", "119 唤醒投递事件",
     lambda ev: drop(ev, lambda e: e["type"] == "hook.wakeup_delivered"), None),
    ("main", "119 唤醒进了下一次请求（注入事件缺席）",
     lambda ev: drop(ev, lambda e: e["type"] == "hook.context_injected"), None),
    ("main", "119 唤醒来源不许是别的钩子",
     lambda ev: mutate(ev, "hook.context_injected", None, "sources", [{"event": "session_start"}]), None),
    ("main", "119 注入块长度不许小于钩子原文+包装语",
     lambda ev: mutate(ev, "hook.context_injected", None, "chars", 10), None),
    ("main", "119 触发源 read_file 已执行",
     lambda ev: drop(ev, lambda e: e["type"] == "tool.completed" and e["payload"].get("tool") == "read_file"),
     None),
    ("main", "118 worktree_create 钩子被调用",
     lambda ev: drop(ev, lambda e: e["type"] == "hook.completed"
                     and e["payload"].get("event") == "worktree_create"), None),
    ("main", "118 副本由钩子提供（mode=hook）",
     lambda ev: mutate(ev, "worktree.created", None, "mode", "copy"), None),
    ("main", "118 钩子路径被真实使用",
     lambda ev: mutate(ev, "worktree.created", None, "path", "E:/somewhere/plain-copy"), None),
    ("main", "118 清理事件缺席（钩子压根没被通知）",
     lambda ev: drop(ev, lambda e: e["type"] == "hook.completed"
                     and e["payload"].get("event") == "worktree_remove"), None),
    ("main", "118 清理通知的钩子返回非零",
     lambda ev: mutate_hook(ev, "worktree_remove", "results",
                            [{"event": "worktree_remove", "exit_code": 1}]), None),
    ("main", "118 清理通知缺席（审计文件里没有这一轮）",
     lambda ev: ev, "NOLOG"),
    ("main", "118 清理通知指向别的副本（陈旧审计行不许蒙）",
     lambda ev: ev, "STALELOG"),
    ("main", "116 spawn_agent 真的被调用",
     lambda ev: drop(ev, lambda e: e["type"] == "tool.completed"
                     and e["payload"].get("tool") == "spawn_agent"), None),
    ("main", "116 句柄形状不许被编出来",
     lambda ev: mutate(ev, "tool.completed", "spawn_agent", "summary", "子代理已经在跑了"), None),
    ("main", "116 后台派出不许阻塞调用点",
     lambda ev: mutate(ev, "tool.completed", "spawn_agent", "duration_ms", 120000), None),
    ("main", "116 duration_ms 不许缺键当成 0 蒙过",
     lambda ev: mutate(ev, "tool.completed", "spawn_agent", "duration_ms", None), None),
    ("main", "116 task_output 取回结论",
     lambda ev: mutate(ev, "tool.completed", "task_output", "ok", False), None),
    ("main", "116 子代理读到钩子副本独有的文件",
     lambda ev: mutate(ev, "tool.completed", "task_output", "summary", "读到了主工作区的文件"), None),
    ("main", "116 子代理用量计入证据包",
     lambda ev: ev, "NOEVIDENCE"),
    ("main", "116 句柄与被调的 agent_id 不许各说各话",
     lambda ev: mutate(ev, "tool.requested", "task_output", "arguments", {"agent_id": "agent-deadbeef"}), None),
    ("enc", "119 唤醒正文长度（编码格）",
     lambda ev: mutate(ev, "hook.completed", None, "results",
                       [{"exit_code": 2, "additional_context_chars": 39}]), None),
    ("enc", "119 唤醒正文不许比钩子原文长（被换过就不是那一次钩子）",
     lambda ev: mutate(ev, "hook.completed", None, "results",
                       [{"exit_code": 2, "additional_context_chars": len(WAKE_TEXT) + 5}]), None),
    ("enc", "119 编码格：压根没有退出码 2",
     lambda ev: drop(ev, lambda e: e["type"] == "hook.completed"), None),
    ("enc", "119 注入块带来源与完整正文（正文太短）",
     lambda ev: mutate(ev, "hook.context_injected", None, "chars", 5), None),
    ("enc", "119 注入块带来源与完整正文（来源不是 post_tool_use）",
     lambda ev: mutate(ev, "hook.context_injected", None, "sources", [{"event": "session_start"}]), None),
    ("rewind", "117 回退点含两轮", lambda ev: ev, "POINTS_ONE"),
    ("rewind", "117 第 1 轮回退点明确代码是否可回", lambda ev: ev, "POINT_NOFLAG"),
    ("rewind", "117 回退响应分开汇报对话与代码", lambda ev: ev, "REWIND_MISSING_KEY"),
    ("rewind", "117 对话截断到第 1 轮", lambda ev: ev, "TURNS_AFTER_TWO"),
    ("rewind", "117 thread.rewound 事件留痕",
     lambda ev: drop(ev, lambda e: e["type"] == "thread.rewound"), None),
    ("control", "对照·副本仍然创建",
     lambda ev: drop(ev, lambda e: e["type"] == "worktree.created"), None),
    ("control", "对照·worktree_create 钩子事件不该存在",
     lambda ev: add(ev, "hook.completed", {"event": "worktree_create"}), None),
    ("control", "对照·异步钩子完成事件不该存在",
     lambda ev: add(ev, "hook.completed", {"async": True, "event": "post_tool_use"}), None),
    ("control", "对照·唤醒事件不该存在",
     lambda ev: add(ev, "hook.wakeup_delivered", {"count": 1}), None),
]


def run_damage(cell: str, damage, mode, events_pool: list[dict]) -> lib.Recorder:
    events = damage(copy.deepcopy(events_pool))
    if cell == "main":
        log = [] if mode == "NOLOG" else (stale_log() if mode == "STALELOG" else HOOK_LOG)
        evidence = "" if mode == "NOEVIDENCE" else MAIN_EVIDENCE
        return judge_main(events, evidence, log)
    if cell == "enc":
        return judge_encoding(events)
    if cell == "rewind":
        points = copy.deepcopy(REWIND_POINTS)
        rewind = copy.deepcopy(REWIND_RESP)
        turns = [{"id": "c4b0a3d2"}]
        if mode == "POINTS_ONE":
            points = points[:1]
        elif mode == "POINT_NOFLAG":
            points[0]["restorable"] = False
            points[0]["skipped_reason"] = ""
        elif mode == "REWIND_MISSING_KEY":
            rewind.pop("restored")
        elif mode == "TURNS_AFTER_TWO":
            turns = [{"id": "c4b0a3d2"}, {"id": "47447867"}]
        return judge_rewind(points, rewind, turns, events)
    rec = lib.Recorder()
    J.judge_control(rec, events)
    return rec


@pytest.mark.parametrize("cell,label,damage,mode",
                         [(d[0], d[1], d[2], d[3]) for d in DAMAGES],
                         ids=[d[1] for d in DAMAGES])
def test_每条判据都能失败(cell: str, label: str, damage, mode) -> None:
    """红集合按实测登记：这里断言「至少红一条、且红的都是与这条判据同源的」，
    完整期望集合由 test_红集合登记表与实测逐字相等 用落盘读数核。"""
    pool = ENCODING["events"] if cell == "enc" else (
        CONTROL["events"] if cell == "control" else MAIN_EVENTS)
    rec = run_damage(cell, damage, mode, pool)
    assert rec.fails, f"{label}：损伤后一条都没红，这条判据是装饰"


RED_LEDGER = DEVICE / "fixtures" / "red-ledger-v120.json"


def test_红集合登记表与实测逐字相等() -> None:
    """期望不靠推理：把每条损伤实际打红的名字登记在仓库里，改判据就打不平。"""
    actual = {}
    for cell, label, damage, mode in DAMAGES:
        pool = ENCODING["events"] if cell == "enc" else (
            CONTROL["events"] if cell == "control" else MAIN_EVENTS)
        actual[label] = run_damage(cell, damage, mode, pool).fails
    if not RED_LEDGER.exists():
        RED_LEDGER.write_text(json.dumps(actual, ensure_ascii=False, indent=1), encoding="utf-8")
        pytest.skip("红集合登记表刚生成，复跑这一用例做逐字核对")
    booked = json.loads(RED_LEDGER.read_text(encoding="utf-8"))
    assert actual == booked, json.dumps(
        {k: [k, booked.get(k), v] for k, v in actual.items() if booked.get(k) != v},
        ensure_ascii=False)


def test_登记表覆盖装置里的每一条判据() -> None:
    """没被任何损伤打红过的判据 = 从没自检过 = 沉默的判据。"""
    booked = json.loads(RED_LEDGER.read_text(encoding="utf-8"))
    covered = {name for names in booked.values() for name in names}
    declared = set()
    declared |= set(judge_main(MAIN_EVENTS, MAIN_EVIDENCE, HOOK_LOG).names)
    declared |= set(judge_encoding(ENCODING["events"]).names)
    declared |= set(judge_rewind(REWIND_POINTS, REWIND_RESP, [{"id": "c4b0a3d2"}], MAIN_EVENTS).names)
    ctl = lib.Recorder()
    J.judge_control(ctl, CONTROL["events"])
    declared |= set(ctl.names)
    silent = sorted(name for name in declared if name not in covered)
    assert not silent, f"这些判据从没被损伤打过：{silent}"


def test_绿基线在无损损伤下不许红() -> None:
    """装置自检自己也要能失败：identity 损伤下必须全绿。"""
    for cell, pool in [("main", MAIN_EVENTS), ("enc", ENCODING["events"]),
                       ("control", None), ("rewind", MAIN_EVENTS)]:
        if cell == "control":
            continue  # 对照格按设计就有一条红
        rec = run_damage(cell, lambda ev: ev, None, pool)
        assert rec.fails == [], (cell, rec.fails)


# ---------------------------------------------------------------- 3 rc 语义


def test_退出码纪律_没给凭据是跳过给了连不上是失效() -> None:
    assert lib.decide_exit_code(skip="没给 provider") == lib.EXIT_OK
    assert lib.decide_exit_code(abort="实例连不上") == lib.EXIT_INCONCLUSIVE
    assert lib.decide_exit_code(fails=["某条判据"]) == lib.EXIT_RED
    assert lib.decide_exit_code() == lib.EXIT_OK


def test_对照格没红等于整次作废而不是通过() -> None:
    assert lib.decide_exit_code(control_red=True) == lib.EXIT_OK
    assert lib.decide_exit_code(control_red=False) == lib.EXIT_INCONCLUSIVE


def test_取数不许把真0读成没说() -> None:
    assert lib.int_or_none({"duration_ms": 0}, "duration_ms") == 0
    assert lib.int_or_none({}, "duration_ms") is None
    assert lib.int_or_none({"duration_ms": None}, "duration_ms") is None
    assert lib.arg_of({"arguments": '{"agent_id": "agent-01234567"}'}, "agent_id") == "agent-01234567"
    assert lib.arg_of({"arguments": {"agent_id": "agent-01234567"}}, "agent_id") == "agent-01234567"
    assert lib.arg_of({"arguments": "不是 JSON"}, "agent_id") == ""


def _stub_client(status: int = 200, *, refuse: bool = False) -> lib.Client:
    cfg = lib.Config(base="http://127.0.0.1:8061", home=Path("E:/tmp/realmodel"), provider_id="p")
    client = lib.Client(cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        if refuse:
            raise httpx.ConnectError("没人听", request=request)
        return httpx.Response(status, json={"status": "ok", "database": "connected"})

    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_实例活着判的是应用自己的健康路由() -> None:
    """这套应用上裸 `/health` 是 404，而「404 也算有东西在听」= 端口挂着别的服务器
    时装置自认在线，随后每个调用都 404 崩栈。活着与否只认 `/api/v1/health`。"""
    cfg = lib.Config(base="http://127.0.0.1:8061", home=Path("E:/tmp/realmodel"), provider_id="p")
    assert cfg.health_url == "http://127.0.0.1:8061/api/v1/health"
    assert _stub_client(200).reachable() is True
    assert _stub_client(404).reachable() is False
    assert _stub_client(refuse=True).reachable() is False


def test_配置只从环境读不落仓库() -> None:
    cfg = lib.Config(base="http://127.0.0.1:8061", home=Path("E:/tmp/realmodel"), provider_id="x")
    assert cfg.api == "http://127.0.0.1:8061/api/v1"
    assert str(cfg.workspace).endswith("ws-v119")
    assert lib.config_from_env().base.startswith("http://127.0.0.1")


# ---------------------------------------------------------------- 4 夹具真造 GBK


def test_钩子夹具写的是gbk字节且产品解码能还原() -> None:
    proc = subprocess.run(
        [sys.executable, str(DEVICE / "hooks" / "post_tool_use.py")],
        input=json.dumps({"tool": "read_file"}).encode("utf-8"),
        capture_output=True, cwd=str(DEVICE),
    )
    assert proc.returncode == 2, proc.stderr[:200]
    raw = proc.stderr
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8", "strict")
    assert raw.decode("gbk", "strict") == WAKE_TEXT
    from blank_api.services.hooks import _decode
    assert _decode(raw) == WAKE_TEXT
    # 反证：老写法就是 #119 那次真缺陷的形状——静默少字，一点动静都没有
    assert len(raw.decode("utf-8", "ignore")) < len(WAKE_TEXT)


def test_无关工具的钩子载荷不该唤醒() -> None:
    proc = subprocess.run(
        [sys.executable, str(DEVICE / "hooks" / "post_tool_use.py")],
        input=json.dumps({"tool": "write_file"}).encode("utf-8"),
        capture_output=True, cwd=str(DEVICE),
    )
    assert proc.returncode == 0 and proc.stderr == b""


def test_worktree夹具真产出独有文件的副本(tmp_path: Path) -> None:
    home = tmp_path / "home"
    src = tmp_path / "ws"
    src.mkdir()
    (src / "note.txt").write_text("marker", encoding="utf-8")
    env = {**os.environ, "BLANK_REALMODEL_HOME": str(home).replace("\\", "/")}

    proc = subprocess.run(
        [sys.executable, str(DEVICE / "hooks" / "worktree_create.py")],
        input=json.dumps({"workspace": str(src).replace("\\", "/")}),
        capture_output=True, text=True, encoding="utf-8", cwd=str(DEVICE), env=env,
    )
    assert proc.returncode == 0, proc.stderr[:300]
    dest = Path(json.loads(proc.stdout)["path"])
    assert str(home.resolve())[:8].lower() in str(dest).lower()
    assert (dest / "FROM-HOOK.txt").read_text(encoding="utf-8").strip() == "created by worktree_create hook"
    assert (src / "FROM-HOOK.txt").exists() is False, "独有文件只能长在副本里，主工作区不该有"

    removed = subprocess.run(
        [sys.executable, str(DEVICE / "hooks" / "worktree_remove.py")],
        input=json.dumps({"path": str(dest), "mode": "hook"}),
        capture_output=True, text=True, encoding="utf-8", cwd=str(DEVICE), env=env,
    )
    assert removed.returncode == 0, removed.stderr[:300]
    lines = (home / "hook-events.log").read_text(encoding="utf-8").splitlines()
    # 日志里是 JSON，反斜杠被转义过——按字段比，别按原始行子串比（那是装置自己的读法 bug）
    recorded = [json.loads(line) for line in lines]
    assert any(row.get("event") == "worktree_remove" and Path(row["path"]) == dest
               for row in recorded), recorded


# ---------------------------------------------------------------- 5 与产品同源


def test_包装语常量不许与产品文案漂移() -> None:
    src = (REPO / "services/api/src/blank_api/services/agent/graph.py").read_text(encoding="utf-8")
    assert lib.WAKE_WRAPPER in src, "graph.py 改了唤醒文案，装置的 floor 判据就要同步改"
    assert len(lib.WAKE_WRAPPER) == 14, "包装语长度变了 = 编码格 floor 的算法要重算"


def test_驱动里每条判据函数都还在被调用() -> None:
    driver = (DEVICE / "async_hooks_v119.py").read_text(encoding="utf-8")
    for fn in ("judge_wakeup", "judge_worktree_hook", "judge_subagent", "judge_rewind",
               "judge_encoding", "judge_control"):
        assert f"J.{fn}(" in driver, f"驱动里没再调 {fn}，这条判据已下线，请把用例一起删掉"


# ---------------------------------------------------------------- 6 无凭据


def test_装置目录里不许有凭据或密钥文件() -> None:
    banned_name = {"settings.json", "secret.key", "run.db", ".env", "id_rsa"}
    banned_words = ("sk-", "api_key=", "Bearer ", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY=")
    files = [p for p in DEVICE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files, "装置目录空了"
    for path in files:
        assert path.name not in banned_name, path
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in banned_words:
            assert word not in text, f"{path.name} 里出现了 {word}"
    launch = (DEVICE / "launch-instance.sh").read_text(encoding="utf-8")
    assert "BLANK_SECRET_KEY_FILE" in launch, "启动器必须只给密钥文件路径，不给内容"


# ---------------------------------------------------------------- 7 姿态：花钱之前先钉死

VENV_PY = REPO / "services/api/.venv/Scripts/python.exe"
HOOK_POSTURE = {"port": "8061", "posture": "hooks"}
PLAIN_POSTURE = {"port": "8061", "posture": "no-hooks"}
# 上一版启动器实际交出去的那串：`printf` 手拼，命令里的引号没转义
BROKEN_RAW = '{"post_tool_use":[{"command":""x" "hooks/post_tool_use.py"","async":true}]}'


def _hooks_raw() -> str:
    import hooks_config as HC
    return HC.hooks_json(str(VENV_PY), DEVICE / "hooks")


def test_启动器生成的钩子配置过产品自己的解析器() -> None:
    """喂给**产品解析器**，不喂给装置自己的读法：`HookRunner.from_env` 解析失败
    一律降级成「没有钩子」，所以配置坏掉时不会有任何地方报错，只有三格白红。"""
    import hooks_config as HC
    from blank_api.services.hooks import HookRunner

    raw = _hooks_raw()
    runner = HookRunner.from_env(raw)
    assert runner.enabled, f"产品把这串读成了没有钩子：{raw[:160]}"
    assert set(runner.hooks) == {"post_tool_use", "worktree_create", "worktree_remove"}
    assert [s.async_run for s in runner.hooks["post_tool_use"]] == [True]
    assert all(not s.async_run for event in ("worktree_create", "worktree_remove")
               for s in runner.hooks[event])
    for specs in runner.hooks.values():
        tokens = shlex.split(specs[0].command.replace("\\", "/"))
        assert len(tokens) == 2, tokens
        assert Path(tokens[1]).is_file(), tokens
    assert HC.check(raw, str(VENV_PY), DEVICE / "hooks") == []


def test_手拼的坏JSON只能读成没有钩子() -> None:
    """负基线：#120 第一次真跑交出去的就是这串。产品 fail-closed 读到「没有钩子」，
    钩子判据全红——红得对，但那 11 条测的是姿态。姿态必须由装置钉住。"""
    from blank_api.services.hooks import HookRunner
    import hooks_config as HC

    assert HookRunner.from_env(BROKEN_RAW).enabled is False
    assert HC.check(BROKEN_RAW, "x", DEVICE / "hooks"), "坏配置必须被启动器自检拦下"


def _targets(posture, cells: list[str]) -> dict[str, lib.Target]:
    """一台实例的跑法：三格都指全局那台，姿态就是那一份留痕。

    #121 把闸门改成按**每格自己那台**的留痕判定，旧签名（一个 posture 打三格）
    在这里保留成"三格同机"这一特例——用例本身判的还是姿态，没有顺手放宽。
    """
    return {cell: lib.Target(base=f"http://127.0.0.1:{posture['port'] if posture else '8061'}",
                             port=posture["port"] if posture else "8061", posture=posture)
            for cell in cells}


@pytest.mark.parametrize("cells,reuse,posture,pass_", [
    # 主格 + 编码格真跑：只有 hooks 姿态放行
    (["main", "encoding"], {"main": "", "encoding": ""}, HOOK_POSTURE, True),
    (["main", "encoding"], {"main": "", "encoding": ""}, None, False),
    (["main", "encoding"], {"main": "", "encoding": ""}, PLAIN_POSTURE, False),
    # 三格都想真跑在一台上：一台实例给不出两种姿态，必须拦
    (["main", "encoding", "control"], {"main": "", "encoding": "", "control": ""}, HOOK_POSTURE, False),
    (["main", "encoding", "control"], {"main": "", "encoding": "", "control": ""}, PLAIN_POSTURE, False),
    # 对照格复用早前无钩子那一次：钩子姿态下也放行
    (["main", "encoding", "control"], {"main": "", "encoding": "", "control": "5deb14b2"}, HOOK_POSTURE, True),
    (["control"], {"control": ""}, PLAIN_POSTURE, True),
    (["control"], {"control": ""}, HOOK_POSTURE, False),
    # 全复用不花钱，姿态无从核对也不拦
    (["main", "encoding", "control"], {"main": "a", "encoding": "b", "control": "c"}, None, True),
], ids=["钩子姿态放行", "留痕缺席要拦", "无钩子姿态要拦", "一台实例给不出两种姿态",
        "无钩子姿态同样拦三格", "对照格复用放行", "对照格真跑无钩子放行",
        "对照格真跑不许带钩子", "全复用不花钱就不拦"])
def test_花钱之前的姿态闸门(cells: list[str], reuse: dict[str, str], posture, pass_: bool) -> None:
    err = lib.premise_error(cells=cells, reuse=reuse, targets=_targets(posture, cells))
    assert bool(err) is not pass_, err
    if not pass_:
        assert lib.decide_exit_code(abort=err) == lib.EXIT_INCONCLUSIVE


# ---------------------------------------------------------------- 8 闸门与各格读同一套变量名

def test_复用任务id的变量名在闸门与各格之间不许分叉(monkeypatch) -> None:
    """上一版有两套名字：姿态闸门按格子名拼 `RM_TASK_CONTROL`，各格自己读
    `RM_TASK_CTL`。于是「复用对照格、只花主格+编码格的钱」这一下合法跑法被闸门读成
    「要新跑对照格」，直接 rc 2 作废——拦的方向没错（没白花钱），但装置自己把跑法堵死了。
    反证：把 `lib.CELLS["control"]["task"]` 改回 RM_TASK_CONTROL，这一条立刻红。"""
    import async_hooks_v119 as DRV

    monkeypatch.setenv("RM_TASK_CTL", "5deb14b2")
    assert DRV.reused("control") == "5deb14b2"
    reuse = DRV.reuse_map()
    assert reuse["control"] == "5deb14b2", reuse
    assert lib.premise_error(cells=["main", "encoding", "control"],
                             reuse=reuse, targets=_targets(HOOK_POSTURE, ["main", "encoding", "control"])) == ""

    monkeypatch.delenv("RM_TASK_CTL")
    assert lib.premise_error(cells=["main", "encoding", "control"],
                             reuse=lib.reuse_map(),
                             targets=_targets(HOOK_POSTURE, ["main", "encoding", "control"])), \
        "真要把对照格在同一台上跑一遍时，闸门必须拦住"


def test_格子名写错时装置自己先失效(monkeypatch, capsys) -> None:
    """`RM_CELL=ctl` 这种拼错不能静默少跑一格：那一格的判据压根不进读数表，
    剩下的看着全绿，其实是没判。"""
    import async_hooks_v119 as DRV

    monkeypatch.setenv("RM_CELL", "ctl")
    assert DRV.main() == lib.EXIT_INCONCLUSIVE
    assert "ctl" in capsys.readouterr().out
