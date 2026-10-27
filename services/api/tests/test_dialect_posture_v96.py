"""#96：方言装置必须自己钉住「这台机器靠什么隔离」，并在前提不成立时判「没法判」。

三条各自会走形的承诺，都来自 2026-10-26 那一跑实物（`E:/tmp/m87/device-sqlite-1.log`：
85 步红 26，任务一律停在 draft，成因是每条 `/run` 当场 409 `sandbox_tier_unavailable`）：

1. **不继承**：`boot()` 的 env 里那一项必须来自装置自己的旋钮，操作者机器上的
   `BLANK_SANDBOX_ENABLED` 不许漏进来。漏进来的那次，同一份代码换个 shell 就是两种结论。
2. **前提红在开场**：`available=False` 时整格判 2（今天没法判），不许退化成 26 个步骤红
   混进方言对照里——红在装配层，压根没走到"换库"那一层。
3. **成因要念出来**：守卫拿到非 2xx 必须当场报错并把响应体带上，不能再退化成
   「status=draft」这种只有现象、没有成因的一句话。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
DEVICE = ROOT / "evals" / "dialect_e2e.py"
SHELL = ROOT / "desktop" / "main.cjs"

REQUEST = httpx.Request("POST", "http://127.0.0.1:8071/api/v1/tasks/t/run")


def _load(monkeypatch, knob: str | None, caller_env: str | None) -> object:
    """按「操作者环境里有没有那一项、装置旋钮有没有给」重新加载一次装置模块。

    旋钮在模块顶部求值，所以必须重加载才量得准——同一次 import 里改 env 再读常量，
    读到的还是上一次的值（这类"读到自己写的旧值"的假绿在探针里翻过车）。
    两处 env 都交给 monkeypatch：漏一份就会把 "true" 带给同进程后面的用例。
    """
    for name in ("BLANK_SANDBOX_ENABLED", "BLANK_E2E_SANDBOX_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    if caller_env is not None:
        monkeypatch.setenv("BLANK_SANDBOX_ENABLED", caller_env)
    if knob is not None:
        monkeypatch.setenv("BLANK_E2E_SANDBOX_ENABLED", knob)
    spec = importlib.util.spec_from_file_location(f"dialect_e2e_{abs(hash((knob, caller_env)))}", DEVICE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resp(status: int, body: object) -> httpx.Response:
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return httpx.Response(status, text=text, request=REQUEST)


class _FakePopen:
    """只接住 `boot()` 真正调用 Popen 的那一次，把 env 交回给测试。"""

    def __init__(self, holder: dict) -> None:
        self.holder = holder

    def __call__(self, cmd, cwd=None, env=None, stdout=None, stderr=None):  # noqa: ANN001
        self.holder["cmd"] = list(cmd)
        self.holder["env"] = dict(env or {})

        class _Proc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        return _Proc()


class _HealthHTTP:
    """替掉装置模块里的 `httpx` 引用：`boot()` 只用 `httpx.get(...).json()["database"]`。"""

    def get(self, url, timeout=None, **kwargs):  # noqa: ANN001, ARG002
        class _R:
            def json(self):
                return {"database": "connected"}

        return _R()


def _booted_env(device, monkeypatch, tmp_path: Path) -> dict:
    holder: dict = {}
    # `boot()` 还读 `subprocess.STDOUT`：假模块少一个属性就红在装置自己手上，量不到产品。
    stub = type("S", (), {"Popen": _FakePopen(holder), "STDOUT": -1})
    monkeypatch.setattr(device, "subprocess", stub)
    monkeypatch.setattr(device, "httpx", _HealthHTTP())
    device.boot("sqlite+aiosqlite:///:memory:", tmp_path)
    return holder["env"]


def test_the_device_default_is_the_desktop_shells_posture(monkeypatch, tmp_path: Path) -> None:
    """操作者设了沙箱开、装置旋钮没给 ⇒ 装置仍按桌面壳的默认（关）起实例。

    这一条就是那 26 步红的正面判据：把 `boot()` 里那一项删掉，继承来的 "true" 会漏进去，
    这里立刻红。
    """
    device = _load(monkeypatch, knob=None, caller_env="true")
    env = _booted_env(device, monkeypatch, tmp_path)
    assert device.DEVICE_SANDBOX_ENABLED == "false", device.DEVICE_SANDBOX_ENABLED
    assert env["BLANK_SANDBOX_ENABLED"] == "false", env["BLANK_SANDBOX_ENABLED"]
    # 与桌面壳同源：`main.cjs` 的默认哪天改成开，这一格必须跟着红，让两个人坐下来对齐一次。
    shell = SHELL.read_text(encoding="utf-8")
    assert 'BLANK_SANDBOX_ENABLED: process.env.BLANK_SANDBOX_ENABLED ?? "false"' in shell, shell[:400]


def test_the_device_knob_can_still_arm_the_tight_cell(monkeypatch, tmp_path: Path) -> None:
    """旋钮必须真能收紧：验收「无 docker 就整任务拒绝」那一格要靠它，不能是死代码。"""
    device = _load(monkeypatch, knob="true", caller_env=None)
    env = _booted_env(device, monkeypatch, tmp_path)
    assert device.DEVICE_SANDBOX_ENABLED == "true"
    assert env["BLANK_SANDBOX_ENABLED"] == "true"


def test_the_posture_gate_reds_before_the_first_step(monkeypatch) -> None:
    """前提不成立时在开场红，并把旋钮与成因一起说清楚。"""
    device = _load(monkeypatch, knob="true", caller_env=None)
    calls: list[str] = []
    drive = device.Drive("http://127.0.0.1:1/api/v1")
    unavailable = {
        "backend": {
            "backend": "none", "isolation": "none", "tier": "workspace", "strength": "none",
            "docker_available": False, "available": False,
            "reason": "sandbox_backend_unavailable", "notice": "沙箱后端不可用：命令被拒绝，不会退回宿主执行",
        }
    }
    drive.c = type("C", (), {"get": lambda self, url, **k: calls.append(url) or _resp(200, unavailable)})()
    with pytest.raises(device.PostureMissing) as caught:
        device.check_posture(drive)
    text = str(caught.value)
    assert "sandbox_backend_unavailable" in text, text
    assert "BLANK_E2E_SANDBOX_ENABLED" in text, text
    assert calls == ["/settings/safety/runtime"], calls


def test_the_posture_gate_passes_when_the_boundary_exists(monkeypatch) -> None:
    """对照格（要能绿）：available=True 时开场自检不许拦。

    少了这一格，自检就退化成「永远红」——那种装置什么都证明不了。
    """
    device = _load(monkeypatch, knob=None, caller_env="true")
    drive = device.Drive("http://127.0.0.1:1/api/v1")
    ok = {"backend": {"backend": "local", "isolation": "none", "tier": "off", "strength": "none",
                      "docker_available": False, "available": True, "reason": "sandbox_disabled",
                      "notice": "部署策略关闭了沙箱"}}
    drive.c = type("C", (), {"get": lambda self, url, **k: _resp(200, ok)})()
    assert device.check_posture(drive) == ok["backend"]


def test_a_refused_run_is_read_as_a_cause_not_a_symptom(monkeypatch) -> None:
    """409 要当场带着响应体报错；200 那份正常路径不能被这条守卫改坏。"""
    device = _load(monkeypatch, knob=None, caller_env="true")

    def drive_for(table: dict[str, httpx.Response]):
        drive = device.Drive("http://127.0.0.1:1/api/v1")
        drive.state["task"] = "t"
        drive.state["provider"] = "prov"
        drive.c = type("C", (), {
            "post": lambda self, url, json=None, timeout=None: table[f"POST {url}"],
            "get": lambda self, url, timeout=None: table[f"GET {url}"],
        })()
        return drive

    refused = drive_for({"POST /tasks/t/run": _resp(
        409, '{"error":{"code":"sandbox_tier_unavailable","message":"沙箱后端不可用：命令被拒绝，不会退回宿主执行"}}')})
    with pytest.raises(AssertionError) as caught:
        device.run_task_with_flake_guard(refused, "p")
    text = str(caught.value)
    assert "/run 没被受理" in text and "sandbox_tier_unavailable" in text, text
    assert "status=draft" not in text, text

    green = drive_for({
        "POST /tasks/t/run": _resp(200, {"status": "succeeded"}),
        "GET /tasks/t": _resp(200, {"status": "succeeded"}),
    })
    assert device.run_task_with_flake_guard(green, "p") == "status=succeeded"


def test_the_control_cell_has_to_be_present_for_any_verdict(monkeypatch) -> None:
    """判据要求每格都在场且全绿：对照格没跑成 ⇒ 2（今天没法判），不是 0 也不是 1。"""
    device = _load(monkeypatch, knob=None, caller_env="true")
    rc, lines = device.verdict({"mysql": {"19": "PASS"}}, {"sqlite": "姿态自检没过"})
    assert rc == 2 and any("姿态自检没过" in x for x in lines), lines
    # 对照格在场、只有它一格全绿时才是"只有一格"（0 + 那句实话），两种诚实不许混。
    rc, lines = device.verdict({"sqlite": {"19": "PASS"}})
    assert rc == 0 and any("只有一格" in x for x in lines), lines
