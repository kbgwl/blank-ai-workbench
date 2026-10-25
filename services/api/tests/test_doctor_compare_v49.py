"""跨机对账（`blank doctor --compare`）的判据：差异必须有名字，同机必须不能冒充跨机。

M4 的退出判据是「在第二台机器上跑通，输出与本机的差异**可解释**」。这个装置只把后半句
做成能失败的东西：

- 归因表是**实测关系**（设计稿 §2.0：非管理员拿不到 AC/受限令牌，身份层只影响文件与网络；
  Job 管进程数与生命周期）。所以「身份层不同」解释得了 P-READ，**解释不了 P-PROC**——
  这条用例是整个装置承重的那块砖：把它放宽，任何差异都能被一句「机器不一样」糊过去。
- `host_id` 相同 → `same_host` 且退出码 1。同一台机器跑两次不是跨机验证，本批真跑用的
  就是本机两份实物报告，它必须被拒。
- 报告缺机器指纹（schema 1）→ 拒绝对账，不按档位补一份假指纹。
- 换 Python 版本、换系统小版本**不解释任何探针**：没实测过的关系就不写进归因表。
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys

import pytest

from blank_api import doctor

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="跨机对账的实物报告来自 Windows 探针")


def _report(*, host_id: str = "aaaaaaaaaaaa", strength: str = "job_only", **overrides) -> dict:
    report = {
        "schema": doctor.SCHEMA_VERSION,
        "platform": "win32",
        "python": "3.12.7",
        "host": {
            "host_id": host_id,
            "platform": "win32",
            "machine": "AMD64",
            "os_release": "11",
            "os_version": "10.0.26300",
            "elevated": False,
        },
        "audit_task_id": "",
        "capability": {
            "strength": strength,
            "apparatus_ok": True,
            "job": True,
            "appcontainer": strength == "os_enforced",
            "restricted_token": strength == "os_enforced",
        },
        "tiers": {"workspace": {"backend": "local", "available": True}},
        "probe_enforcement": {
            "filesystem_enforced": strength == "os_enforced",
            "network_enforced": strength == "os_enforced",
            "pids_enforced": True,
        },
        "boundaries": [
            {"id": probe_id, "status": doctor.NOT_ENFORCED}
            for probe_id in doctor.BOUNDARY_IDS
            if probe_id not in ("P-AUDIT", "P-PROC", "P-INHERIT")
        ]
        # 本机实测形状（job_only）：进程数与生命周期被内核强制，文件与网络没有。
        + [{"id": "P-PROC", "status": doctor.ENFORCED},
           {"id": "P-INHERIT", "status": doctor.ENFORCED},
           {"id": "P-AUDIT", "status": doctor.NOT_TESTED}],
    }
    report.update(overrides)
    return report


def _statuses(report: dict) -> dict[str, str]:
    return {item["id"]: item["status"] for item in report["boundaries"]}


def _set(report: dict, probe_id: str, status: str) -> None:
    for item in report["boundaries"]:
        if item["id"] == probe_id:
            item["status"] = status
            return
    raise AssertionError(f"夹具里没有 {probe_id}")


def _unexplained(result: dict) -> list[str]:
    return [item["probe"] for item in result["differences"] if not item["explained"]]


def test_identity_layer_difference_explains_file_and_network_probes() -> None:
    left, right = _report(), _report(host_id="bbbbbbbbbbbb", strength="os_enforced")
    for probe_id in ("P-READ", "P-WRITE", "P-ENV", "P-ESCAPE", "P-NET"):
        _set(right, probe_id, doctor.ENFORCED)
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.EXPLAINED, result["note"]
    assert result["exit_code"] == 0
    causes = {item["probe"]: item["causes"] for item in result["differences"]}
    assert set(causes) == {"P-READ", "P-WRITE", "P-ENV", "P-ESCAPE", "P-NET"}
    assert "boundary_strength" in causes["P-READ"]


def test_process_probe_difference_is_never_blamed_on_the_identity_layer() -> None:
    """这条是整个装置的承重墙：P-PROC 归 Job 管，身份层差异解释不了它。

    放宽它（"机器不同所以什么都可能"）之后，任何漂移都能被一句"环境不一样"糊成通过。
    """
    left, right = _report(), _report(host_id="bbbbbbbbbbbb", strength="os_enforced")
    for probe_id in ("P-READ", "P-WRITE", "P-ENV", "P-ESCAPE", "P-NET"):
        _set(right, probe_id, doctor.ENFORCED)
    # 假想第二台机器：身份层更强，进程数那条反而没拦住，而 Job 与账本都没变。
    _set(right, "P-PROC", doctor.NOT_ENFORCED)
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.UNEXPLAINED, result["note"]
    assert result["exit_code"] == 1
    assert _unexplained(result) == ["P-PROC"]


def test_job_capability_explains_the_process_probes() -> None:
    left = _report()
    right = _report(host_id="bbbbbbbbbbbb")
    right["capability"]["job"] = False
    right["probe_enforcement"]["pids_enforced"] = False
    _set(right, "P-PROC", doctor.NOT_ENFORCED)
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.EXPLAINED, result["note"]
    causes = {item["probe"]: item["causes"] for item in result["differences"]}
    assert "job" in causes["P-PROC"] or "pids_enforced" in causes["P-PROC"]


def test_backend_change_explains_a_whole_batch_of_untested() -> None:
    left, right = _report(), _report(host_id="bbbbbbbbbbbb")
    right["tiers"]["workspace"] = {"backend": "docker", "available": True}
    for probe_id in doctor.BOUNDARY_IDS:
        if probe_id != "P-AUDIT":
            _set(right, probe_id, doctor.NOT_TESTED)
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.EXPLAINED, result["note"]
    assert len(result["differences"]) == 7
    assert all("backend" in item["causes"] for item in result["differences"])


def test_two_reports_from_the_same_machine_are_not_cross_machine_proof() -> None:
    same = _report()
    result = doctor.compare_reports(same, _report())
    assert result["same_host"] is True
    assert result["verdict"] == doctor.SAME_HOST
    assert result["exit_code"] == 1
    assert "第二台机器" in result["note"]


def test_missing_machine_fingerprint_is_refused_not_filled_in() -> None:
    old = _report()
    old["schema"] = 1
    del old["host"]
    del old["audit_task_id"]
    result = doctor.compare_reports(old, _report(host_id="bbbbbbbbbbbb"))
    assert result["verdict"] == doctor.INCOMPATIBLE
    assert result["exit_code"] == 1
    assert "假指纹" in result["note"]


def test_different_schema_versions_are_not_comparable() -> None:
    left = _report()
    left["schema"] = doctor.SCHEMA_VERSION + 1
    result = doctor.compare_reports(left, _report(host_id="bbbbbbbbbbbb"))
    assert result["verdict"] == doctor.INCOMPATIBLE, result["note"]


def test_different_platform_is_refused_because_the_probes_are_windows_semantics() -> None:
    left = _report()
    left["host"]["platform"] = "linux"
    result = doctor.compare_reports(left, _report(host_id="bbbbbbbbbbbb"))
    assert result["verdict"] == doctor.INCOMPATIBLE
    assert "Windows" in result["note"]


@pytest.mark.parametrize("side", ["左", "右"])
def test_an_untrusted_apparatus_disqualifies_that_report(side: str) -> None:
    left, right = _report(), _report(host_id="bbbbbbbbbbbb")
    target = left if side == "左" else right
    target["capability"]["apparatus_ok"] = False
    target["capability"]["strength"] = "unknown"
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.UNTRUSTED, result["note"]
    assert f"{side}侧" in result["note"]
    assert result["exit_code"] == 1


def test_a_report_that_never_measured_the_apparatus_is_not_trusted() -> None:
    """没测过 = 没有结论资格。缺键与 `False` 必须走同一个分支，否则"没写"会被读成"可信"。"""
    left, right = _report(), _report(host_id="bbbbbbbbbbbb")
    del left["capability"]["apparatus_ok"]
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.UNTRUSTED, result["note"]
    assert "左侧" in result["note"]


def test_version_differences_alone_explain_nothing() -> None:
    """换 Python / 换系统小版本我们没实测会改变哪条结论，所以它不算理由。"""
    left, right = _report(), _report(host_id="bbbbbbbbbbbb")
    right["python"] = "3.13.1"
    right["host"]["os_version"] = "10.0.19045"
    _set(right, "P-READ", doctor.ENFORCED)
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.UNEXPLAINED, result["note"]
    assert _unexplained(result) == ["P-READ"]
    assert {item["name"] for item in result["context"]} == {"python", "os_version"}


def test_audit_probe_difference_from_run_flags_is_not_a_machine_claim() -> None:
    left, right = _report(), _report(host_id="bbbbbbbbbbbb")
    right["audit_task_id"] = "16663dff-8284-4013-8310-d7c35bff8da1"
    _set(right, "P-AUDIT", doctor.VERIFIED)
    result = doctor.compare_reports(left, right)
    assert result["verdict"] == doctor.EXPLAINED, result["note"]
    causes = {item["probe"]: item["causes"] for item in result["differences"]}
    assert causes == {"P-AUDIT": ["run_flags"]}


def test_rendered_refusal_does_not_claim_the_probes_matched() -> None:
    """提前返回时对账**没发生过**：文案不许因为"差异列表是空的"就报八条一致。"""
    old = _report()
    old["schema"] = 1
    del old["host"]
    del old["audit_task_id"]
    result = doctor.compare_reports(old, _report(host_id="bbbbbbbbbbbb"))
    assert result["compared"] is False
    text = doctor.render_compare(result)
    assert "逐条相同" not in text
    assert "没有逐条比较过" in text


def test_identical_reports_from_two_machines_are_consistent() -> None:
    result = doctor.compare_reports(_report(), _report(host_id="bbbbbbbbbbbb"))
    assert result["verdict"] == doctor.CONSISTENT
    assert result["exit_code"] == 0
    assert result["differences"] == []


def test_host_identity_hashes_the_hostname_and_keeps_the_shape_fields() -> None:
    host = doctor.host_identity()
    assert len(host["host_id"]) == 12
    assert all(char in "0123456789abcdef" for char in host["host_id"])
    assert host["platform"] == sys.platform
    assert host["machine"] == platform.machine()
    # 报告要能贴给别人看：主机名不许以明文出现。
    node = platform.node() or ""
    if node:
        assert node not in json.dumps(host, ensure_ascii=False)
    assert host["elevated"] in (True, False, None), "拿不到提权状态必须写 None，不许写成 False"


def test_collected_report_carries_the_fingerprint_and_refuses_itself_as_cross_machine() -> None:
    """真跑一份本机报告（八条探针），再拿它和自己对账：必须被拒成 same_host。

    这就是"本机重跑冒充跨机验证"的实物反证——不是构造出来的夹具。
    """
    report = asyncio.run(doctor.collect())
    assert report["schema"] == doctor.SCHEMA_VERSION
    assert report["host"]["host_id"]
    assert report["audit_task_id"] == ""
    assert {item["id"] for item in report["boundaries"]} == set(doctor.BOUNDARY_IDS)
    again = asyncio.run(doctor.collect())
    result = doctor.compare_reports(report, again)
    assert result["verdict"] == doctor.SAME_HOST, result["note"]
    assert result["exit_code"] == 1
