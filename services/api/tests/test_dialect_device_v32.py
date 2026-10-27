"""方言端到端装置自己的闸门（`evals/dialect_e2e.py`）。

这套装置负责"只有 MySQL 红 = 真方言分歧 / 两边一起红 = 装置错"这条判定，
所以它自己的两处口径必须有失败能力：

1. 哪一类 502 算脚手架抖动（可以换任务重来）、哪一类算产品缺陷（必须照红）；
   这条判据一旦写宽，产品缺陷就会被"重试"洗绿——本文件用真缺陷的原话当反例钉住它。
2. 操作者给的 MySQL 连接串怎么解析、什么样的库名会被拒（库名要拼进 DDL，标识符不能参数化）。
3. 红了以后退出码交回什么：判据要求每一格都绿，对照格红必须让整次判定作废（`verdict()`）。

另外数一遍步骤清单：装置被改短时，"全绿"这句话就不成立了（`EXPECTED_STEPS` 由装置自己声明，
`Drive.finish` 与这里各拿它对账一次）。#52 加进来的审批/答题两格还把前提压在了桩上——
"这一轮抛的是高风险工具"这件事也得能被钉住，否则那一格会悄悄退化成只读格。
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

DEVICE = Path(__file__).resolve().parents[3] / "evals" / "dialect_e2e.py"


def _load_device():
    spec = importlib.util.spec_from_file_location("dialect_e2e", DEVICE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


device = _load_device()

REQUEST = httpx.Request("POST", "http://127.0.0.1:8071/api/v1/tasks/any/run")


def _response(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=REQUEST)


FLAKE_BODY = '{"error":{"code":"model_unreachable","message":"模型服务不可达：stub（ReadError）"}}'
MISSING_GREENLET_BODY = "sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called"


class _FakeHTTP:
    """按 `VERB 路径` 查表的假传输层：只记次数，不碰网络。"""

    def __init__(self, calls: list[str], table: dict[str, httpx.Response]) -> None:
        self.calls = calls
        self.table = table

    def _hit(self, verb: str, url: str) -> httpx.Response:
        key = f"{verb} {url}"
        if key not in self.table:
            raise AssertionError(f"装置没准备这一步的响应：{key}")
        self.calls.append(key)
        return self.table[key]

    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> httpx.Response:
        return self._hit("POST", url)

    def get(self, url: str, timeout: float | None = None) -> httpx.Response:
        return self._hit("GET", url)


def _fake_drive(calls: list[str], table: dict[str, httpx.Response]):
    drive = device.Drive("http://127.0.0.1:1/api/v1")
    drive.c = _FakeHTTP(calls, table)  # type: ignore[assignment]
    drive.state["task"] = "old"
    drive.state["provider"] = "prov"
    return drive


def test_a_flaky_model_connection_is_recognised() -> None:
    """这一条守的是"抖动可重来"：#50 两边同跑时第 19 步撞上它，单独跑四回合全绿。"""
    flaky = _response(
        502, '{"error":{"code":"model_unreachable","message":"模型服务不可达：stub（ReadError）"}}'
    )
    assert device._is_transport_flake(flaky) is True
    for marker in ("ConnectError", "ConnectTimeout", "PoolTimeout", "RemoteProtocolError"):
        assert device._is_transport_flake(
            _response(502, f'{{"error":{{"message":"模型服务不可达：stub（{marker}）"}}}}')
        ) is True, marker
    # 503 也算通道侧症状；404/200 不是。
    assert device._is_transport_flake(_response(503, "ReadError")) is True
    assert device._is_transport_flake(_response(200, "ReadError")) is False


