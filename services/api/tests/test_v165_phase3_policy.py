"""v165 Phase 3：后台设置从「摆设」变成被消费的口径。"""

from __future__ import annotations

from blank_api.services.agent.background_policy import (
    ENFORCE_ABANDON,
    ENFORCE_NONE,
    ENFORCE_NOTIFY,
    ENFORCE_REQUEUE,
    autonomous_enabled,
    budget_override,
    enforcement,
    recovery_limit,
    resolve_background,
)


def test_resolve_background_falls_back_to_safe_defaults():
    policy = resolve_background(None)
    assert policy["enabled"] is True
    assert policy["notify_policy"] == "approval_only"
    assert policy["max_steps"] == 200 and policy["max_seconds"] == 1800
    assert policy["auto_recoveries"] == 2
    assert policy["automation_mode"] == "assist"


def test_resolve_background_clamps_values_to_hard_limits():
    """设置只能把预算调小，不能调大——发布时钉死的成本闸门不能被配置文件绕过。"""
    policy = resolve_background(
        {
            "background": {
                "max_steps": 99999,
                "max_seconds": 999999,
                "token_budget": 999999999,
                "auto_recoveries": 99,
                "notify_policy": "made_up",
            }
        }
    )
    assert policy["max_steps"] == 200
    assert policy["max_seconds"] == 6 * 3600
    assert policy["token_budget"] == 1_500_000
    assert policy["auto_recoveries"] == 3
    assert policy["notify_policy"] == "approval_only"
    smaller = resolve_background({"background": {"max_steps": 8, "auto_recoveries": 0}})
    assert smaller["max_steps"] == 8 and smaller["auto_recoveries"] == 0


def test_budget_override_only_tightens():
    base = {"max_steps": 200, "max_seconds": 1800, "max_recoveries": 2, "token_budget": 1_500_000}
    policy = resolve_background(
        {"background": {"max_steps": 40, "max_seconds": 300, "auto_recoveries": 1, "token_budget": 200_000}}
    )
    out = budget_override(policy, base)
    assert out == {"max_steps": 40, "max_seconds": 300, "max_recoveries": 1, "token_budget": 200_000}
    # 意图给的小预算不能被设置「放大」。
    assert budget_override(policy, {"max_steps": 1, "max_seconds": 60, "max_recoveries": 0, "token_budget": 1000})[
        "max_steps"
    ] == 1
    assert base["max_steps"] == 200  # 不原地改


def test_recovery_limit_replaces_the_hardcoded_three():
    assert recovery_limit(resolve_background({"background": {"auto_recoveries": 1}})) == 1
    assert recovery_limit(resolve_background({"background": {"auto_recoveries": 99}})) == 3
    assert recovery_limit(None) == 2


def test_autonomous_loop_never_double_runs_with_an_external_worker():
    policy = resolve_background({"background": {"enabled": True}})
    assert autonomous_enabled(policy, inprocess=True) is True
    assert autonomous_enabled(policy, inprocess=False) is False
    assert autonomous_enabled(resolve_background({"background": {"enabled": False}}), inprocess=True) is False


def test_enforcement_maps_watchdog_alerts_to_safe_actions():
    assert enforcement("ok") == ENFORCE_NONE
    assert enforcement("diagnose") == "record"
    assert enforcement("notify") == ENFORCE_NOTIFY
    assert enforcement("pause") == ENFORCE_NOTIFY
    assert enforcement("release") == ENFORCE_REQUEUE
    assert enforcement("terminate") == ENFORCE_ABANDON
    assert enforcement("unknown") == "record"


# ── 接线层：这些测试盯的是「设置真的被业务读到了」，不是纯函数本身 ──────────


def test_recovery_attempt_limit_reads_settings_and_never_exceeds_the_hard_cap(monkeypatch, tmp_path):
    from blank_api.api.routes import tasks as task_routes
    from blank_api.services.settings import SettingsStore

    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    SettingsStore().write({"background": {"auto_recoveries": 1}})
    assert task_routes.recovery_attempt_limit() == 1
    SettingsStore().write({"background": {"auto_recoveries": 99}})
    assert task_routes.recovery_attempt_limit() == task_routes.MAX_RECOVERY_ATTEMPTS


def test_supervise_once_runs_all_three_jobs_and_survives_one_failure(monkeypatch):
    """后台巡检不能因为一条坏掉就让另外两条也停摆。"""
    import asyncio

    from blank_api import worker
    from blank_api.services.agent import watch_scan

    calls: list[str] = []

    async def ok(name: str):
        calls.append(name)
        return 1

    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "release_due_triggers", lambda: ok("triggers"))
    monkeypatch.setattr(watch_scan, "watch_once", lambda: ok("watch"))
    monkeypatch.setattr(worker, "inspect_runs", lambda: ok("alerts"))
    monkeypatch.setattr(worker, "expire_stale_approvals", broken)
    result = asyncio.run(worker.supervise_once())
    assert calls == ["triggers", "watch", "alerts"]
    assert result == {"triggers": 1, "watch": 1, "alerts": 1, "expired_approvals": 0}


def test_settings_normalize_keeps_automation_mode_and_rejects_dirty_values():
    from blank_api.services.settings import normalize

    assert normalize({"background": {"automation_mode": "autonomous"}})["background"]["automation_mode"] == "autonomous"
    assert normalize({"background": {"automation_mode": "bypassPermissions"}})["background"]["automation_mode"] == "assist"
    assert normalize({})["background"]["automation_mode"] == "assist"
