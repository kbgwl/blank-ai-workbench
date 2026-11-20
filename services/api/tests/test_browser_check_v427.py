"""browser_check：让模型自己看一眼页面。

存在的理由是一条真机轨迹：模型写完 3D 页面却无法验证，起 electron 连崩五次，
最后把"请你自己打开确认"写进交付。而页面本来是好的。

这里钉三件事：

1. **边界**：只能打开工作区内的文件与本机回环地址。外网、越界路径都要在
   进浏览器之前就被拒——拦在这里比拦在"模型被说服之后"便宜得多。
2. **结论的形状**：页面能开（ok）、控制台有没有错、断言过没过、截图在哪。
   四样缺一样，模型就又要去猜。
3. **坏页面不是坏工具**：有控制台错误时 `ok=False, status="failed"`，
   它该进修复回路，而不是被当成"工具报错"跳过。
"""

from __future__ import annotations

import shutil

import pytest

from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.browser import _browser_check, browser_tools, find_browser_hint, resolve_target

CLEAN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>干净的页面</title></head>
<body><canvas id="stage" width="320" height="200"></canvas><div class="card">ok</div>
<script>document.querySelector('.card').textContent = 'ready';</script>
</body></html>"""

BROKEN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>坏页面</title></head>
<body><div class="card">x</div>
<script>
  console.error('数据加载失败：接口 500');
  null.boom();
</script>
</body></html>"""


def _context(root) -> ToolContext:
    return ToolContext(root=root, task_id="t-browser", session_factory=None)


# ── 边界：不需要浏览器也能测 ───────────────────────────────────────────────


def test_workspace_file_becomes_a_file_url(workspace):
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(CLEAN_PAGE, encoding="utf-8")

    url, problem = resolve_target(workspace, "demo/index.html")

    assert problem == ""
    assert url.startswith("file:///") and url.endswith("demo/index.html")


def test_a_directory_opens_its_index(workspace):
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(CLEAN_PAGE, encoding="utf-8")

    url, problem = resolve_target(workspace, "demo")

    assert problem == "" and url.endswith("demo/index.html")


def test_paths_outside_the_workspace_are_refused(workspace):
    url, problem = resolve_target(workspace, "../secrets.txt")

    assert url == "" and "工作区" in problem


def test_missing_page_says_so(workspace):
    url, problem = resolve_target(workspace, "nope/index.html")

    assert url == "" and "不存在" in problem


def test_external_hosts_are_refused_with_a_way_out(workspace):
    url, problem = resolve_target(workspace, "https://example.com/login")

    assert url == ""
    assert "localhost" in problem, "拒绝时要给出路：告诉他只能看本机地址"
    assert "example.com" in problem


def test_loopback_is_allowed(workspace):
    url, problem = resolve_target(workspace, "http://127.0.0.1:3111/")

    assert problem == "" and url == "http://127.0.0.1:3111/"


def test_the_tool_is_registered_with_a_low_risk(capsys):
    tools = {tool.name: tool for tool in browser_tools()}

    assert "browser_check" in tools
    assert tools["browser_check"].risk_level == "low", "只读页面不该进审批，否则每次自检都卡一次"


# ── 真跑：需要本机有 Chrome/Edge ─────────────────────────────────────────


def _skip_without_browser():
    if not shutil.which("node") and not find_browser_hint():
        pytest.skip("本机没有 node 或浏览器，跳过浏览器实测")


def test_a_clean_page_passes_and_leaves_a_screenshot(workspace):
    _skip_without_browser()
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(CLEAN_PAGE, encoding="utf-8")

    import asyncio

    outcome = asyncio.run(
        _browser_check(_context(workspace), {"url": "demo/index.html", "expect": ["canvas:1", ".card:1"], "wait_ms": 300})
    )

    assert outcome.ok is True, outcome.message or outcome.output
    assert "控制台：没有任何错误" in outcome.output
    assert all(item["ok"] for item in outcome.data["asserts"])
    shot = workspace / outcome.data["screenshot"]
    assert shot.is_file() and shot.stat().st_size > 1000
    assert outcome.data["image"]["media_type"] in ("image/jpeg", "image/png")


