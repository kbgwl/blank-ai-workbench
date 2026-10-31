"""模型会发 Codex 那套 ``*** Begin Patch`` 包装，得接住。

实测原文（0.8.17 建站任务，5 次 apply_patch 失败里的 2 次）：

    *** Begin Patch
    *** Add File: index.html
    +<!DOCTYPE html>
    ...

解析器只认 ``@@``，看到第一行不是空也不是 ``@@`` 就直接报「补丁缺少 @@ 段落头」，
整条补丁作废、白烧一步。两种形态都要接：带 ``@@`` 的只剥壳；只有
``*** Add File:`` 的，按新建文件补出 ``---/+++/@@`` 头。

同时守住反向：**没有 ``***`` 的普通补丁一个字节都不能变**，现有能过的补丁行为不变。
"""

from __future__ import annotations

from blank_api.services.tools.patch import (
    apply_patch_text,
    infer_path,
    normalize_codex_patch,
    parse_hunks,
)

PLAIN = "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('more')\n"


def test_add_file_without_hunk_header_creates_the_file():
    patch = "*** Begin Patch\n*** Add File: index.html\n+<h1>hi</h1>\n+<p>x</p>\n*** End Patch"

    normalized = normalize_codex_patch(patch)

    assert apply_patch_text("", normalized) == "<h1>hi</h1>\n<p>x</p>"


def test_wrapper_around_a_hunk_is_unwrapped():
    patch = "*** Begin Patch\n@@ -0,0 +1,2 @@\n+a\n+b\n*** End Patch"

    assert [h["old_start"] for h in parse_hunks(normalize_codex_patch(patch))] == [0]


def test_update_file_header_keeps_the_hunks():
    patch = "*** Begin Patch\n*** Update File: main.py\n@@ -1,1 +1,2 @@\n+print('new')\n print('hi')\n*** End Patch"

    normalized = normalize_codex_patch(patch)

    assert "+print('new')" in normalized
    assert "***" not in normalized
    assert [h["old_start"] for h in parse_hunks(normalized)] == [1]


def test_plain_patch_is_returned_byte_for_byte():
    assert normalize_codex_patch(PLAIN) == PLAIN


def test_normalizing_twice_changes_nothing_the_second_time():
    once = normalize_codex_patch("*** Begin Patch\n@@ -0,0 +1,1 @@\n+a\n*** End Patch")

    assert normalize_codex_patch(once) == once


def test_empty_patch_survives():
    assert normalize_codex_patch("") == ""


def test_infer_path_reads_codex_file_header():
    patch = "*** Begin Patch\n*** Add File: src/app.py\n+x = 1\n*** End Patch"

    assert infer_path(patch) == "src/app.py"


def test_infer_path_still_reads_plain_headers():
    assert infer_path(PLAIN) == "main.py"