def test_real_defects_are_not_exempted_by_the_retry() -> None:
    """判据写宽的最坏后果是"真缺陷被重试洗绿"——用三句真实缺陷原话钉住它必须照红。"""
    for body in (
        # 缺陷 C：aiomysql 在 PyMySQL ≥1.2 上绑不了 bytes
        'TypeError: \'str\' object is not callable（ReadError 字样也不许单独放行）',
        # 提交后读回缺失
        'sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called',
        # 换库时的字面量默认值
        'pymysql.err.OperationalError: (1101, "BLOB/TEXT column ... default")',
    ):
        resp = _response(500, body)
        assert device._is_transport_flake(resp) is False, body
    # 状态码对了但错误码不是"不可达"，也不能算抖动：只有 502/503 才有这个资格。
    assert device._is_transport_flake(_response(502, '{"error":{"code":"provider_not_found"}}')) is False


def test_the_flake_retry_actually_replays_on_a_fresh_task() -> None:
    """抖动那条分支要真走得通：第一次撞抖动 → 换一条全新任务 → 状态指到新任务。

    只测判据不测这条流程，等于没测——上一批就是靠这类"分支根本没走到却报绿"的靶翻过车。
    """
    calls: list[str] = []
    drive = _fake_drive(
        calls,
        {
            "POST /tasks/old/run": _response(502, FLAKE_BODY),
            "POST /projects/p/tasks": _response(201, '{"id":"new-task"}'),
            "POST /tasks/new-task/run": _response(200, '{"ok":true}'),
            "GET /tasks/new-task": _response(200, '{"status":"succeeded"}'),
        },
    )
    detail = device.run_task_with_flake_guard(drive, "p")
    assert "重试 1 次" in detail, detail
    assert drive.state["task"] == "new-task", "后续步骤必须跟着换到新任务，否则量的是死掉的那一条"
    assert calls == [
        "POST /tasks/old/run",
        "POST /projects/p/tasks",
        "POST /tasks/new-task/run",
        "GET /tasks/new-task",
    ], calls


def test_a_real_failure_on_the_run_is_not_retried_away() -> None:
    """真缺陷（500）不许走重试：直接抛，且一次都不许多跑。"""
    calls: list[str] = []
    drive = _fake_drive(calls, {"POST /tasks/old/run": _response(500, MISSING_GREENLET_BODY)})
    # 只断言"抛了 AssertionError"抓不住撤掉 500 闸门的变异：那条会继续往下走、
    # 在别处抛另一个 AssertionError 照样通过。所以钉住失败原因。
    with pytest.raises(AssertionError, match=r"HTTP 500"):
        device.run_task_with_flake_guard(drive, "p")
    assert calls == ["POST /tasks/old/run"], calls


def test_the_drive_still_registers_every_step_once() -> None:
    """步骤清单本身是判定的一部分：少了步骤，"全绿"这句话就是假的。"""

    class Recorder:
        def __init__(self) -> None:
            self.rows: list[str] = []
            self.state: dict[str, str] = {}

        def step(self, name: str, fn) -> None:  # 只登记，不执行
            self.rows.append(name)

    recorder = Recorder()
    device.drive(recorder)  # type: ignore[arg-type]
    assert len(recorder.rows) == device.EXPECTED_STEPS, len(recorder.rows)
    assert len(set(recorder.rows)) == device.EXPECTED_STEPS, "步骤重名了，对照表会互相盖掉"
    joined = " ".join(recorder.rows)
    for must_have in ("19 任务运行到终态", "38 群消息（触发一轮）",
                      "54 高风险写入挂起成审批卡", "61 答题后答案进了上下文",
                      "62 流式空帧要退回整包调用", "63 群聊审批卡挂起",
                      "66 成员驳回后不过期执行、轮能收尾", "67 删除供应商后账本还在",
                      "73 匿名打每一条接口都被拒",
                      "74 接口面不匿名可读", "75 带令牌不应当被鉴权挡住",
                      "76 网关拒 stream 时退回整包并把活干完", "77 两种形状都 429 时不许静默成功",
                      "78 模型太慢要按超时失败",
                      "81 429 退避一次后把活干完", "82 429 的 Retry-After 要算进等待",
                      "83 压缩真发生且摘要那笔 429 听 Retry-After",
                      "84 scope=tool 批准后下一条高风险命令仍要问",
                      "85 只换载荷尾巴要重新挂卡且批准过的那条不再挂卡"):
        assert must_have in joined, must_have


