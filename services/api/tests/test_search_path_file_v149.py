"""search_code 的 path 指向单个文件时，必须真去搜那个文件。

模型很爱写 ``search_code(pattern="</style>", path="index.html")``——在它的理解里
这是「在这个文件里搜」。但 ``iter_text_files`` 用 ``os.walk`` 遍历，对文件路径
一轮都不转，于是调用方拿到空列表、回一句「未找到匹配结果」。

危险的不是慢，是**静默**：模型判定「文件里确实没有」，接着换个锚点再搜一遍。
0.8.16 的建站实测里，第 12/13/14 步就是这么连着烧的（同一个 ``</style>``，
带 path 的两次都说没有，不带 path 的一次找到了）。
"""

from __future__ import annotations

from blank_api.services.tools.filesystem import _search_blocking
from blank_api.services.tools.paths import iter_text_files


def test_iter_text_files_accepts_single_file(workspace):
    got = list(iter_text_files(workspace, "main.py"))
    assert [name for name, _ in got] == ["main.py"]


def test_iter_text_files_single_file_keeps_nested_relative_path(workspace):
    got = list(iter_text_files(workspace, "src/core.py"))
    assert [name for name, _ in got] == ["src/core.py"]


def test_iter_text_files_still_walks_directory(workspace):
    """改的是「path 是文件」，目录的遍历口径不能被顺手改掉。"""
    names = sorted(name for name, _ in iter_text_files(workspace, ""))
    assert "main.py" in names and "src/core.py" in names
    # 忽略目录仍然生效（.git 里的 hidden 不该冒出来）
    assert all(not name.startswith(".git/") for name in names)


def test_search_blocking_honours_file_prefix(workspace):
    """本地分支真正用的就是它：带文件前缀与带目录前缀，命中的必须是同一行。"""
    by_dir = _search_blocking(workspace, "VALUE", None, "src", 50)
    by_file = _search_blocking(workspace, "VALUE", None, "src/core.py", 50)

    assert len(by_dir) == 1
    assert by_file == by_dir


def test_search_blocking_file_prefix_does_not_leak_siblings(workspace):
    """限定到一个文件时，不能把同目录其他文件的结果也带出来。"""
    (workspace / "src" / "other.py").write_text("VALUE = 2\n", encoding="utf-8")

    by_file = _search_blocking(workspace, "VALUE", None, "src/core.py", 50)
    assert [m["path"] for m in by_file] == ["src/core.py"]
