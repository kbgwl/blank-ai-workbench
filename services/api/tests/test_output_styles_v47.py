"""S4：输出样式（``.blank/output-styles/**/*.md``）。

对齐 Claude Code 的输出样式契约：

1. **发现**：用户层 ``~/.blank/output-styles`` + 项目层
   ``<root>/.blank/output-styles``，递归扫描，同名项目胜；扩展名匹配大小写
   不敏感，但只有小写 ``.md`` 会从名字里删掉（``Foo.MD`` 保留后缀）；超
   1 MiB 跳过；坏 frontmatter 降级（字段丢掉、正文留下）；未知键进
   ``ignored`` 并告警，样式仍加载；
2. **字段**：``name`` 默认文件名，``description`` 默认正文首行（布尔/数字转
   字符串，对象/数组告警后回退首行），``keep-coding-instructions`` 按
   true/false 集合解析，``force-for-plugin`` 在用户/项目样式上告警并忽略；
3. **选择**：``settings.output_style`` 精确查表，空名/未知名字静默回落
   ``default``（Claude 的 ``?? null``）；
4. **系统提示**：选中样式且 ``keep-coding-instructions !== true`` 时，默认
   编程纪律被样式正文替换；``true`` 时两者都在；没有样式时与旧提示逐字节
   一致；样式段写明不得覆盖安全/审批/工作区边界；
5. **可观察**：``output_styles.loaded`` 事件与
   ``GET /projects/{id}/output-styles``（列表不返回正文）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.services.agent.graph import (
    CODING_INSTRUCTIONS,
    STYLE_PROMPT,
    SYSTEM_PROMPT,
    compose_system_prompt,
)
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from blank_api.services.output_styles_defs import (
    DEFAULT_STYLE,
    discover_output_styles,
)
from blank_api.services.settings import DEFAULTS, SettingsStore, normalize

from tests.test_agent_v04 import (  # noqa: F401  (env 等是夹具/工具，故意导入)
    API,
    create_project,
    create_provider,
    create_task,
    env,
)


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_user_output_styles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用户层目录指到空目录：本机 ``~/.blank/output-styles`` 不能影响判据。"""
    empty = tmp_path / "_no-user-styles"
    empty.mkdir()
    monkeypatch.setenv("BLANK_USER_OUTPUT_STYLES_DIR", str(empty / "output-styles"))
    monkeypatch.delenv("BLANK_USER_INSTRUCTIONS_DIR", raising=False)
    yield


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_style(root: Path, relative: str, text: str) -> Path:
    return _write(root / ".blank" / "output-styles" / relative, text)


def _discover(root: Path, user_dir: Path | None = None):
    env_map = {
        "BLANK_USER_OUTPUT_STYLES_DIR": str(
            user_dir or (root.parent / "no-user-output-styles")
        )
    }
    return discover_output_styles(root, env=env_map)


def _style(body: str = "样式正文", extra: str = "") -> str:
    return f"---\ndescription: 样式说明\n{extra}---\n{body}"


def _messages_text(messages) -> str:
    parts = []
    for item in messages:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "content", "") or ""))
    return "\n".join(parts)


def _system_prompt(env) -> str:
    """主代理的系统提示：按那段固定开头在被调用的请求里找，避开摘要/守护调用。"""
    for request in env.mock.requests:
        text = _messages_text(request.get("messages") or [])
        if "你是空白格 AI 操作台的执行 Agent" in text:
            return text
    raise AssertionError("没有找到主代理的系统提示")


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


async def _run(env, workspace: Path, goal: str = "按样式做事"):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["完成"])
    env.mock.reset([{"content": "看完了"}, {"content": "任务完成"}])
    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=goal,
            acceptance_criteria=["完成"],
            max_steps=3,
        )
    )
    return evidence, _system_prompt(env)