def test_the_mysql_connection_string_is_parsed_and_bounded() -> None:
    """CI 给的是带密码的连接串；库名要拼进 DDL，所以只认字面标识符。"""
    shown, host, port, user, password, database = device._split_mysql_url(
        "mysql+aiomysql://m51:Kbgai51Passw0rd@127.0.0.1:3307/kbgai_e2e"
    )
    assert (host, port, user, password, database) == ("127.0.0.1", 3307, "m51", "Kbgai51Passw0rd", "kbgai_e2e")
    assert "Kbgai51Passw0rd" not in shown, "回显给日志的那份必须遮住口令"

    for bad in (
        "",                                                     # 没给
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db",           # 方言不对
        "mysql+aiomysql://root@127.0.0.1:3306/not!a!db",        # 库名不是字面标识符
        "mysql+aiomysql://root@127.0.0.1:3306/db;drop--",       # 想塞第二条语句
    ):
        with pytest.raises(device.MySQLMissing):
            device._split_mysql_url(bad)


def test_the_postgres_connection_string_is_parsed_and_bounded() -> None:
    """第三格同样要把库名当标识符用；MySQL 的 URL 不能混进来，口令也不许回显。"""
    shown, host, port, user, password, database = device._split_postgres_url(
        "postgresql+asyncpg://pg60:Kbgai60Passw0rd@127.0.0.1:55432/kbgai_e2e"
    )
    assert (host, port, user, password, database) == ("127.0.0.1", 55432, "pg60", "Kbgai60Passw0rd",
                                                      "kbgai_e2e")
    assert "Kbgai60Passw0rd" not in shown

    for bad in (
        "",
        "mysql+aiomysql://root@127.0.0.1:3307/kbgai_e2e",       # 方言不对
        "postgresql+asyncpg://postgres@127.0.0.1:5432/not!a!db",
        "postgresql+asyncpg://postgres@127.0.0.1:5432/db;drop--",
    ):
        with pytest.raises(device.PostgresMissing):
            device._split_postgres_url(bad)


def test_the_verdict_names_the_postgres_cell() -> None:
    """加了第三格以后，归属必须点到 PostgreSQL 头上，不能还写着 MySQL。"""
    rc, lines = device.verdict({"sqlite": {"19": "PASS"}, "postgres": {"19": "FAIL"}})
    assert rc == 1 and any("只有 PostgreSQL 红" in x for x in lines), lines
    # 两个被测格一起红时，不许各说一句"只有 X 红"——那是两句假话。
    rc, lines = device.verdict({"sqlite": {"19": "PASS"}, "mysql": {"20": "FAIL"},
                                "postgres": {"20": "FAIL"}})
    assert rc == 1 and any("非对照格" in x for x in lines), lines
    assert not any("只有 MySQL 红" in x or "只有 PostgreSQL 红" in x for x in lines), lines
    rc, lines = device.verdict({"sqlite": {"19": "PASS"}, "mysql": {"19": "PASS"},
                                "postgres": {"19": "PASS"}})
    assert rc == 0 and any("判据成立" in x for x in lines), lines


