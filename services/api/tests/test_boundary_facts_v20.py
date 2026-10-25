"""边界事实（`boundary_strength` / `enforced`）从探测到事件到面板必须一路同口径。

M1a 把 Job Object 接进执行层以后，剩下的风险全在"搬运"这一环：执行层知道内存上限真被
内核拦了，面板却按后端名猜；或反过来，探测装置不可信时面板点亮一片绿。这个文件钉的就是
搬运与真值，每条都能失败：

1. `enforced_fields` / `enforcement_source` 的真值表——逐字段回答"谁在执行这一项"，
   档位由注入的 Capability 决定（本机档位由 M0 探针与 v19 的行为测试负责证明）。
2. 沙箱关档时必须说"没强制"：`get_sandbox` 关档时仍返回 local，只按后端名推导就会在用户
   关掉沙箱之后继续撒谎。这条是 M1b 接线时实测到的漂移，不是设想的坑。
3. 网关白名单：`sandbox_enforcement` 只搬运那 16 个字段，多一个都不进事件——enforcement
   来自工具结果，工具结果可能被宿主路径或密钥污染。
4. 键名与面板读的键名逐字对齐（`web/src/components/tasks/api.ts` 的 limitKeys 映射）。
5. 整条图跑一遍：字段是 graph.py 的 emit 点写进事件的，不是单测里手抄的。
"""

from __future__ import annotations

import sys
import time

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.governance import digest, sandbox_boundary
from blank_api.services.sandbox import boundary, windows_job
from blank_api.services.sandbox.boundary import Capability
from blank_api.services.sandbox.runner import limits_not_applied
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition, ok
from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field

TASK_ID = "task-boundary-facts-1"
# 执行引擎自己交出来的那本账（LocalSandbox / limits_not_applied）。
ENGINE_KEYS = {
    "limits_applied", "boundary_strength", "boundary_strength_label",
    "memory_enforced", "memory_bytes", "pids_enforced", "pids_limit",
    "tree_kill_enforced", "filesystem_enforced", "network_enforced",
    "cpu_enforced", "disk_enforced", "job_created", "job_assigned", "job_error",
}
# M2：档位事实由 _tier_enforcement 合并进来，与后端名同源；事件里要能回答
# "当时按哪一档跑、降级没有、为什么"。
TIER_KEYS = {
    "sandbox_tier", "sandbox_tier_requested", "sandbox_tier_degraded",
    "tier_available", "tier_reason", "tier_notice",
}
# 事件里允许出现的字段：与 gateway._command_enforcement 的 keep 清单一致，
# 面板（api.ts / status.ts）只读这些，读不到别的东西。
EVENT_KEYS = ENGINE_KEYS | TIER_KEYS
ALL_LIMIT_FIELDS = {"memory", "pids", "tree_kill", "cpu", "disk", "network", "filesystem"}


def _cap(**overrides) -> Capability:
    """一封"看起来可信"的探测结果，按需拆掉某一项。默认形状 = os_enforced。"""
    fields = dict(
        platform=sys.platform,
        job=True,
        restricted_token=True,
        token_control=True,       # DuplicateTokenEx 对照
        appcontainer=True,
        carrier_control=True,     # 不带属性的同载体对照
        measured_at=time.time(),
    )
    fields.update(overrides)
    return Capability(**fields)


@pytest.fixture
def cap(monkeypatch):
    """把边界探测的结论钉成用例里写的那个形状。

    只动缓存、不动探针：`measure()` 的 TTL 分支就是给这种时刻用的——被测的是"拿到结论以后
    怎么记账"，不是"这台机器能不能建 Job"（后者由 v19 用真 Job 证明）。
    """
    def _install(capability: Capability) -> Capability:
        monkeypatch.setattr(boundary, "_CACHE", (time.time(), capability))
        return capability

    return _install


