"""分层指令（AGENTS.md）装配：加载、去重、排序与预算裁剪。

`services/instructions.py` 是仓库里唯一能改变 Agent 行为、又不需要改代码的入口，
所以它的每一条承诺都要有断言兜住：缺失文件不算错误、越靠近任务的层越强、
预算不够时先裁弱层。
"""

from pathlib import Path

from blank_api.services.agent.graph import initial_messages
from blank_api.services.instructions import (
    FILE_CHAR_LIMIT,
    LAYER_HARNESS,
    LAYER_PROJECT,
    LAYER_TASK,
    LAYER_USER,
    compose,
    load_layers,
    provenance,
)


def _env(tmp_path: Path) -> dict[str, str]:
    """隔离用户级目录：测试不能受开发机 ~/.blank/AGENTS.md 影响。"""
    return {"BLANK_USER_INSTRUCTIONS_DIR": str(tmp_path / "no-user-dir")}


def test_missing_files_are_not_an_error(tmp_path: Path) -> None:
    assert load_layers(tmp_path, env=_env(tmp_path)) == []
    assert compose("BASE", []) == "BASE"


def test_project_layer_is_loaded_from_workspace(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目约定：先读再改\n", encoding="utf-8")
    layers = load_layers(tmp_path, env=_env(tmp_path))
    assert [item.source for item in layers] == [LAYER_PROJECT]
    assert "先读再改" in layers[0].content
    assert provenance(layers)[0]["truncated"] is False


def test_user_and_project_layers_are_ordered_weak_to_strong(tmp_path: Path) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    (user_dir / "AGENTS.md").write_text("用户偏好\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("项目约定\n", encoding="utf-8")

    layers = load_layers(
        tmp_path,
        extra=["本次任务要求\n"],
        env={"BLANK_USER_INSTRUCTIONS_DIR": str(user_dir)},
    )
    assert [item.source for item in layers] == [LAYER_USER, LAYER_PROJECT, LAYER_TASK]

    composed = compose("BASE", layers)
    assert composed.startswith("BASE")
    assert composed.index("用户偏好") < composed.index("项目约定") < composed.index("本次任务要求")
    # 排序说明必须同时出现在提示里，否则模型无从判断冲突该听谁的
    assert "任务 > 项目 > 用户" in composed


def test_identical_layers_are_collapsed(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("同一份内容\n", encoding="utf-8")
    layers = load_layers(tmp_path, extra=["同一份内容"], env=_env(tmp_path))
    assert [item.source for item in layers] == [LAYER_PROJECT]


def test_oversized_file_is_clipped_with_a_marker(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * (FILE_CHAR_LIMIT + 500), encoding="utf-8")
    layer = load_layers(tmp_path, env=_env(tmp_path))[0]
    assert provenance([layer])[0]["truncated"] is True
    assert "已截断" in layer.content
    assert len(layer.content) <= FILE_CHAR_LIMIT + 100


def test_budget_prefers_strong_layers(tmp_path: Path) -> None:
    """总预算被压到很小时，被牺牲的必须是用户层而不是任务层。"""
    (tmp_path / "AGENTS.md").write_text("P" * 5_000, encoding="utf-8")
    layers = load_layers(tmp_path, extra=["T" * 5_000], env=_env(tmp_path))
    composed = compose("BASE", layers, total_limit=1_200)
    assert "T" * 500 in composed
    assert "P" * 500 not in composed


def test_initial_messages_inject_layers_into_system_prompt(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目约定：改代码前先读\n", encoding="utf-8")
    layers = load_layers(tmp_path, env=_env(tmp_path))
    messages = initial_messages("目标", ["条件"], None, layers)

    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "改代码前先读" in system
    assert messages[-1]["role"] == "user"
    assert "目标" in messages[-1]["content"]


def test_initial_messages_without_layers_is_stable() -> None:
    """没有 AGENTS.md 时不能凭空多出内容，否则缓存前缀会随环境漂移。"""
    with_none = initial_messages("目标", [])
    with_empty = initial_messages("目标", [], None, [])
    assert with_none == with_empty
    assert with_none[0]["content"].startswith("你是空白格")


def test_harness_layer_sits_between_project_and_task(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目约定\n", encoding="utf-8")
    harness = tmp_path / ".blank" / "harness"
    harness.mkdir(parents=True)
    (harness / "systemprompt.md").write_text("harness 约定\n", encoding="utf-8")

    layers = load_layers(tmp_path, extra=["任务要求\n"], env=_env(tmp_path))
    assert [item.source for item in layers] == [LAYER_PROJECT, LAYER_HARNESS, LAYER_TASK]

    composed = compose("BASE", layers)
    assert composed.index("项目约定") < composed.index("harness 约定") < composed.index("任务要求")
    assert "任务 > harness > 项目 > 用户" in composed


def test_harness_on_demand_content_stays_on_disk(tmp_path: Path) -> None:
    harness = tmp_path / ".blank" / "harness"
    (harness / "skills" / "pdf").mkdir(parents=True)
    (harness / "systemprompt.md").write_text("先读再改\n", encoding="utf-8")
    (harness / "skills" / "pdf" / "SKILL.md").write_text(
        "---\ndescription: 处理 PDF\n---\n技能正文\n", encoding="utf-8"
    )

    layer = load_layers(tmp_path, env=_env(tmp_path))[0]
    assert "技能正文" not in layer.content
    assert "skills/pdf/SKILL.md" in layer.content


def test_broken_harness_does_not_break_loading(tmp_path: Path) -> None:
    """半写坏的文件只让 harness 这一层缺席，不能拖垮整次任务。"""
    (tmp_path / "AGENTS.md").write_text("项目约定\n", encoding="utf-8")
    harness = tmp_path / ".blank" / "harness"
    harness.mkdir(parents=True)
    (harness / "systemprompt.md").write_bytes(b"\xff\xfe\x00broken")

    layers = load_layers(tmp_path, env=_env(tmp_path))
    assert [item.source for item in layers] == [LAYER_PROJECT]