def test_the_verdict_fails_loudly_when_any_cell_is_red() -> None:
    """红了该算谁的：判据要求**每格都绿**，任何一格红都不许把退出码交回 0。

    钉住最后一条是实测出来的洞——`E:/tmp/m54/drive.log` 那一次对照格（SQLite）第 19 步红、
    被测格全绿，老代码只在被测格红时置非 0，退出码 0、CI 会绿，
    而那一次连"不做换库的对照"都没过不去，任何换库结论都不成立。
    """
    both_green = {"sqlite": {"s1": "PASS", "s2": "PASS"}, "mysql": {"s1": "PASS", "s2": "PASS"}}
    rc, lines = device.verdict(both_green)
    assert rc == 0 and "判据成立" in " ".join(lines), lines

    only_tested = {"sqlite": {"19": "PASS"}, "mysql": {"19": "FAIL"}}
    rc, lines = device.verdict(only_tested)
    assert rc == 1 and any("只有 MySQL 红" in x for x in lines), lines

    both_red = {"sqlite": {"19": "FAIL"}, "mysql": {"19": "FAIL"}}
    rc, lines = device.verdict(both_red)
    assert rc == 1 and any("装置错" in x for x in lines), lines

    only_control = {"sqlite": {"19": "FAIL"}, "mysql": {"19": "PASS"}}
    rc, lines = device.verdict(only_control)
    assert rc == 1, "对照格红却交回 0：CI 会绿，而这次跑什么也没证明"
    assert any("只有对照格" in x for x in lines), lines

    single_green = {"mysql": {"19": "PASS"}}
    rc, lines = device.verdict(single_green)
    # 对照格（sqlite）不在场 ⇒ 判据连成立的机会都没有：这不是"换库全绿"，是"今天没法判"。
    # 2026-10-26 那一次 26 步红全在 sqlite 格，装置前提一崩就整格没跑——老口径在这里返回 0。
    assert rc == 2 and any("对照格" in x and "缺席" in x for x in lines), lines

    single_red = {"mysql": {"19": "FAIL"}}
    rc, lines = device.verdict(single_red)
    assert rc == 2 and any("对照格" in x for x in lines), lines


def test_the_control_cell_present_but_green_is_reported_as_one_cell() -> None:
    """对照格在场、被测格缺席时才是「只有一格」：这一句和「没法判」是两种诚实。"""
    rc, lines = device.verdict({"sqlite": {"19": "PASS"}})
    assert rc == 0 and any("只有一格" in x for x in lines), lines


def test_the_stub_only_switches_tools_on_the_probe_marker() -> None:
    """桩换工具只认探针标记：这一格的全部前提都在"这一轮抛的是高风险工具"上。

    如果换成按"审批"两个字判断，产品自己的系统提示里那句"高风险需审批"就会让
    每一格都挂审批卡，而那张卡跟这一步要量的东西毫无关系。
    """
    import json as _json

    assert device._next_call("随便读读工作区")["name"] == "list_files"
    assert device._next_call("这一步需要人工审批，请谨慎")["name"] == "list_files"
    approval = device._next_call(f"写点东西 {device.PROBE_APPROVAL}")
    question = device._next_call(f"不确定放哪 {device.PROBE_QUESTION}")
    assert (_json.loads(approval["arguments"]).get("path"), question["name"]) == (
        device.PROBE_PATH, "request_user_input")
    # 两个标记同时在场时必须有一个确定答案，不能靠字典顺序碰运气
    assert device._next_call(f"{device.PROBE_APPROVAL} {device.PROBE_QUESTION}")["name"] == "request_user_input"


def test_the_approval_probe_really_is_a_high_risk_call() -> None:
    """这一格能被跑到，靠的是补丁里那条合成密钥把风险顶到 high（default 模式才会打断）。

    风险口径哪天改了（比如 `contains_secret` 不再升级），这一步会静默退化成"只读格"，
    /approval 依旧没人跑过——所以把前提本身钉住，而不是只钉结论。
    """
    import json as _json

    from blank_api.services.log_safety import contains_secret, redact
    from blank_api.services.tools.patch import patch_tools

    arguments = _json.loads(device._APPROVAL_CALL["arguments"])
    tool = patch_tools()[0]
    assert tool.resolve_risk(arguments) == "high", tool.resolve_risk(arguments)
    assert tool.resolve_risk({"path": "notes/x.md", "patch": "+正常一行"}) == "medium"
    assert contains_secret(arguments["patch"]), "探针补丁没带上「密钥形态」，风险升不上去"
    redacted = redact(arguments["patch"])
    assert "SyntheticNotARealKey0001" not in redacted and "****" in redacted, redacted