async def _seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-bf", name="边界事实"))
        session.add(Task(id=TASK_ID, project_id="project-bf", title="边界", goal="边界", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _context(root) -> ToolContext:
    return ToolContext(root=root, task_id=TASK_ID, session_factory=get_session_factory())


# --------------------------------------------------------------------- 真值表


def test_job_only_marks_three_of_seven(cap):
    """本机档位：内存/进程数/进程树被内核强制，CPU、磁盘、文件、网络都没有。

    把 job_only 当成"沙箱已启用"就是立项要消灭的那类谎，所以四项 False 逐条点名。
    """
    cap(_cap(appcontainer=False, restricted_token=False))
    enforced = boundary.enforced_fields("local")
    assert enforced["memory"] is True
    assert enforced["pids"] is True
    assert enforced["tree_kill"] is True
    assert enforced["cpu"] is False, "非管理员拿不到可靠的 Job CPU 限速：设计稿 §8 本期不做"
    assert enforced["disk"] is False
    assert enforced["network"] is False
    assert enforced["filesystem"] is False
    assert boundary.enforcement_source("local") == "measured_job_object"


def test_os_enforced_marks_the_six_it_really_covers(cap):
    """os_enforced 也不等于七项全亮：CPU 限速与磁盘配额本期不做，档位再高也是 False。

    这一条反过来钉 `enforced_fields`：把它写成"强度够就全 True"（docker 分支就是这么写的）
    会让面板在 AC 机器上点亮一个没有任何东西执行的 cpus。
    """
    cap(_cap())
    enforced = boundary.enforced_fields("local")
    assert set(enforced) == ALL_LIMIT_FIELDS
    assert enforced["cpu"] is False and enforced["disk"] is False
    assert {key: value for key, value in enforced.items() if key not in ("cpu", "disk")} == dict.fromkeys(
        ("memory", "pids", "tree_kill", "network", "filesystem"), True
    )
    assert boundary.enforcement_source("local") == "measured_appcontainer_or_restricted_token"


def test_no_job_means_no_limits_at_all(cap):
    """连 Job 都建不起来 ⇒ 强度落到 none ⇒ 三项也不能亮。"""
    cap(_cap(job=False, job_error="本平台无 Job Object", appcontainer=False, restricted_token=False))
    assert boundary.measure().strength == "none"
    assert boundary.enforced_fields("local") == boundary.no_limits()
    assert boundary.enforcement_source("local") == "nothing_enforced"


def test_failed_apparatus_forbids_every_claim(cap):
    """对照失败 ⇒ 结论只能是 unknown，且一项都不许承诺。

    M0 第一版探针漏挂 lpAttributeList 时 32 个属性号全部"起得来、没效果"：那种机器上
    任何 True 都是编的。宁可全红。
    """
    cap(_cap(carrier_control=False))
    assert boundary.measure().strength == "unknown"
    assert boundary.measure().apparatus_ok is False
    assert boundary.enforced_fields("local") == boundary.no_limits()
    assert boundary.enforcement_source("local") == "probe_apparatus_failed"


def test_disabled_sandbox_stops_claiming_limits(cap):
    """关档时 `get_sandbox` 照样返回 local，账上却必须全是 False。

    这是 M1b 接线时抓到的真实漂移：只按后端名推导，面板就会在用户关掉沙箱后继续
    显示"内存上限由内核强制"。反向也一样不许——把没强制的说成强制。
    """
    cap(_cap(appcontainer=False, restricted_token=False))
    enforced = boundary.enforced_fields("local", limits_enabled=False)
    assert enforced == boundary.no_limits()
    assert boundary.enforcement_source("local", limits_enabled=False) == "sandbox_disabled"
    # 同一个后端、开着的时候必须亮，证明 False 来自关档而不是来自后端名。
    assert boundary.enforced_fields("local", limits_enabled=True)["memory"] is True


def test_backend_none_is_not_measured_job(cap):
    """后端起不来时不许借用探测结论：没执行过就等于没强制。"""
    cap(_cap(appcontainer=False, restricted_token=False))
    assert boundary.enforced_fields("none") == boundary.no_limits()
    assert boundary.enforcement_source("none") == "no_backend"
    assert boundary.enforcement_source("") == "no_backend"


def test_docker_claims_are_labelled_as_flags(cap):
    """容器那条路的 True 来自 docker 命令行参数，不是本机实测：来源必须分开写。"""
    cap(_cap(job=False, appcontainer=False, carrier_control=False))
    assert all(boundary.enforced_fields("docker").values())
    assert boundary.enforcement_source("docker") == "docker_flags"


def test_panel_reads_exactly_these_field_names():
    """面板的 limitKeys 映射表在后端键名里必须逐个存在。

    `web/src/components/tasks/api.ts` 拿 cpus→cpu、tmpfs→filesystem 去查 enforced；
    后端改名的话每一项都会安静地长成"（未强制）"——数字还在，标记变了，没人报错。
    """
    enforced = boundary.no_limits()
    for panel_key, backend_key in (("cpus", "cpu"), ("memory", "memory"), ("pids", "pids"), ("tmpfs", "filesystem")):
        assert backend_key in enforced, f"面板按 {panel_key}→{backend_key} 查账，后端必须有这个键"
    assert set(enforced) == ALL_LIMIT_FIELDS, "多一项少一项都意味着面板与执行层口径分叉"


# ---------------------------------------------------------------- 搬运契约


def test_off_path_reports_the_same_key_set_as_on_path():
    """关档也得有一本账，而且键集合与执行层一致——读的一侧不用分两种形状。"""
    off = limits_not_applied("沙箱开关为关")
    assert ENGINE_KEYS <= set(off), "执行层那本账的键集合必须与开档一致"
    assert off["limits_applied"] is False
    assert off["memory_enforced"] is False and off["memory_bytes"] == 0
    assert off["pids_enforced"] is False and off["pids_limit"] == 0
    assert off["tree_kill_enforced"] is False
    assert off["filesystem_enforced"] is False and off["network_enforced"] is False
    assert off["cpu_enforced"] is False and off["disk_enforced"] is False
    assert "沙箱开关为关" in off["job_error"], "为什么没执行要写在账上，不能只留一串 False"


async def test_gateway_moves_only_whitelisted_facts(seeded, tmp_path):
    """假工具把宿主路径塞进 enforcement，也必须被白名单挡在事件外面。"""

    async def handler(context, arguments):
        return ok(
            "done",
            exit_code=0,
            sandbox="local",
            enforcement={
                "limits_applied": True,
                "boundary_strength": "job_only",
                "memory_enforced": True,
                "memory_bytes": 50331648,
                "cwd": r"C:\Users\someone\secret-project",
                "stdout": "sk-live-token-abcdef",
            },
        )

    tool = ToolDefinition(name="run_shell", description="d", input_schema={"type": "object"},
                          risk_level="low", handler=handler)
    payload = await ToolGateway([tool]).execute("run_shell", {"command": "whoami"}, _context(tmp_path), "act")
    field = sandbox_backend_event_field(payload)
    assert field["sandbox_backend"] == "local"
    facts = field["sandbox_enforcement"]
    assert facts["boundary_strength"] == "job_only"
    assert facts["memory_bytes"] == 50331648
    assert "cwd" not in facts and "stdout" not in facts
    assert set(facts) <= EVENT_KEYS
    assert "secret-project" not in str(field) and "sk-live-token" not in str(field)


def test_event_field_without_enforcement_carries_only_backend():
    """没带 enforcement 的调用（别的运行时写的记录）不许被补出一份假的。"""
    assert sandbox_backend_event_field({"sandbox_backend": "docker", "data": {}}) == {"sandbox_backend": "docker"}
    assert sandbox_backend_event_field({"data": {}}) == {}


# ------------------------------------------------------- 真路径：事件与证据包


async def test_host_run_records_limits_not_applied(seeded, workspace):
    """conftest 关掉了沙箱：这条命令直接跑在宿主上，账本必须自己说出来。"""
    payload = await ToolGateway(build_default_tools()).execute(
        "run_shell", {"command": "echo facts"}, _context(workspace), "act"
    )
    facts = payload["data"]["enforcement"]
    assert facts["limits_applied"] is False
    assert facts["memory_enforced"] is False
    assert sandbox_backend_event_field(payload)["sandbox_enforcement"]["limits_applied"] is False


async def test_events_and_evidence_carry_the_same_facts(seeded, workspace):
    """工具卡片与证据包读的是同一份事实：两处各写一遍迟早会漂。"""
    gateway = ToolGateway(build_default_tools())
    recorder = EventRecorder(get_session_factory(), TASK_ID)
    for name, arguments in (("run_shell", {"command": "echo hi"}), ("read_file", {"path": "README.md"})):
        payload = await gateway.execute(name, arguments, _context(workspace), "act")
        await recorder.add(
            "tool.completed",
            "act",
            {
                "tool": name,
                "status": payload.get("status"),
                "ok": payload.get("ok"),
                "tool_call_id": payload["tool_call_id"],
                **sandbox_backend_event_field(payload),
            },
        )
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    by_name = {row["name"]: row for row in evidence["tools"]}
    shell_facts = by_name["run_shell"]["sandbox_enforcement"]
    assert shell_facts["limits_applied"] is False
    assert set(shell_facts) <= EVENT_KEYS
    assert "sandbox_enforcement" not in by_name["read_file"], "只读工具没起进程，不许有一本假的账"


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object 只在 Windows 上存在")
async def test_windows_job_limits_reach_the_event(seeded, workspace, monkeypatch):
    """开着沙箱跑一条真命令：事件里的 True 来自现场真建起来的 Job，不是推导出来的。

    反向也钉：如果 LocalSandbox 忘了把 enforcement 交出来，这里会直接拿到空字典。
    """
    if not windows_job.supported():
        pytest.skip("本机建不出 Job：这条要钉的是 Job 的账")
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "1")
    payload = await ToolGateway(build_default_tools()).execute(
        "run_shell", {"command": "echo facts"}, _context(workspace), "act"
    )
    if payload["data"].get("sandbox") != "local":
        pytest.skip("本机拿到了别的沙箱后端，这条只钉 local")
    facts = payload["data"]["enforcement"]
    assert facts["limits_applied"] is True
    assert facts["job_created"] is True and facts["job_assigned"] is True
    assert facts["memory_enforced"] is True and facts["memory_bytes"] > 0
    assert facts["pids_enforced"] is True and facts["pids_limit"] > 0
    assert facts["tree_kill_enforced"] is True
    # 本期不做的两项即使在 Job 档位上也不能亮。
    assert facts["cpu_enforced"] is False and facts["disk_enforced"] is False
    assert facts["boundary_strength"] in {"job_only", "os_enforced"}
    assert set(sandbox_backend_event_field(payload)["sandbox_enforcement"]) <= EVENT_KEYS


async def test_governance_snapshot_uses_the_same_source(seeded, tmp_path, cap, monkeypatch):
    """治理快照里的 enforced 与事件里的 enforcement 走同一个函数，档位由现场决定。

    这条同时钉住面板那一段文案的输入：`sandbox.boundary_strength` 与 `sandbox.enforced`
    必须都在，缺一项前端就只能拿配置值当生效值显示。
    """
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "1")
    cap(_cap(appcontainer=False, restricted_token=False))
    block = sandbox_boundary()
    assert block["boundary_strength"] == "job_only"
    assert block["enforcement_source"] == "measured_job_object"
    assert block["enforced"]["memory"] is True
    assert block["enforced"]["cpu"] is False
    assert set(block["enforced"]) == ALL_LIMIT_FIELDS
    assert block["boundary_apparatus_ok"] is True
    assert isinstance(block["boundary_measured_seconds_ago"], (int, float))