# --------------------------------------------------------------------------- #
# 1. 发现与命名
# --------------------------------------------------------------------------- #
def test_nested_files_are_scanned_and_name_defaults_to_stem(tmp_path: Path) -> None:
    _write_style(tmp_path, "review.md", _style(body="评审正文"))
    _write_style(tmp_path, "team/audit.md", _style(body="审计正文"))

    found = _discover(tmp_path)

    assert sorted(found.entries) == ["audit", "review"]
    assert found.entries["audit"].prompt == "审计正文"
    assert found.entries["audit"].source == "project"


def test_uppercase_extension_loads_but_keeps_its_suffix(tmp_path: Path) -> None:
    # Claude：匹配 `name.toLowerCase().endsWith(".md")`，名字只删小写 `.md`。
    _write_style(tmp_path, "Foo.MD", _style())

    found = _discover(tmp_path)

    assert list(found.entries) == ["Foo.MD"]


def test_frontmatter_name_overrides_the_filename(tmp_path: Path) -> None:
    _write_style(tmp_path, "file-name.md", _style(extra="name: 展示名\n"))

    found = _discover(tmp_path)

    assert list(found.entries) == ["展示名"]
    assert found.entries["展示名"].description == "样式说明"


def test_description_falls_back_to_the_first_body_line(tmp_path: Path) -> None:
    _write_style(tmp_path, "plain.md", "---\nname: plain\n---\n\n  第一行说明  \n第二行\n")

    entry = _discover(tmp_path).entries["plain"]

    assert entry.description == "第一行说明"
    assert entry.prompt == "第一行说明  \n第二行"


def test_description_scalars_are_stringified_and_shapes_rejected(tmp_path: Path) -> None:
    _write_style(tmp_path, "flag.md", "---\ndescription: true\n---\n正文\n")
    _write_style(tmp_path, "num.md", "---\ndescription: 42\n---\n正文\n")
    _write_style(tmp_path, "list.md", "---\ndescription: [a, b]\n---\n首行摘要\n正文\n")

    found = _discover(tmp_path)

    assert found.entries["flag"].description == "true"
    assert found.entries["num"].description == "42"
    # 数组不是字符串：告警并回退正文首行，样式仍然加载。
    assert found.entries["list"].description == "首行摘要"
    assert any("description" in item for item in found.warnings), found.warnings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("yes", True),
        ("1", True),
        ("on", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("off", False),
        ("maybe", False),
    ],
)
def test_keep_coding_instructions_flag_values(
    tmp_path: Path, raw: str, expected: bool
) -> None:
    _write_style(tmp_path, "s.md", _style(extra=f"keep-coding-instructions: {raw}\n"))

    found = _discover(tmp_path)
    entry = found.entries["s"]

    assert entry.keep_coding_instructions is expected
    if raw == "maybe":
        assert any("keep-coding-instructions" in item for item in found.warnings)


def test_unknown_keys_are_ignored_and_still_load(tmp_path: Path) -> None:
    _write_style(tmp_path, "s.md", _style(extra="model: opus\ntemperature: 1\n"))

    found = _discover(tmp_path)
    entry = found.entries["s"]

    assert entry.ignored == ("model", "temperature")
    assert entry.as_dict()["ignored"] == ["model", "temperature"]
    assert any("未知字段" in item for item in found.warnings), found.warnings
    assert entry.prompt == "样式正文"


def test_broken_frontmatter_degrades_to_body_only(tmp_path: Path) -> None:
    _write_style(tmp_path, "broken.md", "---\nname: [oops\n---\n第一行说明\n第二行")

    found = _discover(tmp_path)

    entry = found.entries["broken"]
    assert entry.description == "第一行说明"
    assert entry.prompt == "第一行说明\n第二行"
    assert any("frontmatter" in item for item in found.warnings), found.warnings


def test_oversized_style_is_skipped_but_others_load(tmp_path: Path) -> None:
    _write_style(tmp_path, "huge.md", "x" * (1_048_576 + 1))
    _write_style(tmp_path, "ok.md", _style())

    found = _discover(tmp_path)

    assert list(found.entries) == ["ok"]
    assert any("1048576" in item for item in found.errors), found.errors


