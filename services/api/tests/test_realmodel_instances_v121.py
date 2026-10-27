"""#121 装置的一格一实例：对照格必须能落在**另一台没配钩子的实例**上与主格同刻真跑。

上一批（#120）把装置搬进仓库，但装置只认识一台实例：`BLANK_REALMODEL_BASE` 一个值
管三格。后果写在该批文档 §12 里——「对照格本轮是**复用**早前无钩子那一次的读数，
不是与主格同一时刻双实例并跑」。复用不是错，但它让"两种姿态各起一台"这句话
永远只能引用旧读数，而闸门还会把三格同跑一律拦死。

这一批把「格子」与「实例」解绑，逐条钉住：

1. 变量名只有一处定义（`lib.CELLS`）——任务 id、base、home 三样都从这张表拿，
   闸门与各格读的是同一份（#120 就是栽在两套名字上，电池 M20 钉住过任务 id，
   这一批把 base 也纳进同一张表）；
2. 姿态按**每格自己那台**的留痕核对：主格那台要 hooks，对照格那台要没有 hooks；
3. 同一台既要领钩子格又要领对照格时才拦，且文案要点名「用 RM_BASE_CTL 起第二台」，
   不许再退化成"今天没法同刻并跑"；
4. 装置自己得能证明它真的把对照格打到了另一台——离线假客户端记录每格用的 base
   与工作区落在哪个 home，指错了就红，不用花真模型的钱来验路由。

夹具里 `*-v119/-v120` 那几份是上一批的旧实物，本文件的路由用例继续复用它们；
§4 另吃 2026-10-27 双实例同刻并跑取回的新实物（`*-v121`），那是这一批唯一的线上证据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEVICE = REPO / "evals" / "realmodel"
FIXTURES = DEVICE / "fixtures"
sys.path.insert(0, str(DEVICE))

import async_hooks_v119 as DRV  # noqa: E402
import judgments as J  # noqa: E402
import lib  # noqa: E402

# 夹具是 {"task_id", "events"} 那一份原样读数，判据吃的是里面那个事件数组
CONTROL_EVENTS = json.loads((FIXTURES / "events-control-v119.json").read_text(encoding="utf-8"))["events"]

HOOK = {"port": "8061", "posture": "hooks"}
PLAIN = {"port": "8063", "posture": "no-hooks"}
# 「主格那台没钩子」这一条要的是**另一台也没有钩子**的实例：如果和对照格共用一个端口，
# 先撞上的是"一台给不出两种姿态"那条冲突，测的就不是钩子格这一支了。
PLAIN_ON_MAIN = {"port": "8061", "posture": "no-hooks"}
# 反过来的那一台：留痕写着 hooks 却摆在对照格的端口上（配错第二台是最可能的失误）
HOOK_ON_CTL_PORT = {"port": "8063", "posture": "hooks"}
ALL = ["main", "encoding", "control"]


def _one(posture: dict | None, cells: list[str] = ALL) -> dict[str, lib.Target]:
    """三格同为一台（上一批唯一的形状）。"""
    port = (posture or HOOK)["port"]
    return {c: lib.Target(base=f"http://127.0.0.1:{port}", port=port, posture=posture)
            for c in cells}


def _two(hooked: dict | None, plain: dict | None) -> dict[str, lib.Target]:
    """主格/编码格一台、对照格另一台——这一批要能表达的形状。"""
    out = {c: lib.Target(base=f"http://127.0.0.1:{(hooked or HOOK)['port']}",
                         port=(hooked or HOOK)["port"], posture=hooked) for c in ("main", "encoding")}
    out["control"] = lib.Target(base=f"http://127.0.0.1:{(plain or PLAIN)['port']}",
                                port=(plain or PLAIN)["port"], posture=plain)
    return out


NO_SPEND = {"main": "a", "encoding": "b", "control": "c"}
FRESH_ALL = {"main": "", "encoding": "", "control": ""}


# ---------------------------------------------------------------- 1 变量名只有一处定义

def test_任务id与实例的变量名同出一张表() -> None:
    """`DRV.TASK_ENV` 是 `lib.CELLS` 的投影，不是第二份登记。

    反证（电池 M20）：把 `lib.CELLS["control"]["task"]` 改成 RM_TASK_CONTROL，
    这里与「闸门与各格不许分叉」那条一起红。
    """
    assert DRV.TASK_ENV == {c: s["task"] for c, s in lib.CELLS.items()}
    assert DRV.reuse_map() == lib.reuse_map()
    # 整张表钉死：变量名一旦有人另起一套，这一条先红
    assert lib.CELLS == {
        "main": {"task": "RM_TASK_MAIN", "base": "RM_BASE_MAIN", "home": "RM_HOME_MAIN"},
        "encoding": {"task": "RM_TASK_ENC", "base": "RM_BASE_ENC", "home": "RM_HOME_ENC"},
        "control": {"task": "RM_TASK_CTL", "base": "RM_BASE_CTL", "home": "RM_HOME_CTL"},
    }, lib.CELLS
    assert len({s["task"] for s in lib.CELLS.values()}) == 3
    assert len({s["base"] for s in lib.CELLS.values()}) == 3
    assert len({s["home"] for s in lib.CELLS.values()}) == 3


def test_没给每格的env就回落到全局那一台(monkeypatch, tmp_path) -> None:
    for key in ("RM_BASE_CTL", "RM_HOME_CTL", "RM_BASE_MAIN", "RM_HOME_MAIN",
                "RM_BASE_ENC", "RM_HOME_ENC"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BLANK_REALMODEL_BASE", "http://127.0.0.1:8061")
    monkeypatch.setenv("BLANK_REALMODEL_HOME", str(tmp_path))
    for cell in ALL:
        cfg = lib.cell_config(cell)
        assert cfg.base == "http://127.0.0.1:8061", cell
        assert cfg.home == tmp_path, cell


def test_对照格指到另一台时只有它自己变(monkeypatch, tmp_path) -> None:
    ctl_home = tmp_path / "ctl"
    monkeypatch.setenv("BLANK_REALMODEL_BASE", "http://127.0.0.1:8061")
    monkeypatch.setenv("BLANK_REALMODEL_HOME", str(tmp_path))
    monkeypatch.setenv("RM_BASE_CTL", "http://127.0.0.1:8063/")
    monkeypatch.setenv("RM_HOME_CTL", str(ctl_home))
    assert lib.cell_config("control").base == "http://127.0.0.1:8063", "尾斜杠要收掉，否则留痕文件名对不上"
    assert lib.cell_config("control").home == ctl_home
    assert lib.cell_config("control").port == "8063"
    assert lib.cell_config("main").base == "http://127.0.0.1:8061"
    assert lib.cell_config("main").port == "8061"
    # 留痕按端口分文件：两台实例各自一份，主格的姿态永远读不到对照格那台
    assert lib.cell_config("control").posture_file.name == "instance-posture-8063.json"
    assert lib.cell_config("main").posture_file.name == "instance-posture-8061.json"
    assert lib.cell_config("control").workspace == ctl_home / "ws-v119"


# ---------------------------------------------------------------- 2 姿态按每格那台核

@pytest.mark.parametrize("cells,reuse,targets,pass_,needle", [
    # 这一条是整批的理由：上一批它只能靠"复用"过关，现在同刻并跑放行
    (ALL, FRESH_ALL, _two(HOOK, PLAIN), True, ""),
    # 对照格那台的留痕其实是 hooks：那条该红的判据红不了，拦
    (ALL, FRESH_ALL, _two(HOOK, HOOK_ON_CTL_PORT), False, "8063"),
    # 主格那台没钩子：钩子判据必红，白花钱，拦
    (ALL, FRESH_ALL, _two(PLAIN_ON_MAIN, PLAIN), False, "钩子判据"),
    # 主格那台留痕不在（手起的）：姿态未知，拦
    (ALL, FRESH_ALL, _two(None, PLAIN), False, "留痕文件不在"),
    # 对照格那台留痕不在：这一格要真跑，姿态未知同样拦
    (ALL, FRESH_ALL, _two(HOOK, None), False, "留痕文件不在"),
    # 三格挤在一台 hooks 实例上：还是拦，但要点名第二台怎么起
    (ALL, FRESH_ALL, _one(HOOK), False, "RM_BASE_CTL"),
    # 三格挤在一台无钩子实例上：先撞"一台给不出两种姿态"，同样要点名第二台
    (ALL, FRESH_ALL, _one(PLAIN), False, "RM_BASE_CTL"),
    # 复用格不核对姿态（读的是旧事件，姿态归旧那轮）
    (ALL, NO_SPEND, _two(None, None), True, ""),
    (["main", "encoding"], {"main": "", "encoding": "", "control": "c"}, _two(HOOK, None), True, ""),
    # 只跑对照格：它自己那台没有钩子就放行
    (["control"], {"control": ""}, _two(HOOK, PLAIN), True, ""),
    (["control"], {"control": ""}, _one(HOOK), False, "no-hooks"),
    # 留痕文件坏了（读到怪值）：对照格这格姿态不明就不许花这笔钱
    (["control"], {"control": ""}, _one({"port": "8063", "posture": "坏掉的留痕文件"}), False, "留痕读到姿态"),
], ids=["同刻双实例放行", "对照那台其实带钩子", "主格那台没钩子要拦",
        "主格那台没留痕要拦", "对照那台没留痕要拦", "挤在钩子一台要点名第二台",
        "挤在无钩子一台也要点名第二台", "全复用不核姿态", "复用对照格不核它那台",
        "单跑对照格无钩子放行", "单跑对照格带钩子要拦", "坏留痕不许当无钩子"])
def test_花钱之前按每格自己那台的留痕判定(cells, reuse, targets, pass_, needle: str) -> None:
    err = lib.premise_error(cells=cells, reuse=reuse, targets=targets)
    assert bool(err) is not pass_, err
    if not pass_:
        assert lib.decide_exit_code(abort=err) == lib.EXIT_INCONCLUSIVE
        assert needle in err, f"拦下了却没念出成因：{err}"


def test_拦的时候要念出是哪一台和怎么办() -> None:
    """成因里必须带 base 与那条 env 名：只说"姿态不对"的话，操作的人得猜。"""
    err = lib.premise_error(cells=ALL, reuse=FRESH_ALL, targets=_one(HOOK))
    assert "http://127.0.0.1:8061" in err, err
    assert "RM_BASE_CTL" in err and "launch-instance.sh" in err, err
    assert "两种姿态" in err, err


# ---------------------------------------------------------------- 3 装置真的分两台打

class FakeClient:
    """离线假客户端：记录每格把请求打到哪台实例，不发出一个字节。"""

    seen: list[tuple[str, str]] = []
    workspaces: list[str] = []

    def __init__(self, cfg: lib.Config) -> None:
        self.cfg = cfg

    def reachable(self) -> bool:
        return True

    def post(self, path: str, payload: dict | None = None) -> dict:
        FakeClient.seen.append((self.cfg.base, path))
        if "workspaces" in path:
            FakeClient.workspaces.append(str((payload or {}).get("path")))
        return {"id": "gen-1"}

    def get(self, path: str):
        FakeClient.seen.append((self.cfg.base, path))
        if path.endswith("/evidence"):
            return {}
        if "/events" in path:
            return CONTROL_EVENTS
        return {"status": "succeeded"}

    def events(self, task_id: str) -> list[dict]:
        return CONTROL_EVENTS

    def wait_terminal(self, task_id: str, budget: float) -> str:
        return "succeeded"


@pytest.fixture()
def two_instances(monkeypatch, tmp_path):
    """真的落两份留痕文件：装置读的是磁盘上的自证，不是参数里的口头姿态。"""
    main_home, ctl_home = tmp_path / "main", tmp_path / "ctl"
    for home, port, posture in ((main_home, "8061", "hooks"), (ctl_home, "8063", "no-hooks")):
        home.mkdir(parents=True)
        (home / f"instance-posture-{port}.json").write_text(
            json.dumps({"port": port, "posture": posture, "hook_settle": "2",
                        "started_at": "2026-10-27T00:00:00Z"}), encoding="utf-8")
    monkeypatch.setenv("BLANK_REALMODEL_BASE", "http://127.0.0.1:8061")
    monkeypatch.setenv("BLANK_REALMODEL_HOME", str(main_home))
    monkeypatch.setenv("BLANK_REALMODEL_PROVIDER", "provider-under-test")
    monkeypatch.setenv("RM_BASE_CTL", "http://127.0.0.1:8063")
    monkeypatch.setenv("RM_HOME_CTL", str(ctl_home))
    monkeypatch.setenv("RM_TASK_MAIN", "reuse-main")
    monkeypatch.setenv("RM_TASK_ENC", "reuse-enc")
    monkeypatch.delenv("RM_TASK_CTL", raising=False)
    monkeypatch.setattr(lib, "Client", FakeClient)
    FakeClient.seen, FakeClient.workspaces = [], []
    return {"main": main_home, "ctl": ctl_home}


def test_对照格的请求真的打到另一台(two_instances, monkeypatch) -> None:
    monkeypatch.setenv("RM_CELL", "control")
    assert DRV.main() == lib.EXIT_OK, "对照格真跑在无钩子那台：该红的那条红了就是绿"
    bases = {base for base, _ in FakeClient.seen}
    assert bases == {"http://127.0.0.1:8063"}, f"对照格打到了 {bases}"


def test_三格各打各的实例(two_instances, monkeypatch) -> None:
    """全跑（主格/编码格复用、对照格真跑）时，每格的取数都落在自己那台。"""
    monkeypatch.delenv("RM_TASK_CTL", raising=False)
    DRV.main()
    per_base = {}
    for base, path in FakeClient.seen:
        per_base.setdefault(base, set()).add(path.split("/")[2] if path.count("/") > 1 else path)
    assert "http://127.0.0.1:8061" in per_base and "http://127.0.0.1:8063" in per_base, per_base
    # 对照格的工作区铺在它自己那台的 home 下，不跟主格抢同一份 run.db
    assert all(p.startswith(str(two_instances["ctl"])) for p in FakeClient.workspaces), \
        FakeClient.workspaces


def test_指错实例时闸门先拦下来不花钱(two_instances, monkeypatch) -> None:
    """把对照格指回主格那台（带钩子）：闸门必须 rc 2，一台客户端请求都不许发。"""
    monkeypatch.setenv("RM_BASE_CTL", "http://127.0.0.1:8061")
    assert DRV.main() == lib.EXIT_INCONCLUSIVE
    assert FakeClient.seen == [], "闸门拦住了，装置却已经发过请求"


def test_留痕文件坏了照样拦(two_instances) -> None:
    bad = two_instances["ctl"] / "instance-posture-8063.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    assert DRV.main() == lib.EXIT_INCONCLUSIVE
    assert FakeClient.seen == []


def test_读数要自报每格的来源(two_instances, capsys) -> None:
    """复用格念「旧读数，不是此刻的实物」，真跑格念它落在哪台：
    「同刻双实例并跑」这句话只能由这一行证据支持，不能由文档声称。"""
    DRV.main()
    out = capsys.readouterr().out
    assert "main 格复用" in out and "旧读数" in out, out
    assert "control 格此刻真跑 @ http://127.0.0.1:8063" in out, out


# ------------------------------------------------- 4 双实例同刻并跑那轮的实物重放

# 2026-10-27 08:45 那一轮：主格+编码格在 :8061（留痕 hooks），对照格在 :8065（留痕
# no-hooks），三格都是当刻真跑、没有一格靠复用。装置当场报「红 1 / 判据 29 … RC=0」。
# 下面三份 `*-v121` 就是那一轮从两台实例原样取回的实物；文档里「同刻并跑」这句话
# 的证据是这些字节，重放它们必须复现同一副红绿——红的那条只能是对照格那一条。
MAIN_121 = json.loads((FIXTURES / "events-main-v121.json").read_text(encoding="utf-8"))
EVIDENCE_121 = json.dumps(json.loads((FIXTURES / "evidence-main-v121.json").read_text(
    encoding="utf-8")), ensure_ascii=False)
HOOK_LOG_121 = (FIXTURES / "hook-log-main-v121.txt").read_text(encoding="utf-8").splitlines()
CONTROL_121 = json.loads((FIXTURES / "events-control-v121.json").read_text(encoding="utf-8"))
ENCODING_121 = json.loads((FIXTURES / "events-encoding-v121.json").read_text(encoding="utf-8"))
WAKE_TEXT = (FIXTURES / "wake-text.txt").read_text(encoding="utf-8").rstrip("\r\n")


def test_主格那台的实物重放一条不红() -> None:
    """16 条主格判据在双实例那一轮的实物上复现全绿——绿的一半证据。"""
    rec = lib.Recorder()
    J.judge_wakeup(rec, MAIN_121)
    J.judge_worktree_hook(rec, MAIN_121, HOOK_LOG_121)
    J.judge_subagent(rec, MAIN_121, EVIDENCE_121)
    assert len(rec.reads) == 16, rec.reads
    assert rec.fails == [], rec.fails


def test_对照格那台的实物只红该红的那条() -> None:
    rec = lib.Recorder()
    assert J.judge_control(rec, CONTROL_121["events"]) is True
    assert rec.fails == [J.CONTROL_RED_NAME], rec.reads


def test_对照格的副本落在另一台的home而不是主格那台() -> None:
    """路由对不对，看的是副本路径的**前缀**：它得在对照格那台的 home 下。

    主格与对照格同刻跑时共用一个仓库，只有 home 分开才证明请求真打到了另一台；
    若哪天有人把 `RM_HOME_CTL` 漏了（两格挤同一份 run.db），这条先红。
    """
    created = lib.of_type(CONTROL_121["events"], "worktree.created")
    paths = [str(e["payload"].get("path") or "") for e in created]
    assert paths, CONTROL_121["events"]
    assert all(p.replace("\\", "/").startswith("E:/tmp/realmodel-ctl/") for p in paths), paths
    assert not any("realmodel-ctl" not in p and p.replace("\\", "/").startswith("E:/tmp/realmodel/")
                   for p in paths), paths


def test_同刻并跑的编码格实物复现绿基线() -> None:
    rec = lib.Recorder()
    J.judge_encoding(rec, ENCODING_121["events"], WAKE_TEXT)
    assert rec.fails == [], rec.reads


def test_并跑那轮的读数自证是两台实例() -> None:
    """两份实物各自盖上当刻的 base：同一台给不出两种姿态，读数里就得看得见是两个端口。"""
    assert CONTROL_121["base"] == "http://127.0.0.1:8065", CONTROL_121["base"]
    assert ENCODING_121["base"] == "http://127.0.0.1:8061", ENCODING_121["base"]
    assert CONTROL_121["task_id"] != ENCODING_121["task_id"], "两台实例得是两个不同的任务"
    assert lib.of_type(ENCODING_121["events"], "hook.wakeup_delivered"), "编码格那台得是钩子姿态"
    assert not lib.of_type(CONTROL_121["events"], "hook.wakeup_delivered")