def test_the_ttl_survives_the_second_dialect_and_says_no_on_the_way_out() -> None:
    """TTL 要穿过库再算一次：MySQL 的 DATETIME 不带时区，读回来当 UTC 才是那条口径。

    写成 naive 就当本地时间的话，东八区的机器上 5 分钟的卡会当场"已过期"，
    界面给出一个必然 409 的按钮——那正是 #27 收口过的那类分歧，别在新格子里长回来。
    """
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert 4.5 < device._ttl_minutes(future) <= 5.1, device._ttl_minutes(future)
    naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
    assert 4.5 < device._ttl_minutes(naive.isoformat()) <= 5.1, "无时区那份按 UTC 算了没有"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert device._ttl_minutes(past) < 0, "过期的卡必须报负数，不能被当成还能点"


def test_finish_marks_a_shortened_run_red_instead_of_green() -> None:
    """装置被改短（或步数与声明不符）时，"每一格全绿"这句话不能照发。"""
    drive = device.Drive("http://127.0.0.1:1/api/v1")
    drive.rows = [("01 只有一步", "PASS", ""), ("02 还有一步", "PASS", "")]
    fails = drive.finish("sqlite")
    assert fails == 1, drive.rows
    assert drive.rows[-1][0] == "<步骤清单自检>", drive.rows
    rc, lines = device.verdict({"sqlite": {n: s for n, s, _ in drive.rows},
                                "mysql": {"01 只有一步": "PASS"}})
    assert rc == 1 and any("步骤清单自检" in x or "只有对照格" in x for x in lines), lines


# --------------------------------------------------------------------------- #
# 审批/答题那八步自己的判定能不能失败（#52）
# --------------------------------------------------------------------------- #
# 端到端那一跑只证明"这次是绿的"；下面这些离线用例证明的是"坏了会红"。
# 两类各有各的实物，缺哪一类都只剩半句话。

from datetime import datetime, timedelta, timezone  # noqa: E402,F811  （上面那批用例也读得到，放在顶部导入）

REQUEST2 = httpx.Request("POST", "http://127.0.0.1:8071/api/v1/tasks/t/approval")


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else _json_dumps(body)
    return httpx.Response(status, text=text, request=REQUEST2)


