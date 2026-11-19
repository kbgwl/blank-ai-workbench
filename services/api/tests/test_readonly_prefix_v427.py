"""审批疲劳：只读命令该被记住，重定向不该被一句前缀顺带放行。

两件事在同一个函数里改的，所以放一起测：

1. **只读命令给前缀**（`dir` / `ls` / `cat`）。原先 `memo_rule` 只肯记"带子命令"的前缀，
   裸可执行名一律拒——理由是 `rm` / `bash` 的裸前缀等于全放行。但 `dir` 被这条规则
   一起误伤了：每列一次目录弹一张卡。真机 7 步 6 次审批里两次就是 `dir`。
2. **带重定向/管道的命令不给前缀**。这是顺手补的漏洞：`echo hi > out.txt` 的规则是
   `echo hi`，而前缀匹配只看前几个 token —— 同一条规则会一并免问
   `echo hi > secrets.txt`。内容一样、目标随便换，等于把"批准往这个文件写"
   放大成"批准往任何地方写"。
"""

from __future__ import annotations

from blank_api.services.permissions import (
    covered_by_rules,
    memo_rule,
    read_only_prefix,
    render_rule,
)


def test_read_only_commands_get_a_prefix_rule():
    for command, expected in (
        ("dir services\\api /b", ("dir",)),
        ("ls -la", ("ls",)),
        ("cat README.md", ("cat",)),
        ("pwd", ("pwd",)),
    ):
        assert memo_rule(command) == expected, command


def test_an_ordinary_bare_executable_stays_unmemorizable():
    """`rm` / `del` / `bash` 的裸前缀等于全放行，这条路不能被只读白名单带开。"""
    for command in ("rm -rf build", "del x.txt", "bash script.sh", "curl https://x.example"):
        assert memo_rule(command) == (), command


def test_redirects_and_pipes_are_never_memorized():
    """前缀盖不到重定向后面的目标，所以同一条规则不能免问"写到别处"。"""
    for command in (
        "echo hi > out.txt",
        "echo hi >> log.txt",
        "cat a.txt | grep x",
        "type a.txt > b.txt",
    ):
        assert memo_rule(command) == (), command


def test_the_redirect_hole_is_actually_closed():
    """这条是回归的靶心：修之前 `echo hi > out.txt` 会给出 `echo hi`，
    而那条规则命中 `echo hi > secrets.txt`。"""
    rule = memo_rule("echo hi > out.txt")
    assert rule == ()
    assert covered_by_rules("echo hi > secrets.txt", [("echo", "hi")]) is None, (
        "前缀规则不该覆盖带重定向的同类命令"
    )


def test_a_read_only_prefix_does_not_leak_into_a_chained_command():
    """`dir a && del b` 的第一段是只读的，但整条不是——必须逐段全中才免问。"""
    assert covered_by_rules("dir a /b && del b", [("dir",)]) is None
    assert covered_by_rules("dir a /b", [("dir",)]) is not None


def test_the_prefix_matches_only_the_same_executable():
    assert covered_by_rules("dir C:\\Windows", [("dir",)]) is not None
    assert covered_by_rules("direx a", [("dir",)]) is None


def test_read_only_prefix_requires_a_single_plain_segment():
    assert read_only_prefix("dir a && del b") == ()
    assert read_only_prefix("dir a > out.txt") == ()
    assert read_only_prefix("dir a") == ("dir",)
    assert render_rule(read_only_prefix("dir a")) == "dir"


def test_echo_without_redirect_is_still_memorizable():
    """别为了堵重定向把无害命令也一起堵死：`echo hi` 不动任何文件。"""
    assert memo_rule("echo hi") == ("echo", "hi")
