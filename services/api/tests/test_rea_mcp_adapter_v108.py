"""#108 的判据：REA 的 MCP Adapter 真的接进了 `/reverse-lab`。

每条判据都注明**什么缺陷会让它红**。装置自检放在最前面：构造反例的工具函数一旦算错偏移，
反例就会"红得不对"（比如本该第 5 步失败却在第 2 步就失败），所以每个反例都断言到
「第几步 + 步骤名 + 被解析对象」三件，而不是只断言"报错了"。

字节布局是照着公开格式自己拼的（与 ``services/rea/artifact.py`` 同一套偏移），
仓库里那份真实 ``app.asar`` / ``elevate.exe`` 另外单测一次（存在才跑，见文件末尾）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path

import pytest

from blank_api.services.mcp import bridge as mcp_bridge
from blank_api.services.rea import adapter, artifact

API = "/api/v1"
DESKTOP_RESOURCES = Path(__file__).resolve().parents[3] / "desktop" / "dist" / "win-unpacked" / "resources"


# --------------------------------------------------------------------------- #
# 实物构造：已知答案的正样与反样
# --------------------------------------------------------------------------- #
def pack_asar(meta_json: bytes, data: bytes) -> bytes:
    """按 asar 的头部布局打包：[4][header_size][string_size][json_size][json+pad][数据]。

    三个尺寸字段互相咬合（``string_size = 4 + len(json) + pad``、``header_size = 4 + string_size``），
    改哪一个都能造出不同步骤的反例——所以自检断言放在 happy path 里。
    """
    json_size = len(meta_json)
    pad = (-json_size) % 4
    string_size = 4 + json_size + pad
    header_size = 4 + string_size
    return struct.pack("<IIII", 4, header_size, string_size, json_size) + meta_json + b"\x00" * pad + data


def build_asar(files: dict[str, bytes]) -> bytes:
    ordered = sorted(files)
    offset = 0
    entries = {}
    blob = bytearray()
    for name in ordered:
        body = files[name]
        entries[name] = {"size": len(body), "offset": str(offset), "unpacked": False}
        blob.extend(body)
        offset += len(body)
    return pack_asar(json.dumps({"files": entries}, separators=(",", ":")).encode("utf-8"), bytes(blob))


DEMO_PACKAGE = json.dumps({"name": "demo-app", "version": "9.9.9", "main": "index.cjs"})


def build_pe(*, signature: bytes = b"PE\x00\x00", sections: int = 2) -> bytes:
    """一个自洽的 PE32：两个段，.text 在 512、.rdata 在 1024，总长 1280。"""
    e_lfanew = 0x40
    opt_size = 224
    # MS-DOS 头要能容下 0x3C 处的 4 字节 e_lfanew：2 字节魔数 + 62 字节填充 = 64 字节
    dos = bytearray(b"MZ" + bytes(62))
    struct.pack_into("<I", dos, 0x3C, e_lfanew)
    coff = signature + struct.pack("<HHIIIHH", 0x14C, sections, 0x6545_0000, 0, 0, opt_size, 0x0102)
    optional = bytearray(opt_size)
    struct.pack_into("<H", optional, 0, 0x10B)  # PE32
    struct.pack_into("<I", optional, 16, 0x2000)  # AddressOfEntryPoint
    struct.pack_into("<I", optional, 28, 0x1000)  # BaseOfCode
    struct.pack_into("<I", optional, 32, 0x2000)  # ImageBase
    struct.pack_into("<I", optional, 56, 0x4000)  # SizeOfImage
    struct.pack_into("<I", optional, 60, 0x200)  # SectionAlignment
    struct.pack_into("<I", optional, 64, 0x200)  # FileAlignment
    struct.pack_into("<H", optional, 68 - 2, 3)  # Subsystem = 控制台

    def section(name: bytes, vsize: int, vaddr: int, rsize: int, rptr: int) -> bytes:
        # 段头固定 40 字节：8 字节名 + VirtualSize/VirtualAddress/SizeOfRawData/PointerToRawData/
        # PointerToRelocations/PointerToLinenumbers（各 4）+ 两个计数（各 2）+ Characteristics（4）
        return name + struct.pack("<IIIIIIHHI", vsize, vaddr, rsize, rptr, 0, 0, 0, 0, 0x60000020)

    table = bytearray()
    if sections >= 1:
        table += section(b".text\x00\x00\x00", 0x100, 0x2000, 0x200, 0x200)
    if sections >= 2:
        table += section(b".rdata\x00\x00", 0x80, 0x3000, 0x100, 0x400)
    body = bytes(dos) + bytes(coff) + bytes(optional) + bytes(table)
    return body + bytes(512 - len(body)) + bytes(512) + bytes(256)


def build_elf(*, shentsize: int = 64, shnum: int = 3, shoff: int = 0x1000) -> bytes:
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    head = struct.pack(
        "<HHIQQQIHHHHHH",
        2,  # e_type = 可执行
        0x3E,  # e_machine = AMD x86-64
        1,
        0x401050,  # e_entry
        0x40,  # e_phoff
        shoff,  # e_shoff
        0,
        64,
        56,
        2,
        shentsize,
        shnum,
        shnum - 1 if shnum else 0,
    )
    body = ident + head
    return body + bytes(shoff - len(body)) + bytes(shentsize * shnum)


# 已知答案的反例表：(目标, 格式, 期望步骤, 期望步骤名, 期望被点名的对象)
# 只有一处定义——装置自检（test_parser_happy_path_answers_are_known）、落盘的反例文件、
# 解析层单测与 HTTP 判据读的都是同一张表，构造器算错偏移时先在这里红。
COUNTER_EXAMPLES = [
    ("missing_body.asar", "asar", 5, "文件内容偏移", "<missing_body.asar→index.cjs>"),
    ("short_header.asar", "asar", 2, "头部 pickle 尺寸", "<short_header.asar>"),
    ("bad_meta.asar", "asar", 4, "元数据形状", "<bad_meta.asar>"),
    ("liar_table.asar", "asar", 5, "文件内容偏移", "<liar_table.asar→a.js>"),
    ("bad_sig.exe", "pe", 3, "PE 签名", "<bad_sig.exe>"),
    ("clip.exe", "pe", 7, "段内容边界", "<clip.exe#.rdata>"),
    ("wrong_entsize.elf", "elf", 4, "段表规模", "<wrong_entsize.elf>"),
]


def _counter_example_bytes(target: str) -> bytes:
    package = DEMO_PACKAGE.encode("utf-8")
    whole = build_asar({"package.json": package, "index.cjs": bytes(600)})
    return {
        "missing_body.asar": lambda: whole[: len(whole) - 300],
        "short_header.asar": lambda: whole[:18],
        "bad_meta.asar": lambda: pack_asar(b"1234", b""),
        "liar_table.asar": lambda: pack_asar(b'{"files":{"a.js":{"size":40,"offset":"0"}}}', b"abc"),
        "bad_sig.exe": lambda: build_pe(signature=b"XX\x00\x00"),
        "clip.exe": lambda: build_pe()[:1064],
        "wrong_entsize.elf": lambda: build_elf(shentsize=40, shnum=2, shoff=0x100),
        "random.bin": lambda: bytes(range(64)),
    }[target]()


def build_reverse_lab_root(root: Path) -> Path:
    """在授权根里放一份正样 + 一组已知答案的反样（反例字节全部来自同一张表）。"""
    (root / "resources").mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "resources" / "app.asar").write_bytes(
        build_asar(
            {
                "package.json": DEMO_PACKAGE.encode("utf-8"),
                "index.cjs": b"console.log('entry point');\n",
            }
        )
    )
    for name, *_rest in COUNTER_EXAMPLES:
        (root / name).write_bytes(_counter_example_bytes(name))
    (root / "random.bin").write_bytes(_counter_example_bytes("random.bin"))
    return root


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
async def _no_leftover_connection():
    """REA 的子进程按根目录缓存。用例之间事件循环不同，留着就会互相绊住。"""
    yield
    await adapter.close_all()
    adapter.reset_connections()


async def _bind_project(client, root: Path) -> str:
    project = (await client.post(f"{API}/projects", json={"name": "REA 逆向项目"})).json()
    bound = await client.post(
        f"{API}/projects/{project['id']}/workspaces", json={"path": str(root)}
    )
    assert bound.status_code == 201, bound.text
    return project["id"]


@pytest.fixture
def rea_root(tmp_path: Path) -> Path:
    return build_reverse_lab_root(tmp_path / "rev")


def _events_by_type(events: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in events:
        grouped.setdefault(item["type"], []).append(item)
    return grouped


# --------------------------------------------------------------------------- #
# 判据 1：适配器状态必须说清"连上了、工具表全不全、在哪个边界执行"
# --------------------------------------------------------------------------- #
async def test_status_reports_the_real_tool_table(client, rea_root: Path):
    """红于：接线断了、服务端起不来、工具表少一个却报 ready、边界事实没写。"""
    project_id = await _bind_project(client, rea_root)
    body = (await client.get(f"{API}/reverse-lab/status?project_id={project_id}")).json()
    adapter_body = body["adapter"]

    assert adapter_body["state"] == "ready"
    assert adapter_body["server"] == "rea-artifacts 0.1.0"
    assert adapter_body["transport"] == "stdio"
    assert sorted(adapter_body["declared"]) == sorted(adapter.EXPECTED_TOOLS)
    assert sorted(adapter_body["tools"]) == [
        "mcp__rea__find_strings",
        "mcp__rea__inspect_target",
        "mcp__rea__scan_targets",
    ]
    assert adapter_body["missing"] == []
    # 边界事实：REA 服务端是我们拉起来的进程。Windows 上必须真的挂进 Job；
    # 挂不上（或非 Windows 没有 Job）就必须红字说"未由内核强制"，不许写成已纳入边界。
    if sys.platform == "win32":
        assert adapter_body["boundary"] == "job_object"
        assert "Job Object" in adapter_body["boundary_note"]
    else:
        assert adapter_body["boundary"] == "host_process_unbounded"
        assert "未由内核强制" in adapter_body["boundary_note"]
    # 宿主绝对路径不许出现在给前端的任何字段里
    assert str(rea_root) not in json.dumps(body, ensure_ascii=False)
    assert body["root"].startswith("<workspace:")


async def test_rea_does_not_pollute_the_global_mcp_table(client, rea_root: Path):
    """红于：把 REA 塞进全局 manager()/mcp.json 的账里——设置页的工具条数就变成假的了。"""
    project_id = await _bind_project(client, rea_root)
    await client.get(f"{API}/reverse-lab/status?project_id={project_id}")

    settings_body = (await client.get(f"{API}/settings/mcp")).json()
    assert settings_body["tools"] == 0
    assert [item["name"] for item in settings_body["servers"]] == []

    bundle = await mcp_bridge.manager().bundle()
    assert bundle.tools == []
    assert not any(tool.name.startswith("mcp__rea__") for tool in bundle.tools)


# --------------------------------------------------------------------------- #
# 判据 2：一条真实链路跑通，结果进事件与证据（已知答案的正样）
# --------------------------------------------------------------------------- #
async def test_happy_path_lands_in_events_and_evidence(client, rea_root: Path):
    """红于：工具没被发现、调用没进网关、台账没落库、结论没有 Evidence ID。"""
    project_id = await _bind_project(client, rea_root)
    response = await client.post(
        f"{API}/reverse-lab/investigations",
        json={"project_id": project_id, "target": "resources/app.asar"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    task_id = body["task"]["id"]

    assert body["state"] == "ok" and body["ok"] is True
    assert body["result"]["format"] == "asar"
    # 已知答案：入口、包名版本、文件表都必须是真读出来的
    found = {item["kind"]: item["value"] for item in body["findings"]}
    assert found["入口"] == "index.cjs"
    assert found["包声明"] == "demo-app 9.9.9"
    assert found["文件表"] == "index.cjs, package.json"
    assert found["实物指纹"].startswith("asar，")
    # 自检：正样的字节确实是自洽的（尺寸字段算错就会在这里红，而不是让反例"红得不对"）
    assert body["result"]["size"] == len((rea_root / "resources" / "app.asar").read_bytes())

    evidence_ids = [item["evidence_id"] for item in body["findings"]]
    assert len(evidence_ids) == len(set(evidence_ids))
    assert all(re.fullmatch(r"EVD-[0-9a-f]{10}", item) for item in evidence_ids)

    assert body["evidence"]["tool"] == "mcp__rea__inspect_target"
    assert body["evidence"]["boundary_kind"] == "mcp_host_process"
    assert body["evidence"]["risk_level"] == "low"

    events = (await client.get(f"{API}/tasks/{task_id}/events")).json()
    grouped = _events_by_type(events)
    assert grouped["task.created"][0]["payload"]["origin"] == "reverse_lab"
    started = grouped["rea.investigation.started"][0]["payload"]
    assert started["tool"] == "mcp__rea__inspect_target"
    assert sorted(started["tools"]) == sorted(
        ["mcp__rea__find_strings", "mcp__rea__inspect_target", "mcp__rea__scan_targets"]
    )
    completed = grouped["tool.completed"][0]["payload"]
    assert completed["tool"] == "mcp__rea__inspect_target"
    assert completed["status"] == "ok" and completed["ok"] is True
    assert completed["boundary_kind"] == "mcp_host_process"
    assert completed["tool_call_id"] == body["evidence"]["tool_call_id"]
    recorded = grouped["rea.findings.recorded"][0]["payload"]
    assert recorded["count"] == len(body["findings"])
    assert recorded["evidence_ids"] == evidence_ids
    assert grouped["task.completed"][0]["payload"]["state"] == "ok"
    # 不许有被咽掉的失败
    assert "rea.parse.failed" not in grouped
    assert "rea.investigation.blocked" not in grouped

    bundle = (await client.get(f"{API}/tasks/{task_id}/evidence")).json()
    assert bundle["task"]["status"] == "succeeded"
    tool_rows = [item for item in bundle["tools"] if item["name"] == "mcp__rea__inspect_target"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["status"] == "ok"
    assert tool_rows[0]["risk_level"] == "low"
    assert tool_rows[0]["boundary_kind"] == "mcp_host_process"
    assert tool_rows[0]["arguments"] == {"target": "resources/app.asar"}
    # 每条结论都带着 Evidence ID 进验收位，而不是只活在事件流里
    covered = [item for item in bundle["acceptance"] if item.get("source") == "rea_mcp"]
    assert covered and all(item["covered"] and item["evidence"] for item in covered)
    assert bundle["boundaries"]["tool_calls"]["mcp_host_process"] >= 1

    artifact_body = (
        await client.get(f"{API}/tasks/{task_id}/artifacts/{body['evidence']['tool_call_id']}")
    ).json()
    assert artifact_body["data"]["structured"]["entry"]["main"] == "index.cjs"

    task = (await client.get(f"{API}/tasks/{task_id}")).json()
    assert task["status"] == "succeeded"
    assert "5 条结论" in task["result_summary"] or "条结论" in task["result_summary"]


async def test_tool_call_is_audited_with_the_host_boundary(client, rea_root: Path):
    """红于：审计账没写、边界写成了进程内、把"读了个文件"记成"起了条命令"。"""
    audit_path = Path(os.environ["BLANK_AUDIT_DIR"]) / "tool-calls.jsonl"
    project_id = await _bind_project(client, rea_root)
    body = (
        await client.post(
            f"{API}/reverse-lab/investigations",
            json={"project_id": project_id, "target": "resources/app.asar"},
        )
    ).json()
    lines = _audit_records(audit_path)
    matched = [item for item in lines if item.get("tool") == "mcp__rea__inspect_target"]
    assert matched, f"审计账里没有这条调用（现有 {len(lines)} 行）"
    line = matched[-1]
    assert line["detail"]["boundary_kind"] == "mcp_host_process"
    # 只读解析不起命令：ran_command 必须是 False，而 boundary 仍然说明它在宿主进程里跑过
    assert line["detail"]["ran_command"] is False
    assert line["status"] == "ok"
    assert line["task_id"] == body["task"]["id"]


def _audit_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return rows


# --------------------------------------------------------------------------- #
# 判据 3：已知答案的实物反例——报错必须指名"哪一步、哪个对象"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("target", "format_name", "step", "step_name", "object_name"),
    COUNTER_EXAMPLES,
    ids=[item[0] for item in COUNTER_EXAMPLES],
)
async def test_parse_failure_names_step_and_object(
    client, rea_root: Path, target: str, format_name: str, step: int, step_name: str, object_name: str
):
    """红于：报错退化成笼统的一句"解析失败"、步骤名漂了、HTTP 变成 500、事件里查不到是谁。"""
    project_id = await _bind_project(client, rea_root)
    response = await client.post(
        f"{API}/reverse-lab/investigations", json={"project_id": project_id, "target": target}
    )
    # 不是 5xx：调用真的发生了，失败的是那个实物
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "parse_failed"
    assert body["ok"] is False

    reason = body["reason"]
    assert reason.startswith(f"解析失败｜被解析对象 {object_name}｜格式 {format_name}｜")
    assert f"步骤 {step}/{len(artifact.STEPS[format_name])}「{step_name}」" in reason
    # 三件里少任何一件都不算指名道姓：步号、步骤名、对象
    assert body["failure"]["step"] == step
    assert body["failure"]["step_name"] == step_name
    assert body["failure"]["target"] in object_name
    assert body["failure"]["detail"], "必须说清声明值与实际字节数的差"

    task_id = body["task"]["id"]
    events = _events_by_type((await client.get(f"{API}/tasks/{task_id}/events")).json())
    failed = events["rea.parse.failed"][0]["payload"]
    assert failed["step"] == step
    assert failed["step_name"] == step_name
    assert failed["parsed_object"] in object_name
    assert events["tool.completed"][0]["payload"]["status"] == "error"
    assert events["task.failed"][0]["payload"]["state"] == "parse_failed"

    bundle = (await client.get(f"{API}/tasks/{task_id}/evidence")).json()
    assert bundle["task"]["status"] == "failed"
    assert bundle["task"]["failure_reason"] == reason
    uncovered = [item for item in bundle["acceptance"] if not item["covered"]]
    assert uncovered and reason in uncovered[0]["evidence"]


async def test_unsupported_format_is_not_reported_as_nothing_found(client, rea_root: Path):
    """红于：把"没能力看"写成"没有发现"——那正是 §20.6 里 unavailable 被吞掉的形状。"""
    project_id = await _bind_project(client, rea_root)
    body = (
        await client.post(
            f"{API}/reverse-lab/investigations",
            json={"project_id": project_id, "target": "random.bin"},
        )
    ).json()
    assert body["state"] == "unsupported"
    assert "没有解析器认领被解析对象 <random.bin>" in body["reason"]
    assert "首部字节" in body["reason"]
    assert body["findings"] == []
    # 台账里必须留下这条"没能力看"，而不是消失
    assert body["evidence"]["tool_call_id"]
    bundle = (await client.get(f"{API}/tasks/{body['task']['id']}/evidence")).json()
    assert bundle["task"]["status"] == "failed"
    assert bundle["tools"][0]["status"] == "error"


# --------------------------------------------------------------------------- #
# 判据 4：外部服务端不可用 = 明确报状态，绝不半份工具表冒充成功
# --------------------------------------------------------------------------- #
async def test_unavailable_server_reports_explicit_state(client, rea_root: Path):
    """红于：连不上却返回空清单、返回 500、或者把服务端自己的话咽了。"""
    project_id = await _bind_project(client, rea_root)
    # 授权根消失（卷没挂载 / 缓存被清）——服务端自己会为此说一句话
    shutil.rmtree(rea_root)

    status = (await client.get(f"{API}/reverse-lab/status?project_id={project_id}")).json()
    assert status["adapter"]["state"] == "failed"
    assert status["adapter"]["tools"] == []
    assert status["adapter"]["boundary"] == "unknown"
    assert status["adapter"]["root_exists"] is False
    # 这句话是子进程 stderr 的原样，不是我们编的
    assert "REA MCP 服务端启动失败：授权根目录不存在或不是目录" in status["adapter"]["error"]

    targets = await client.get(f"{API}/reverse-lab/targets?project_id={project_id}")
    assert targets.status_code == 503, targets.text
    assert targets.json()["error"]["code"] == "rea_adapter_unavailable"

    investigations = await client.post(
        f"{API}/reverse-lab/investigations",
        json={"project_id": project_id, "target": "resources/app.asar"},
    )
    assert investigations.status_code == 503, investigations.text
    assert "REA MCP 服务端启动失败" in investigations.json()["error"]["message"]
    # /strings 也必须同一顺序：把「链路坏了」说成「用户挑错了文件」的读法，
    # 三个端点里任一处放宽都会让界面上一格绿一格红，操作者分不清坏了什么。
    strings = await client.get(
        f"{API}/reverse-lab/strings",
        params={"project_id": project_id, "target": "resources/app.asar"},
    )
    assert strings.status_code == 503, strings.text
    assert strings.json()["error"]["code"] == "rea_adapter_unavailable", strings.text
    # 没连上就不该留下一条"跑了但什么都没有"的任务
    tasks = (await client.get(f"{API}/tasks", params={"project_id": project_id})).json()
    assert tasks == []


async def test_partial_tool_table_is_refused_not_trusted(client, rea_root: Path, monkeypatch):
    """红于：服务端少报一个工具，适配器却报 ready——半份工具表冒充成功就是这一条。"""
    monkeypatch.setenv("BLANK_REA_OMIT_TOOLS", "find_strings")
    project_id = await _bind_project(client, rea_root)

    status = (await client.get(f"{API}/reverse-lab/status?project_id={project_id}")).json()
    body = status["adapter"]
    assert body["state"] == "partial"
    assert body["missing"] == ["find_strings"]
    assert body["tools_count"] == 2
    assert body["expected"] == list(adapter.EXPECTED_TOOLS)
    assert "工具表不全" in body["error"] and "find_strings" in body["hint"]

    investigations = await client.post(
        f"{API}/reverse-lab/investigations",
        json={"project_id": project_id, "target": "resources/app.asar"},
    )
    assert investigations.status_code == 503
    tasks = (await client.get(f"{API}/tasks", params={"project_id": project_id})).json()
    assert tasks == []


async def test_disable_switch_reports_disabled(client, rea_root: Path, monkeypatch):
    """红于：开关关着还去起进程，或者关掉了却不告诉界面。"""
    monkeypatch.setenv("BLANK_REA_DISABLE", "1")
    project_id = await _bind_project(client, rea_root)
    status = (await client.get(f"{API}/reverse-lab/status?project_id={project_id}")).json()
    assert status["adapter"]["state"] == "disabled"
    assert "BLANK_REA_DISABLE" in status["adapter"]["error"]
    response = await client.get(f"{API}/reverse-lab/targets?project_id={project_id}")
    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# 判据 5：边界与脱敏
# --------------------------------------------------------------------------- #
async def test_target_escape_is_refused_before_a_task_exists(client, rea_root: Path):
    """红于：``..`` 或绝对路径能越出授权根；或者被拒时还留下了一条 Task 行。"""
    project_id = await _bind_project(client, rea_root)
    (rea_root.parent / "outside.txt").write_text("宿主侧的别的文件\n", encoding="utf-8")

    escape = await client.post(
        f"{API}/reverse-lab/investigations",
        json={"project_id": project_id, "target": "../outside.txt"},
    )
    assert escape.status_code == 403, escape.text
    assert escape.json()["error"]["code"] == "path_escape_denied"

    absolute = await client.post(
        f"{API}/reverse-lab/investigations",
        json={"project_id": project_id, "target": str(rea_root.parent / "outside.txt")},
    )
    assert absolute.status_code == 403

    missing = await client.post(
        f"{API}/reverse-lab/investigations",
        json={"project_id": project_id, "target": "nope.asar"},
    )
    assert missing.status_code == 404

    assert (await client.get(f"{API}/tasks", params={"project_id": project_id})).json() == []


async def test_scan_and_strings_come_from_the_server_not_a_mock(client, rea_root: Path):
    """红于：候选清单写死在前端/后端里（真目录换了却不跟着变），或路径越界没被拦。"""
    project_id = await _bind_project(client, rea_root)
    (rea_root / "extra.dll").write_bytes(build_pe())

    listed = (await client.get(f"{API}/reverse-lab/targets?project_id={project_id}")).json()
    names = {item["target"] for item in listed["targets"]}
    assert {"resources/app.asar", "extra.dll", "clip.exe"} <= names
    formats = {item["target"]: item["format"] for item in listed["targets"]}
    assert formats["resources/app.asar"] == "asar"
    assert formats["extra.dll"] == "pe"
    assert formats["random.bin"] == ""

    strings = (
        await client.get(
            f"{API}/reverse-lab/strings",
            params={"project_id": project_id, "target": "resources/app.asar", "min_length": 6},
        )
    ).json()
    texts = " ".join(item["text"] for item in strings["strings"])
    assert "demo-app" in texts and "console.log" in texts
    assert strings["adapter"]["state"] == "ready"

    escaped = await client.get(
        f"{API}/reverse-lab/targets", params={"project_id": project_id, "path": ".."}
    )
    assert escaped.status_code == 403

    # 三种读法各归各位：能力在线却挑错路径是 404，越界是 403，链路坏了才是 503。
    # 「先判能力」不能把前两格一起吃掉——这一条就是那格的读数。
    absent = await client.get(
        f"{API}/reverse-lab/targets", params={"project_id": project_id, "path": "没有这个目录"}
    )
    assert absent.status_code == 404, absent.text


async def test_no_host_path_nor_secret_leaks_into_the_recorded_chain(client, rea_root: Path, monkeypatch):
    """红于：事件/证据里出现宿主绝对路径（目录结构被端出去）或明文密钥。"""
    monkeypatch.setenv("BLANK_REA_TEST_SECRET", "合成密钥-DO-NOT-LEAK")
    project_id = await _bind_project(client, rea_root)
    body = (
        await client.post(
            f"{API}/reverse-lab/investigations",
            json={"project_id": project_id, "target": "resources/app.asar"},
        )
    ).json()
    task_id = body["task"]["id"]
    texts = json.dumps(
        {
            "investigation": body,
            "events": (await client.get(f"{API}/tasks/{task_id}/events")).json(),
            "evidence": (await client.get(f"{API}/tasks/{task_id}/evidence")).json(),
            "artifact": (
                await client.get(f"{API}/tasks/{task_id}/artifacts/{body['evidence']['tool_call_id']}")
            ).json(),
        },
        ensure_ascii=False,
    )
    assert str(rea_root) not in texts
    assert re.search(r"[A-Za-z]:[\\/]{1,2}(Users|projects)", texts) is None
    assert "DO-NOT-LEAK" not in texts


# --------------------------------------------------------------------------- #
# 判据 6：解析层自身的已知答案（不经 MCP，也要能被同一批反例证伪）
# --------------------------------------------------------------------------- #
def test_parser_happy_path_answers_are_known():
    """红于：布局算错、字段读错；也自检反例构造器本身（截断必须落在预期的那一步）。"""
    raw = build_asar({"package.json": DEMO_PACKAGE.encode("utf-8"), "index.cjs": b"x"})
    report = artifact.inspect_bytes("app.asar", raw)
    assert report["format"] == "asar"
    assert report["entry"]["main"] == "index.cjs"
    assert [item["kind"] for item in report["findings"]] == [
        "实物指纹",
        "容器结构",
        "文件表",
        "包声明",
        "入口",
    ]

    pe = artifact.inspect_bytes("app.exe", build_pe())
    assert pe["entry"] == {"width": 32, "entry_rva": 0x2000, "entry_section": ".text", "sections": 2}
    elf = artifact.inspect_bytes("app.elf", build_elf())
    assert elf["entry"] == {"width": 64, "endianness": "little", "entry_rva": 0x401050, "sections": 3}

    # 自检：反例构造器算错偏移时，先在这里红——每个反例都必须落在预期的那一步
    for target_name, format_name, expected_step, expected_step_name, object_name in COUNTER_EXAMPLES:
        raw = _counter_example_bytes(target_name)
        with pytest.raises(artifact.ArtifactParseError) as boom:
            artifact.inspect_bytes(target_name, raw)
        failure = boom.value.as_dict()
        assert (failure["format"], failure["step"], failure["step_name"]) == (
            format_name,
            expected_step,
            expected_step_name,
        )
        assert failure["target"] in object_name

    with pytest.raises(artifact.UnsupportedFormat) as boom:
        artifact.inspect_bytes("random.bin", bytes(range(64)))
    assert boom.value.kind == "unsupported_format"


@pytest.mark.parametrize(
    ("target", "format_name", "step", "step_name", "object_name"),
    COUNTER_EXAMPLES,
    ids=[item[0] for item in COUNTER_EXAMPLES],
)
def test_parser_failure_carries_step_and_object(target, format_name, step, step_name, object_name):
    """红于：步骤表与报错文案漂移（改了步骤名却不改断言，或反之）。"""
    raw = _counter_example_bytes(target)
    with pytest.raises(artifact.ArtifactParseError) as boom:
        artifact.inspect_bytes(target, raw)
    failure = boom.value.as_dict()
    assert failure["format"] == format_name
    assert failure["step"] == step
    assert failure["step_name"] == step_name
    assert artifact.STEPS[format_name][step - 1] == step_name
    assert failure["target"] in object_name


def test_load_target_refuses_directories_and_escapes(rea_root: Path):
    """红于：目标是目录时也去读字节；越出授权根时不报错。"""
    from blank_api.services.rea.artifact import TargetUnavailable

    with pytest.raises(TargetUnavailable):
        artifact.load_target(str(rea_root), "resources")
    with pytest.raises(TargetUnavailable):
        artifact.load_target(str(rea_root), "../outside.txt")
    with pytest.raises(TargetUnavailable):
        artifact.load_target(str(rea_root), "没有这个文件.asar")


# --------------------------------------------------------------------------- #
# 判据 7：仓库里那份真实产物（存在才跑；不在就如实说明）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (DESKTOP_RESOURCES / "app.asar").exists(), reason="本机没有桌面包产物")
def test_real_desktop_asar_answers_are_known():
    """红于：解析器只对自己的合成样本成立，遇到真产物就报错。

    字面量按 0.8.5 产物当刻量得（6 个文件，与 desktop/package.json 的 build.files 同集）；
    壳文件增删会让这条红，是有意的。
    """
    raw = (DESKTOP_RESOURCES / "app.asar").read_bytes()
    report = artifact.inspect_bytes("app.asar", raw)
    assert report["format"] == "asar"
    assert report["entry"]["main"] == "main.cjs"
    values = {item["kind"]: item["value"] for item in report["findings"]}
    assert values["容器结构"] == "6 个文件 / 0 个子目录"
    assert set(values["文件表"].split(", ")) == {
        "api-base.cjs",
        "gpu-guard.cjs",
        "main.cjs",
        "package.json",
        "preload.cjs",
        "shell-policy.cjs",
    }
    assert values["包声明"].startswith("blank-ai-workbench-desktop")


@pytest.mark.skipif(not (DESKTOP_RESOURCES / "elevate.exe").exists(), reason="本机没有桌面包产物")
def test_real_pe_truncation_names_the_section():
    """红于：真实 PE 截断时报不出是哪个段缺了多少字节。"""
    raw = (DESKTOP_RESOURCES / "elevate.exe").read_bytes()[:53760]
    with pytest.raises(artifact.ArtifactParseError) as boom:
        artifact.inspect_bytes("elevate.exe", raw)
    failure = boom.value.as_dict()
    assert failure["format"] == "pe"
    assert failure["step"] == 7 and failure["step_name"] == "段内容边界"
    assert failure["target"].startswith("elevate.exe#")
    assert "缺 " in failure["detail"]