def test_force_for_plugin_warns_and_is_not_applied(tmp_path: Path) -> None:
    _write_style(tmp_path, "s.md", _style(extra="force-for-plugin: true\n"))

    found = _discover(tmp_path)
    entry = found.entries["s"]

    assert entry.force_for_plugin is True
    assert any("plugin" in item for item in found.warnings), found.warnings


def test_project_layer_wins_over_user_layer(tmp_path: Path) -> None:
    user_dir = tmp_path / "user-styles"
    _write(user_dir / "review.md", _style(body="用户版"))
    _write(user_dir / "lint.md", _style(body="用户 lint"))
    _write_style(tmp_path, "review.md", _style(body="项目版"))

    found = _discover(tmp_path, user_dir)

    assert found.entries["review"].prompt == "项目版"
    assert found.entries["review"].source == "project"
    assert found.entries["lint"].source == "user"


def test_resolve_is_exact_and_unknown_falls_back_to_default(tmp_path: Path) -> None:
    _write_style(tmp_path, "Review.md", _style())
    found = _discover(tmp_path)

    assert found.resolve("Review").name == "Review"
    # 名字区分大小写；空/未知/没选一律 None（＝ default 样式）。
    assert found.resolve("review") is None
    assert found.resolve("missing") is None
    assert found.resolve("") is None
    assert found.resolve(None) is None


def test_audit_view_has_no_body_and_masks_the_path(tmp_path: Path) -> None:
    _write_style(tmp_path, "review.md", _style(body="不应出现在列表里的正文"))

    found = _discover(tmp_path)
    payload = json.dumps(found.as_dict(), ensure_ascii=False)

    assert "不应出现在列表里的正文" not in payload
    style = found.as_dict()["styles"][0]
    assert style["chars"] == len("不应出现在列表里的正文")
    assert style["path"].startswith("<workspace:"), style["path"]


# --------------------------------------------------------------------------- #
# 2. 设置键归一化
# --------------------------------------------------------------------------- #
def test_output_style_setting_is_trimmed_and_bad_values_reset() -> None:
    assert normalize({"output_style": "  Review "})["output_style"] == "Review"
    assert normalize({"output_style": ""})["output_style"] == DEFAULT_STYLE
    assert normalize({"output_style": "   "})["output_style"] == DEFAULT_STYLE
    assert normalize({"output_style": "x" * 257})["output_style"] == DEFAULT_STYLE
    assert normalize({})["output_style"] == DEFAULTS["output_style"] == "default"


