"""交付改动不该把 `.blank/scratchpad/` 里的自测脚本算进去（#388）。

真机复跑（任务 a5e7dda8）交付正文写「改动：5 个文件（index.html、styles.css、
app.js、.blank/scratchpad/selftest.js、.blank/scratchpad/servetest.js）」——
提示词明确说 scratchpad 不属于交付物，模型照做了，交付却把两个自测脚本算了进去。
用户数文件时会多出两个「我没要的东西」，而且 Diff 计数也被脚本的反复改写撑大。
"""

from __future__ import annotations

from blank_api.services.agent import graph


def _write(path: str, *, change_type: str = "created", hunks: int = 1) -> dict:
    diff = "".join(f"@@ -{i},1 +{i},1 @@\n-x\n+y\n" for i in range(hunks))
    return {
        "tool": "write_file",
        "status": "ok",
        "ok": True,
        "data": {"path": path, "change_type": change_type, "diff": diff},
        "output": "",
    }


def _state(*results: dict) -> dict:
    return {"tool_results": list(results)}


def test_scratchpad_helpers_are_not_counted_as_delivery_changes() -> None:
    state = _state(
        _write("index.html", hunks=2),
        _write(".blank/scratchpad/selftest.js", hunks=3),
        _write(".blank/scratchpad/servetest.js", hunks=1),
    )
    facts = graph._delivery_facts("结论\n- 记账页交付完成", state)
    assert facts.files == ("index.html",)
    assert facts.changed_files == 1
    # Diff 计数同口径：脚本的多次改写不能撑大「几处 Diff」
    assert facts.diff_count == 2


def test_windows_separators_and_nested_scratchpad_paths_are_excluded() -> None:
    state = _state(
        _write("app.js"),
        _write(".blank\\scratchpad\\selftest.js"),
        _write(".blank/scratchpad/nested/deep.js"),
    )
    facts = graph._delivery_facts("结论\n- 完成", state)
    assert facts.files == ("app.js",)


def test_blank_directory_files_outside_scratchpad_still_count() -> None:
    # 反向守卫：只排除 scratchpad 这个命名空间，不能顺手把 .blank 下别的真改动也吞掉。
    state = _state(
        _write("index.html"),
        _write(".blank/output-styles/custom.md"),
    )
    facts = graph._delivery_facts("结论\n- 完成", state)
    assert facts.files == ("index.html", ".blank/output-styles/custom.md")


def test_a_root_level_selftest_script_still_counts() -> None:
    # 反向守卫：按目录排除，不按文件名猜。用户点名的根目录脚本仍是交付物。
    state = _state(_write("_selftest.js"))
    facts = graph._delivery_facts("结论\n- 完成", state)
    assert facts.files == ("_selftest.js",)


def test_delivery_text_does_not_mention_scratchpad() -> None:
    state = _state(
        _write("index.html", hunks=2),
        _write("app.js", hunks=1),
        _write(".blank/scratchpad/selftest.js", hunks=5),
    )
    validation = {
        "status": "ok",
        "passed": True,
        "command": "node .blank\\scratchpad\\selftest.js",
        "exit_code": 0,
    }
    state["validation"] = validation

    class _Session:
        pass

    text = graph._delivery_text("结论\n- 记账页交付完成", state, _Session())
    assert "改动：2 个文件（index.html、app.js）" in text
    assert "改动：3 个文件" not in text
    # 验证命令来自校验记录，仍然原样保留：它确实跑过。
    assert "node .blank\\scratchpad\\selftest.js（退出码 0）" in text


def test_only_scratchpad_changes_do_not_produce_a_fake_change_line() -> None:
    # 只有自测脚本时没有可验收的改动：不能硬拼「改动：0 个文件」，退回原文。
    state = _state(_write(".blank/scratchpad/selftest.js"))
    text = graph._delivery_text("结论\n- 写好了自测脚本", state, object())
    assert text == "结论\n- 写好了自测脚本"