def test_a_broken_page_is_a_failed_check_not_a_broken_tool(workspace):
    _skip_without_browser()
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(BROKEN_PAGE, encoding="utf-8")

    import asyncio

    outcome = asyncio.run(_browser_check(_context(workspace), {"url": "demo/index.html", "wait_ms": 300}))

    assert outcome.ok is False
    assert outcome.status == "failed", "页面报错是结论，不是工具故障；error 会让它被跳过"
    texts = " ".join(item["text"] for item in outcome.data["console"])
    assert "数据加载失败" in texts, "控制台错误必须原样带回来"


def test_a_missing_selector_reports_the_count(workspace):
    _skip_without_browser()
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(CLEAN_PAGE, encoding="utf-8")

    import asyncio

    outcome = asyncio.run(
        _browser_check(_context(workspace), {"url": "demo/index.html", "expect": ["table:2"], "wait_ms": 300})
    )

    assert outcome.ok is False
    assert outcome.data["asserts"][0] == {"selector": "table", "min": 2, "count": 0, "ok": False}


# ── 动作序列：验"用起来对不对"，不只"长得对不对" ─────────────────────────

INTERACTIVE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>可交互</title></head>
<body>
<button id="add" type="button">加一张</button>
<ul id="list"></ul>
<script>
  document.getElementById('add').addEventListener('click', function () {
    var li = document.createElement('li');
    li.className = 'card';
    li.textContent = '新卡片';
    document.getElementById('list').appendChild(li);
  });
</script>
</body></html>"""


def test_steps_prove_the_page_actually_works(workspace):
    """点一下再断言——这是"元素存在"证明不了的那部分。"""
    _skip_without_browser()
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(INTERACTIVE_PAGE, encoding="utf-8")

    import asyncio

    outcome = asyncio.run(
        _browser_check(
            _context(workspace),
            {
                "url": "demo/index.html",
                "wait_ms": 300,
                "steps": [
                    {"do": "assert", "text": "打开时是空的", "expr": "document.querySelectorAll('.card').length === 0"},
                    {"do": "click", "selector": "#add"},
                    {"do": "assert", "text": "点一下多一张", "expr": "document.querySelectorAll('.card').length", "min": 1},
                ],
            },
        )
    )

    assert outcome.ok is True, outcome.output
    assert [item["ok"] for item in outcome.data["steps"]] == [True, True, True]
    assert "点一下多一张" in outcome.output, "每条动作要带着它要证明的话回来，否则模型看不出哪一步塌了"
    # 附给模型看的必须是首屏：末态是它自己动出来的，证明不了"用户打开时看到什么"。
    assert outcome.data["screenshot"].endswith("-open.png")
    assert outcome.data["screenshot_end"].endswith(".png") and not outcome.data["screenshot_end"].endswith("-open.png")


def test_a_step_that_does_not_hold_fails_the_check(workspace):
    _skip_without_browser()
    (workspace / "demo").mkdir()
    (workspace / "demo" / "index.html").write_text(INTERACTIVE_PAGE, encoding="utf-8")

    import asyncio

    outcome = asyncio.run(
        _browser_check(
            _context(workspace),
            {
                "url": "demo/index.html",
                "wait_ms": 300,
                "steps": [
                    {"do": "assert", "text": "打开就该有卡片", "expr": "document.querySelectorAll('.card').length", "min": 1},
                ],
            },
        )
    )

    assert outcome.ok is False, "空壳页面必须判失败——空页面永远没有控制台错误"
    assert outcome.status == "failed"
    assert outcome.data["steps"][0]["ok"] is False
    assert outcome.data["steps"][0]["actual"] == 0
