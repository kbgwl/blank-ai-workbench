"""第四种判据：`page`——用户打开页面时看见什么。

为什么必须加这一维：前三类判据（文件在不在 / 文件里有没有那段字 / 命令过没过）
全部可以被一个**空壳**满足。真实案例是 L6 那个任务看板：三条验收都过了
（拖拽真派事件验了、localStorage 真写三条验了、无控制台错误），交到用户手里
是三个空框，第一句话是「就做了个这个」。判据里没有任何一条问过「打开时有什么」。

`page` 不自己起浏览器——verifier 是纯函数，它只读 `browser_check` 已经留下的回执。
三条纪律沿用：没跑成是 unknown（不是 failed）；跑过但没断言这个选择器也是 unknown
（它验了别的，不等于验了这条）；第一版不 blocking。
"""

from __future__ import annotations

import pathlib

from blank_api.services.agent.verifier import verify
from blank_api.services.plan import _clean_check, normalize_goal

PAGE = "deliverables/demo/index.html"


def _goal(selector: str = ".task-item", minimum: int = 3) -> dict:
    return normalize_goal(
        {
            "objective": "做一个任务看板",
            "done_when": [
                {
                    "text": "打开时三列里已经有卡片",
                    "check": {"type": "page", "path": PAGE, "text": f"{selector}:{minimum}"},
                }
            ],
        }
    )


def _browser_row(count: int, ok: bool, selector: str = ".task-item") -> dict:
    return {
        "tool": "browser_check",
        "ok": ok,
        "status": "ok" if ok else "failed",
        "data": {
            "url": "file:///D:/kbg%20harness/night-shift/workspace/deliverables/demo/index.html",
            "title": "任务看板",
            "asserts": [{"selector": selector, "min": 3, "count": count, "ok": ok}],
            "steps": [],
        },
    }


def _verify(goal: dict, rows: list[dict]) -> dict:
    return verify(
        goal_spec=goal,
        tool_results=rows,
        changed_paths=[PAGE],
        report="已完成。",
        root=pathlib.Path("."),
    )


def test_clean_check_accepts_page_only_with_a_selector():
    kept = _clean_check({"type": "page", "path": PAGE, "text": ".task-item:3"})
    assert kept == {"type": "page", "path": PAGE, "selector": ".task-item", "min": 3}

    # 没写选择器 = 「打开了但没要求」，这种判据天然通过，不能当判据。
    assert _clean_check({"type": "page", "path": PAGE}) == {}
    # 没写页面同样不成立。
    assert _clean_check({"type": "page", "text": ".task-item:3"}) == {}
    # 不写个数按 1；伪类里的冒号不该被当成个数。
    assert _clean_check({"type": "page", "path": PAGE, "text": ".task-item"})["min"] == 1
    assert _clean_check({"type": "page", "path": PAGE, "text": ".task-item:hover"}) == {
        "type": "page",
        "path": PAGE,
        "selector": ".task-item:hover",
        "min": 1,
    }


def test_an_empty_page_fails_even_though_the_file_exists(tmp_path):
    """这就是「就做了个这个」：文件在、能跑，但打开时没有东西。"""
    (tmp_path / "deliverables" / "demo").mkdir(parents=True)
    (tmp_path / "deliverables" / "demo" / "index.html").write_text(
        "<html><body><div class='col'></div></body></html>", encoding="utf-8"
    )

    result = verify(
        goal_spec=_goal(),
        tool_results=[_browser_row(count=0, ok=False)],
        changed_paths=[PAGE],
        report="已完成。",
        root=tmp_path,
    )
    item = result["evidence"]["done_when"][0]

    assert item["status"] == "failed"
    assert "0 个" in item["evidence"] and "≥3" in item["evidence"]
    assert result["checks"]["done_when"] is False
    assert result["blocking"] is False, "判据是模型写的，第一版不该据此判死"


def test_a_seeded_page_passes(tmp_path):
    """同一份代码补了种子数据，判据就该转绿——否则这个闸门只是「怎么都红」。"""
    result = _verify(_goal(), [_browser_row(count=3, ok=True)])
    item = result["evidence"]["done_when"][0]

    assert item["status"] == "passed"
    assert "3 个" in item["evidence"]
    assert result["checks"]["done_when"] is True


def test_never_opened_is_unknown_not_failed(tmp_path):
    result = verify(
        goal_spec=_goal(), tool_results=[], changed_paths=[PAGE], report="已完成。", root=tmp_path
    )
    item = result["evidence"]["done_when"][0]

    assert item["status"] == "unknown", "没验成不是没做到"
    assert "没打开过" in item["evidence"]
    assert result["checks"].get("done_when") is None


def test_opened_without_asserting_this_selector_is_unknown(tmp_path):
    """它验了别的选择器，不等于验了这条。"""
    result = _verify(_goal(selector=".task-item"), [_browser_row(count=9, ok=True, selector=".board")])
    item = result["evidence"]["done_when"][0]

    assert item["status"] == "unknown"
    assert "没有断言" in item["evidence"]


def test_page_matches_a_relative_path_against_a_file_url():
    """判据写相对路径、回执给 file:/// 绝对地址（还是百分号编码过的），两边要能对上。"""
    result = _verify(_goal(), [_browser_row(count=3, ok=True)])
    assert result["evidence"]["done_when"][0]["status"] == "passed"

    # 盘符/工作区不同、但文件名一致：仍算同一张页面（一次任务通常只有一张 index.html）。
    other_root = dict(_browser_row(count=3, ok=True))
    other_root["data"] = dict(other_root["data"], url="file:///C:/another/place/deliverables/demo/index.html")
    assert _verify(_goal(), [other_root])["evidence"]["done_when"][0]["status"] == "passed"

    # 换了另一张页面就不该混。
    wrong = dict(_browser_row(count=3, ok=True))
    wrong["data"] = dict(wrong["data"], url="file:///C:/x/deliverables/demo/other.html")
    assert _verify(_goal(), [wrong])["evidence"]["done_when"][0]["status"] == "unknown"