def test_probe_age_is_shown_but_not_in_the_fingerprint(cap):
    """「距今多少秒」要能显示，但不许进治理指纹。

    这条是我自己 M1b 改动踩出来的缺陷：把 age_seconds 混进 sandbox 以后，同一套策略
    每隔几步就会算出不同的 digest，turn.context 的去重静默失效（表现为快照数暴涨，
    而面板上写着"策略变了"）。反向同样要能失败：策略真变了时指纹必须跟着变。
    """
    cap(_cap(appcontainer=False, restricted_token=False))
    block = sandbox_boundary()
    age = block["boundary_measured_seconds_ago"]
    assert isinstance(age, (int, float)), "年龄是给面板显示「这是什么时候测的」用的，不能没有"

    same_policy = {"sandbox": {**block, "boundary_measured_seconds_ago": age + 37.5}}
    assert digest({"sandbox": block}) == digest(same_policy), "探测缓存的年龄变了不等于策略变了"
    # 反向：只改 sandbox 里的一个策略字段，指纹必须动（证明上面那条不是把整块 sandbox 扔了）。
    changed = {"sandbox": {**block, "memory": "999m"}}
    assert digest({"sandbox": block}) != digest(changed)
    # 生效值翻一个方向就得算成"策略变了"——用取反而不是写死 True：关档时它本来就是 False。
    weaker = {"sandbox": {**block, "enforced": {**block["enforced"], "memory": not block["enforced"]["memory"]}}}
    assert digest({"sandbox": block}) != digest(weaker), "生效值变了就是策略变了"