def _json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _card(**over) -> dict:
    card = {
        "tool": "apply_patch",
        "arguments": {"path": device.PROBE_PATH, "patch": "+api_key = x"},
        "risk_level": "high",
        "approval_state": "待审批",
        "expired": False,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    card.update(over)
    return card


def _evidence_drive(calls: list[str], evidence: dict, task_body: dict) -> device.Drive:
    drive = _fake_drive(calls, {
        "GET /tasks/t/evidence": _resp(200, evidence),
        "GET /tasks/t": _resp(200, task_body),
    })
    drive.state["task"] = "t"
    drive.state["approval_task"] = "t"  # 58/57 那两步从状态里取的是探针任务，不是通用 task
    return drive


def test_the_approval_card_judgement_can_fail_five_ways() -> None:
    """第 55 步的五条红线各钉一次：卡片缺参数、风险不对、被判过期、TTL 走样、状态不配对。"""
    ok = {"pending_approvals": [_card()], "changes": {}}
    calls: list[str] = []
    detail = device._assert_card_shape(_evidence_drive(calls, ok, {"status": "waiting_approval"}), "t")
    assert "剩余 TTL" in detail, detail

    reds = [
        ({"pending_approvals": [_card(arguments={})]}, "补丁参数"),
        ({"pending_approvals": [_card(risk_level="medium")]}, "风险等级"),
        ({"pending_approvals": [_card(expired=True)]}, "过期"),
        ({"pending_approvals": [_card(expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())]}, "TTL"),
        ({"pending_approvals": [_card()]}, "waiting_approval"),
    ]
    for evidence, reason in reds:
        body = {"status": "running"} if reason == "waiting_approval" else {"status": "waiting_approval"}
        drive = _evidence_drive(calls, evidence, body)
        with pytest.raises(AssertionError, match=reason):
            device._assert_card_shape(drive, "t")


def test_the_secret_claim_is_checked_on_both_ends() -> None:
    """第 58 步两头都要能失败：库里带密钥、事件里带密钥、工作区反倒没密钥，三种都算红。

    只断言"diff 被打了码"的话，`redact` 被撤掉而 diff 整个不落库也能报绿。
    """
    secret = "SyntheticNotARealKey0001"
    green = {"changes": {"diff": "+api_key = \"****\"", "count": 1, "files": [device.PROBE_PATH]},
             "timeline": [{"type": "tool.completed", "payload": {"arguments": {"patch": "+api_key = \"****\""}}}]}
    calls: list[str] = []
    workdir = Path(tempfile.mkdtemp())
    (workdir / device.PROBE_PATH).write_text(f'api_key = "{secret}"\n', encoding="utf-8")
    old_ws = device.WS
    device.WS = workdir  # type: ignore[assignment]
    try:
        drive = _evidence_drive(calls, green, {"status": "succeeded"})
        detail = device._assert_diff_redacted(drive)
        assert "打码" in detail, detail

        for evidence, reason in (
            ({"changes": {"diff": f'+api_key = "{secret}"', "count": 1, "files": [device.PROBE_PATH]},
              "timeline": []}, "还带着明文密钥"),
            ({"changes": {"diff": "+api_key = \"****\"", "count": 1, "files": [device.PROBE_PATH]},
              "timeline": [{"type": "approval.required", "payload": {"arguments": {"patch": f'api_key = "{secret}"'}}}]},
             "没脱敏"),
            ({"changes": {"diff": "", "count": 0, "files": []}, "timeline": []}, "没有 diff"),
        ):
            with pytest.raises(AssertionError, match=reason):
                device._assert_diff_redacted(_evidence_drive(calls, evidence, {"status": "succeeded"}))
        # 工作区那份原件必须还留着密钥：脱敏只能脱库里那份，不能脱到用户要的文件上
        (workdir / device.PROBE_PATH).write_text("api_key = \"****\"\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="工作区"):
            device._assert_diff_redacted(_evidence_drive(calls, green, {"status": "succeeded"}))
    finally:
        device.WS = old_ws  # type: ignore[assignment]


def test_the_resume_guard_accepts_the_paused_stop_and_survives_a_flake() -> None:
    """第 59 步等的那个终态里必须有 `paused`——驳回是当场停轮，不是失败。

    这一条是实测翻出来的：装置第一次跑就在 59 白等 180 秒，红在"没跑到终态（status=paused）"，
    而被测产品的 `TERMINAL_RUN_STATES` 里 `paused` 从来都算终态。
    顺带钉住"抖了之后第二次报 409 也算放行成功"那条分支，别把它写成"照红"。
    """
    calls: list[str] = []
    drive = _fake_drive(calls, {
        "POST /tasks/t/approval": _response(502, FLAKE_BODY),
    })
    # 第一次抖、第二次 409（其实已经恢复过），随后读到 paused：必须当成功返回，不是抛
    table = {
        "POST /tasks/t/approval": _resp(409, '{"error":{"code":"task_not_waiting_approval"}}'),
        "GET /tasks/t": _resp(200, {"status": "paused"}),
    }

    class FlakyOnce(_FakeHTTP):
        def post(self, url, json=None, timeout=None):  # noqa: D102
            if url == "/tasks/t/approval" and not self.fired:
                self.fired = True
                self.calls.append("POST " + url + " (flake)")
                return _response(502, FLAKE_BODY)
            return self._hit("POST", url)

    drive = device.Drive("http://127.0.0.1:1/api/v1")
    transport = FlakyOnce(calls, table)
    transport.fired = False
    drive.c = transport  # type: ignore[assignment]
    status, retries = device._resume_with_guard(
        drive, "/tasks/t/approval", {"approve": False},
        terminal=frozenset({"succeeded", "failed", "cancelled", "paused"}))
    assert (status, retries) == ("paused", 1), (status, retries, calls)

    # 默认那组终态里不该有 paused：批准那条路必须跑到 succeeded，停在 paused 就是没放行。
    # 等待窗口在这里收成 0.6 秒（生产里是 180 秒）——这一条要的是"停在 paused 会红"这句话，
    # 不是白等三分钟；把窗口写死在函数里，这条判据就把整个文件的跑时拖成了 180 秒。
    assert "paused" not in device.RESUME_TERMINAL, device.RESUME_TERMINAL
    with pytest.raises(AssertionError, match=r"终态.*status=paused"):
        device._resume_with_guard(
            _fake_drive(calls, {"POST /tasks/t/approval": _resp(200, {"ok": True}),
                                 "GET /tasks/t": _resp(200, {"status": "paused"})}),
            "/tasks/t/approval", {"approve": True}, wait_seconds=0.6, poll_seconds=0.2)


def test_both_stub_branches_use_the_openai_tool_call_envelope() -> None:
    """桩的流式与非流式两条响应分支必须共用同一个信封，且产品从中读得出参数。

    m60 实测到的正是这里：非流式那一支发的是裸的 `{"name","arguments"}`。产品侧
    `tool_call_args` 只认 `call["function"]["arguments"]`（或 `args`），裸形状读出空参数
    ⇒ 高风险补丁退化成"缺少必填参数：patch"的 error 调用、风险算成 medium、按 `risk:auto`
    放行 ⇒ 一张审批卡都挂不出来。而这条路只有撞上"流式退非流式"的退路才会走到，
    所以单跑一格全绿、两格同跑就红——红的全是装置自己。
    """
    from blank_api.services.agent.graph import tool_call_args, tool_call_name

    device = _load_device()
    for call in (device._APPROVAL_CALL, device._QUESTION_CALL, device._READ):
        enveloped = device._envelope(call)
        assert tool_call_name(enveloped) == call["name"]
        assert tool_call_args(enveloped), f"{call['name']} 的参数在产品侧读不出来"
    patch = tool_call_args(device._envelope(device._APPROVAL_CALL))["patch"]
    assert device.PROBE_PATH in patch and "api_key" in patch, patch
    # 裸形状读得出工具名、读不出参数：这就是那一步不报错、只静默放行的原因
    assert tool_call_name(device._APPROVAL_CALL) == "apply_patch"
    assert tool_call_args(device._APPROVAL_CALL) == {}


class _FakeOut:
    """替身 socket 的写端：把桩发出的报文体原样收下来，好核对边界与帧。"""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data

    def flush(self) -> None:
        pass


def _stub_reply(messages: list[dict], *, stream: bool) -> tuple[bytes, dict[str, str], bool]:
    """只跑 `_Handler._respond`，绕开真 socket：返回（报文体, 响应头, 服务端是否收尾连接）。"""
    device = _load_device()
    handler = device._Handler.__new__(device._Handler)
    out = _FakeOut()
    headers: dict[str, str] = {}
    handler.wfile = out  # type: ignore[attr-defined]
    handler.request_version = "HTTP/1.1"  # type: ignore[attr-defined]
    handler.close_connection = False  # type: ignore[attr-defined]
    handler._body = lambda: {"messages": messages, "stream": stream, "model": "stub"}  # type: ignore[method-assign]
    handler.send_response = lambda code: None  # type: ignore[method-assign]
    handler.send_header = lambda key, value: headers.__setitem__(key, value)  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    handler._respond()
    return bytes(out.buf), headers, bool(handler.close_connection)


def test_every_stub_reply_is_self_bounded_and_the_stream_probe_has_no_frames() -> None:
    """桩的响应必须自带边界；流式退路那一格必须一个 `data:` 帧都不给。

    钉两件事，都是 m62–m66 实测出来的：
    * 老写法把 SSE 交给"连接关闭"定界，而 Windows 上服务端 `close()` 若撞上未读字节会发
      RST，把已经 flush 的响应一起丢——端到端读出来是 `ReadError`，产品记成
      `model_unreachable`、任务停在 failed，红的还是装置。现在两条分支都带
      Content-Length，客户端不必等那个永远不来的优雅 FIN。
    * 探针那一格只回 `: ping` 心跳：用产品自己的解析器验证它读不出任何帧，
      这样"退路没走"和"帧发多了"都会红，而不是静默全绿。
    """
    from blank_api.services.models.openai_compat import data_payload

    normal, normal_headers, _ = _stub_reply([{"role": "user", "content": "列一下工作区"}], stream=True)
    assert normal_headers["Content-Length"] == str(len(normal)), normal_headers
    assert all(chunk.startswith("data: ") for chunk in normal.decode().split("\n\n")[:-1]), normal[:120]
    assert any(data_payload(line) for line in normal.decode().splitlines()), "正常流式那一格没发出可解析的帧"
    assert "tool_calls" in normal.decode(), normal[:200]

    probe, probe_headers, probe_closes = _stub_reply(
        [{"role": "user", "content": f"{device.PROBE_STREAM_FLAKE} 列一下工作区"}], stream=True)
    assert b"data:" not in probe, probe
    assert not any(data_payload(line) for line in probe.decode().splitlines()), probe
    assert probe_headers["Content-Length"] == str(len(probe))
    assert probe_closes, "探针那一格也得收尾连接：不然 keep-alive 会把下一次调用打到旧连接上"

    plain, plain_headers, _ = _stub_reply([{"role": "user", "content": "列一下工作区"}], stream=False)
    assert plain_headers["Content-Length"] == str(len(plain))
    assert json.loads(plain)["choices"][0]["message"]["tool_calls"][0]["function"]["name"]


def test_the_diagnostics_lead_with_the_failure_reason() -> None:
    """红的时候第一句就得是"为什么"，取证通道坏了也不许改成另一个异常。

    m62–m66 那几轮每次都要重跑一遍才看清下一句话：判据文本按开头截断，而原因写在时间线
    末尾那条 `task.failed` 的载荷里。所以这条把两件事钉住——原因在最前、且带着载荷；
    证据接口本身 500 时只能报"取证失败"，不能把判定盖掉。
    """
    timeline = [
        {"type": "tool.completed", "payload": {"tool": "apply_patch", "status": "ok"}},
        {"type": "model.stream_fallback", "payload": {"code": "model_unreachable",
                                                      "reason": "模型服务不可达：stub（ReadError）"}},
        {"type": "task.failed", "payload": {"status": "failed", "reason": "模型服务不可达：stub（ReadError）"}},
    ]
    calls: list[str] = []
    drive = _fake_drive(calls, {
        "GET /tasks/t/evidence": _resp(200, {"timeline": timeline, "tools": [{"name": "apply_patch", "status": "ok"}]}),
        "GET /settings/agent": _resp(200, {"permission_mode": None, "approve_high_risk": None}),
    })
    why = device._why(drive, "t")
    assert why.startswith("｜终态原因"), why[:80]
    assert "ReadError" in why, why[:200]
    assert "task.failed" in why and "model.stream_fallback" in why, why[:200]

    broken = _fake_drive([], {"GET /tasks/broken/evidence": _resp(500, "boom"),
                              "GET /settings/agent": _resp(200, {})})
    assert "取证失败" in device._why(broken, "broken")

