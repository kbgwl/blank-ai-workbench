"""工作包能不能独立验证。

判据来自「最小可验证」，不是「最细」：「最细」没有上界，会一路拆到"写一个 div"。
四个问题码：没产出物、没判据、两个包验同一件事、步骤空。

第一版**只提示不拦**——判据形态是新的写法，模型还没学会，一上来就打回会让它
为了过闸去凑一句废话。所以这些测试验的是"判得准"，不是"拦得住"。
"""

from __future__ import annotations

from blank_api.services.plan import normalize_steps, work_package_problems


def _pkg(step: str, output: str = "", check: dict | None = None) -> dict:
    item = {"step": step}
    if output:
        item["output"] = output
    if check is not None:
        item["check"] = check
    return item


def test_a_well_formed_package_has_no_problems():
    steps = normalize_steps(
        [
            _pkg("写三列骨架", "index.html", {"type": "path_exists", "path": "index.html"}),
            _pkg("补种子数据", "首屏三张卡片", {"type": "page", "path": "index.html", "text": ".task-item:3"}),
        ]
    )
    assert work_package_problems(steps) == []


def test_missing_output_and_check_are_both_reported():
    steps = normalize_steps([_pkg("实现整个功能")])

    codes = {item["code"] for item in work_package_problems(steps)}
    assert codes == {"WP_NO_OUTPUT", "WP_NO_CHECK"}
    # 人话里要带第几步，否则模型不知道改哪条。
    assert any("第 1 步" in item["message"] for item in work_package_problems(steps))


def test_two_packages_proving_the_same_thing_should_merge():
    """过碎的真实形态不是包多，是包多但判据重复。"""
    steps = normalize_steps(
        [
            _pkg("写首页", "index.html", {"type": "path_exists", "path": "index.html"}),
            _pkg("确认首页在", "index.html", {"type": "path_exists", "path": "index.html"}),
        ]
    )

    problems = work_package_problems(steps)
    dup = [item for item in problems if item["code"] == "WP_DUP_CHECK"]
    assert len(dup) == 1
    assert "第 1 步" in dup[0]["message"] and "第 2 步" in dup[0]["message"]


def test_different_assertions_on_the_same_page_are_not_duplicates():
    """同一张页面验两个不同选择器，是两件事，不该被判重复。"""
    steps = normalize_steps(
        [
            _pkg("三列在", "三列", {"type": "page", "path": "index.html", "text": ".task-list:3"}),
            _pkg("有卡片", "卡片", {"type": "page", "path": "index.html", "text": ".task-item:3"}),
        ]
    )
    assert work_package_problems(steps) == []


def test_empty_step_text_is_reported_not_crashed():
    problems = work_package_problems([{"id": "s1", "step": "  ", "status": "pending"}])
    assert [item["code"] for item in problems] == ["WP_NO_STEP"]


def test_output_and_check_survive_a_resubmit_that_only_changes_wording():
    """模型常常只改措辞并重发整份列表。判据不该因此被清空。"""
    first = normalize_steps(
        [_pkg("写三列骨架", "index.html", {"type": "path_exists", "path": "index.html"})]
    )
    again = normalize_steps([{"id": first[0]["id"], "step": "写三列骨架（含窄屏）"}], previous=first)

    assert again[0]["output"] == "index.html"
    assert again[0]["check"] == {"type": "path_exists", "path": "index.html"}
    assert work_package_problems(again) == []


def test_a_bare_string_step_is_not_reported_as_no_step():
    """老写法（纯字符串步骤）仍然合法，只是会被提示缺产出物与判据。"""
    steps = normalize_steps(["调研现有实现"])
    codes = [item["code"] for item in work_package_problems(steps)]
    assert "WP_NO_STEP" not in codes
    assert "WP_NO_CHECK" in codes
