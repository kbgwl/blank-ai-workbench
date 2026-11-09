"""受限令牌后端的规则文件：三条纪律每一条都要能失败。

规则文件不是"配置项"，是文件边界的一部分：
- 默认拒绝写成 allow 必须报错，否则一份手滑的配置就把边界关掉了；
- 任何授权根包含受保护路径（如 `~/.ssh`）必须让整份规则作废，而不是"跳过那一条"；
- 认不出的字段/占位符必须报错，拼错 `protected` 等于悄悄少一层保护。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from blank_api.services.sandbox import rules as rules_module
from blank_api.services.sandbox.rules import RulesError, load


def _base(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "capability_sid": "logon",
        "default_action": "deny",
        "write_allow": ["{workspace}"],
        "read_allow": ["{python_base}", "{appdata}/npm"],
        "protected": ["{home}/.ssh", "{home}/.aws"],
    }
    payload.update(overrides)
    return payload


def _write(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_shipped_rules_are_deny_by_default_and_protect_ssh() -> None:
    rule = load()
    assert rule.default_action == "deny"
    assert rule.source.endswith("rules.default.json")
    ctx = rule.context(str(Path.cwd()))
    protected = {os.path.normcase(path) for path in rule.protected_paths(ctx)}
    assert os.path.normcase(str(Path(ctx["home"]) / ".ssh")) in protected
    assert str(Path(ctx["workspace"])) in rule.write_roots(ctx)


def test_default_action_allow_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="default_action"):
        load(_write(tmp_path, _base(default_action="allow")))


def test_unknown_field_is_refused(tmp_path: Path) -> None:
    """拼错 protected_paths 会让保护项被静默忽略，所以未知字段必须报错。"""
    with pytest.raises(RulesError, match="认不出的字段"):
        load(_write(tmp_path, _base(protected_paths=["{home}/.ssh"])))


def test_unknown_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="版本"):
        load(_write(tmp_path, _base(version=2)))


@pytest.mark.parametrize("sid", ["Users", "Everyone", "S-1-1-0"])
def test_capability_sid_must_be_logon_or_a_logon_scoped_sid(tmp_path: Path, sid: str) -> None:
    with pytest.raises(RulesError, match="capability_sid"):
        load(_write(tmp_path, _base(capability_sid=sid)))


def test_capability_sid_accepts_explicit_logon_scoped_sid(tmp_path: Path) -> None:
    rule = load(_write(tmp_path, _base(capability_sid="S-1-5-5-0-123456")))
    assert rule.capability_sid == "S-1-5-5-0-123456"


def test_granting_home_is_refused_because_it_covers_ssh(tmp_path: Path) -> None:
    rule = load(_write(tmp_path, _base(write_allow=["{home}"])))
    ctx = rule.context(str(tmp_path))
    with pytest.raises(RulesError, match="受保护路径"):
        rule.guard(rule.write_roots(ctx), ctx)


def test_read_grant_covering_ssh_is_refused_too(tmp_path: Path) -> None:
    rule = load(_write(tmp_path, _base(read_allow=["{home}"])))
    ctx = rule.context(str(tmp_path))
    with pytest.raises(RulesError, match="受保护路径"):
        rule.guard(rule.read_roots(ctx), ctx)


def test_workspace_is_always_writable_even_if_config_omits_it(tmp_path: Path) -> None:
    rule = load(_write(tmp_path, _base(write_allow=[])))
    ctx = rule.context(str(tmp_path))
    assert rule.write_roots(ctx) == (os.path.abspath(str(tmp_path)),)


def test_empty_placeholder_drops_the_rule_instead_of_becoming_a_drive_root() -> None:
    """APPDATA 为空时 `{appdata}/npm` 必须整条作废，不能退化成盘根下的 `\\npm`。"""
    assert rules_module._expand("{appdata}/npm", {"appdata": ""}) == ""
    full = rules_module._expand("{appdata}/npm", {"appdata": r"C:\Users\x\AppData\Roaming"})
    assert full.endswith(os.path.join("AppData", "Roaming", "npm"))


def test_unknown_placeholder_is_an_error() -> None:
    with pytest.raises(RulesError, match="不认识的占位符"):
        rules_module._expand("{protcted}/.ssh", {"home": r"C:\Users\x"})


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="不存在"):
        load(str(tmp_path / "nope.json"))


def test_missing_packaged_file_falls_back_to_builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules_module, "_RULE_FILE", tmp_path / "gone.json")
    rule = load()
    assert rule.source == "builtin"
    assert rule.default_action == "deny"
    assert "{home}/.ssh" in rule.protected


def test_non_string_allow_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="字符串数组"):
        load(_write(tmp_path, _base(write_allow="{workspace}")))
