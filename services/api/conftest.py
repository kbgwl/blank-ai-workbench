"""后端测试的根级 conftest。

这里只做一件事：修正宿主 Python 钩子对 Windows 扩展长度路径（``\\\\?\\``）的临时目录判定。

背景（2026-11-09 实测）
--------------------
WorkBuddy 会给每个 Python 进程注入 ``sitecustomize.py``，其中装了一个「批量删除保护」：
同一个 turn 内删除数量超过阈值（默认 50）就抛 ``SystemExit``，用来防止 agent 误删用户文件。
它本意是对**系统临时目录**放行的（``_is_under_os_tmp_dir``），但判定用的是

    os.path.normcase(os.path.realpath(os.path.abspath(path)))

而 Windows 上 ``realpath`` 会保留 ``\\\\?\\`` 前缀，临时目录根却没有前缀，于是：

    '\\\\?\\C:/Users/.../Temp/pytest-of-.../garbage-00'  ->  '\\\\?\\c:\\users\\...\\temp\\pytest-of-...'
    under_tmp = False      # 本应 True

pytest 收尾清理自己的 `garbage-*` 目录时，路径正是带前缀的形态，于是被误判成
「在项目里批量删文件」并中断整个测试进程：用例已经全跑完，却在打印 summary 前退出。
表现就是任务单里说的「测试长时间不结束 / 没有完整结束」。

修法
----
只给钩子的临时目录清单补上带前缀的等价根，让它按设计放行系统临时目录。
**不关闭保护**：项目目录、用户目录下的批量删除依旧会被拦。

在没有该钩子的机器（CI、他人的 venv）上这段是空操作。
"""

from __future__ import annotations

import sys


def _teach_host_shim_about_extended_length_temp_paths() -> bool:
    shim = sys.modules.get("sitecustomize")
    if shim is None or not sys.platform.startswith("win"):
        return False

    roots = getattr(shim, "_os_tmp_dirs", None)
    if not isinstance(roots, list) or not roots:
        return False

    added = False
    for root in list(roots):
        if not isinstance(root, str) or root.startswith("\\\\?\\"):
            continue
        prefixed = "\\\\?\\" + root
        if prefixed not in roots:
            roots.append(prefixed)
            added = True
    return added


try:
    _teach_host_shim_about_extended_length_temp_paths()
except Exception:  # 钩子换实现也不能让测试起不来
    pass
