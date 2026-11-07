"""v165 P0：用户真机实测踩到的三处故障。

1. 审批卡上写「同意」，事实闸门却只认「就用这个/是的/对的/没错/可以」，
   用户答「同意」被当成给了一个叫"同意"的值，继续拦写 → 界面像没同意。
2. 设置里选「全放行」，非法档位被 SettingsStore.normalize 静默回落成
   default，界面还提示「已更新」→ 每条命令继续弹审批。
3. 工作区没有 pytest / package.json test 时，run_tests 报 error，整轮被判
   failed；用户看到「失败 / 测试未能执行」，误以为刚才那次批准没生效。
"""
from __future__ import annotations

from types import SimpleNamespace

from blank_api.services.agent.unknowns import decide
from blank_api.services.permissions import parse_decision
from blank_api.services.settings import SettingsStore


def _item() -> SimpleNamespace:
    return SimpleNamespace(key="company_phone", token="400-000-0000", label="电话")


def test_approval_words_release_the_fact_gate():
    for word in ("同意", "批准", "确认", "允许", "通过", "确定", "没问题", "就用这个", "是的", "可以", "好", "行", "OK", "yes"):
        assert decide(_item(), word) == "allow", word


def test_negations_are_never_treated_as_approval():
    # 「不同意」里含「同意」，「不行」里含「行」：按子串放行就刚好反了。
    for word in ("不同意", "不批准", "不通过", "不允许", "不可以", "不行", "不好", "不用", "不要", "留空"):
        assert decide(_item(), word) == "blank", word


def test_delegate_values_and_blanks_still_mean_what_they_meant():
    assert decide(_item(), "你看着办") == "delegate"
    assert decide(_item(), "13800000000") == "value"
    assert decide(_item(), "") == "none"
    assert decide(_item(), "我另给一个") == "none"


def test_parse_decision_accepts_chinese_approval_and_rejects_negation():
    assert parse_decision({"decision": "同意"})[0] is True
    assert parse_decision({"decision": "批准", "scope": "tool"}) == (True, "tool")
    assert parse_decision({"decision": "不同意"})[0] is False
    assert parse_decision({"decision": "什么鬼"})[0] is False


async def test_invalid_permission_mode_is_422_and_valid_bypass_sticks(client):
    # 设置文件是全会话共享的。必须按**原始字节**复位，不能用 PUT default 兜底：
    # PUT 会写一份完整 normalize 后的文件，凭空多出 guardian / background 等段，
    # 于是 Guardian.from_settings() 认为「设置页配过了」，不再回落 BLANK_GUARDIAN
    # 环境变量，后面的 guardian 用例集体丢掉 guardian.reviewed 事件（实测踩过）。
    store = SettingsStore()
    original = store.path.read_bytes() if store.path.exists() else None

    bad = await client.put(
        "/api/v1/settings/agent",
        json={"permissions": {"default_mode": "bypassPermissions"}},
    )
    assert bad.status_code == 422, bad.text

    try:
        good = await client.put(
            "/api/v1/settings/agent",
            json={"permissions": {"default_mode": "bypass"}},
        )
        assert good.status_code == 200, good.text
        assert good.json()["permissions"]["default_mode"] == "bypass"
        # 新字段也必须回显，否则前端「保存成功」是真的在骗人（同一个 P0）。
        assert good.json()["background"]["automation_mode"] == "assist"
        assert "autonomous" in good.json()["limits"]["automation_modes"]

        check = await client.get("/api/v1/settings/agent")
        assert check.status_code == 200
        assert check.json()["permissions"]["default_mode"] == "bypass"
    finally:
        # 留着 bypass 会让后面所有审批用例被静默放行；删文件则比原样写回更糟，
        # conftest 那份 safety 白名单会一起消失。
        if original is None:
            store.path.unlink(missing_ok=True)
        else:
            store.path.write_bytes(original)


async def test_automation_mode_round_trips_and_rejects_dirty_values(client):
    store = SettingsStore()
    original = store.path.read_bytes() if store.path.exists() else None
    try:
        bad = await client.put(
            "/api/v1/settings/agent",
            json={"background": {"automation_mode": "bypassPermissions"}},
        )
        assert bad.status_code == 422, bad.text

        good = await client.put(
            "/api/v1/settings/agent",
            json={"background": {"automation_mode": "autonomous"}},
        )
        assert good.status_code == 200, good.text
        assert good.json()["background"]["automation_mode"] == "autonomous"
        assert (await client.get("/api/v1/settings/agent")).json()["background"]["automation_mode"] == "autonomous"
    finally:
        if original is None:
            store.path.unlink(missing_ok=True)
        else:
            store.path.write_bytes(original)