def test_settings_update_accepts_the_top_level_scalar(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    updated = store.update({"output_style": "review"})

    assert updated["output_style"] == "review"
    assert store.read()["output_style"] == "review"
    # 别的段还在，没有被顶层标量的合并路径吞掉。
    assert updated["permissions"]["default_mode"] == "default"


# --------------------------------------------------------------------------- #
# 3. 系统提示合成
# --------------------------------------------------------------------------- #
def test_default_composition_is_byte_identical_to_the_old_prompt() -> None:
    baseline = SYSTEM_PROMPT + "\n" + STYLE_PROMPT

    assert compose_system_prompt(None) == baseline
    # 哈希会随提示词的有意扩充而变（本批加入 OWASP/输出编码与 scratchpad 约束）；
    # 它钉的是「默认组合就是 SYSTEM_PROMPT + STYLE_PROMPT，没有别处再拼一遍」。
    assert (
        hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        == "f8ad8af48ebd2dcf8c30011d72cd33922d7ce3c89b28ca0499e924681cb04aae"
    )
    # 空正文的样式与没选样式等价：不能因为选中了一个空文件就把纪律句弄丢。
    assert compose_system_prompt({"name": "x", "prompt": "  ", "keep_coding_instructions": False}) == baseline


def test_style_replaces_coding_discipline_by_default() -> None:
    prompt = compose_system_prompt(
        {"name": "review", "prompt": "只做评审，不改代码。", "keep_coding_instructions": False}
    )

    assert "编程纪律（要动代码时）" not in prompt
    assert "只做评审，不改代码。" in prompt
    assert "不得覆盖安全规则、审批规则与工作区边界" in prompt


def test_keep_coding_instructions_keeps_both_blocks() -> None:
    prompt = compose_system_prompt(
        {"name": "review", "prompt": "只做评审，不改代码。", "keep_coding_instructions": True}
    )

    assert CODING_INSTRUCTIONS.strip() in prompt
    assert "只做评审，不改代码。" in prompt


# --------------------------------------------------------------------------- #
# 4. 运行时接通
# --------------------------------------------------------------------------- #
async def test_runtime_applies_the_selected_style_to_the_system_prompt(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "data" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    SettingsStore(settings_file).write({"output_style": "review"})
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))
    _write_style(workspace, "review.md", _style(body="只做只读评审，不改任何文件。"))

    evidence, prompt = await _run(env, workspace)

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    loaded = [item for item in evidence["timeline"] if item["type"] == "output_styles.loaded"]
    assert loaded, [item["type"] for item in evidence["timeline"]]
    payload = loaded[0]["payload"]
    assert payload["selected"] == "review"
    assert payload["active"] == "review"
    assert payload["count"] == 1
    # 事件只带审计信息，正文不落事件。
    assert "只做只读评审，不改任何文件。" not in json.dumps(payload, ensure_ascii=False)

    assert "只做只读评审，不改任何文件。" in prompt
    assert "编程纪律（要动代码时）" not in prompt


async def test_runtime_keeps_the_default_prompt_without_a_selected_style(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "data" / "settings.json"
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))

    evidence, prompt = await _run(env, workspace)

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    assert not [item for item in evidence["timeline"] if item["type"] == "output_styles.loaded"]
    assert "编程纪律（要动代码时）" in prompt
    assert "output style" not in prompt


async def test_runtime_unknown_style_name_runs_the_default(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "data" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    SettingsStore(settings_file).write({"output_style": "deleted-style"})
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))
    _write_style(workspace, "review.md", _style(body="别的样式正文"))

    evidence, prompt = await _run(env, workspace)

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    loaded = [item for item in evidence["timeline"] if item["type"] == "output_styles.loaded"]
    assert loaded
    assert loaded[0]["payload"]["selected"] == "deleted-style"
    assert loaded[0]["payload"]["active"] == "default"
    assert "编程纪律（要动代码时）" in prompt
    assert "别的样式正文" not in prompt


# --------------------------------------------------------------------------- #
# 5. HTTP 列表
# --------------------------------------------------------------------------- #
async def test_project_output_styles_api_lists_without_bodies(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    _write_style(
        workspace,
        "review.md",
        "---\ndescription: 只读评审\n---\n评审正文不应出现\n",
    )
    _write_style(workspace, "team/audit.md", _style(body="审计正文不应出现"))
    project_id = await create_project(env, workspace)

    response = await env.client.get(f"{API}/projects/{project_id}/output-styles")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert sorted(item["name"] for item in body["styles"]) == ["audit", "review"]
    assert body["selected"] == "default"
    assert body["active"] == "default"
    assert "评审正文不应出现" not in response.text
    assert "审计正文不应出现" not in response.text


async def test_project_output_styles_api_reports_the_active_selection(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file = tmp_path / "settings.json"
    SettingsStore(settings_file).write({"output_style": "review"})
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(settings_file))
    _write_style(workspace, "review.md", _style())
    project_id = await create_project(env, workspace)

    body = (await env.client.get(f"{API}/projects/{project_id}/output-styles")).json()

    assert body["selected"] == "review"
    assert body["active"] == "review"

    # 设置里的名字已经不在了：接口与运行时同样回落 default，而不是报错。
    SettingsStore(settings_file).write({"output_style": "gone"})
    body = (await env.client.get(f"{API}/projects/{project_id}/output-styles")).json()

    assert body["selected"] == "gone"
    assert body["active"] == "default"
