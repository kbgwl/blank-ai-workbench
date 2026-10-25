"""`pymysql<1.3` 这条上界买来的时间，今天量一次它到底在防什么。

来历（任务 #50、#79 之后补的声明）：aiomysql 0.3.2 在**模块顶层**
`from pymysql.converters import (... escape_bytes_prefixed ...)`，而 PyMySQL 从 1.2 起
把这个名字换成哨兵字符串 `"DO NOT IMPORT THIS!!!"`（上游 PyMySQL/PyMySQL#1274，
1.3.0 计划删除）。1.2.x 的形状由 `blank_api.db.ensure_mysql_driver_compat()` 兜住
（`tests/test_mysql_driver_compat_v31.py`），1.3.0 的形状兜不住——坏的是 import 本身。

这批要的不是"以后再说"，而是**现在就把到期那天量出来**：
- E4：上界确实写在册，而且写的是 1.3（有人放宽它，这里先红）；
- E1：当前发行版还没到期，判定用的是**发行元数据**；
- E3：把那个名字真删掉（子进程里，不动这台机器的环境），到期形状必须
  以我们点名的那句 ImportError 出现，而不是裸的 `cannot import name`；
- E2：为什么不能用 `pymysql.__version__` 判上界——1.2.3 把它硬写成 `"2.2.8"`
  （为通过 Django 的 mysqlclient 版本检查），按它判会把没到期的读成已过期。

E2 是一条会随上游改悔而红的哨兵：若哪天 `__version__` 与发行号一致了，它红并告诉你
"这条分歧已被上游修掉，可以退役；E1 的口径不要动"。
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from blank_api.db import PYMYSQL_UPPER_BOUND, ensure_mysql_driver_compat, pymysql_release_version

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
MYSQL_URL = "mysql+aiomysql://root@127.0.0.1:3307/x"

# CI 只跑 SQLite，`mysql` extra 根本没装。这几条量的是驱动自身的形状，
# 装不上就是"不适用"，不能报成错误——但也不许悄悄恒通过，所以跳过理由要点名缺谁。
try:
    import pymysql  # noqa: F401

    HAS_PYMYSQL = True
except ImportError:
    HAS_PYMYSQL = False

needs_pymysql = pytest.mark.skipif(
    not HAS_PYMYSQL,
    reason="本环境没装 PyMySQL（mysql extra 未安装），驱动上界判定不适用",
)

# 子进程里只做一件事：把别名删掉，再走一遍建引擎前那步兼容检查。
# 不写中文——这台机器上 argv 里的中文会按 ANSI 码页解释（见验证纪律）。
_PROBE = """
import sys

sys.path.insert(0, {src!r})
import pymysql.converters

if {delete}:
    del pymysql.converters.escape_bytes_prefixed

from blank_api.db import ensure_mysql_driver_compat

try:
    ensure_mysql_driver_compat({url!r})
except ImportError as error:
    print("IMPORT-ERROR", error, "CAUSE", type(error.__cause__).__name__, error.__cause__)
    sys.exit(0)
except Exception as error:  # 形状不对：不能是别的异常
    print("WRONG-EXCEPTION", type(error).__name__, error)
    sys.exit(2)
print("OK")
sys.exit(1)
"""


def _probe(delete_alias: bool) -> tuple[int, str]:
    src = str(PYPROJECT / "src")
    code = _PROBE.format(src=src, delete=delete_alias, url=MYSQL_URL)
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.returncode, done.stdout.strip()


def _mysql_requirements() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["project"]["optional-dependencies"]["mysql"])


def test_the_bound_is_declared_and_is_the_one_we_measured() -> None:
    """上界写在 pyproject 里，值就是 1.3；放宽或删掉它都该先在这里红。"""
    from packaging.requirements import Requirement
    from packaging.version import Version

    specs = [Requirement(item) for item in _mysql_requirements()]
    pymysql_spec = next((item for item in specs if item.name.lower() == "pymysql"), None)
    assert pymysql_spec is not None, f"mysql extra 里没有 pymysql：{_mysql_requirements()}"
    uppers = [c.operator + c.version for c in pymysql_spec.specifier if c.operator in ("<", "<=")]
    assert uppers == [f"<{PYMYSQL_UPPER_BOUND}"], f"上界不再是 {PYMYSQL_UPPER_BOUND}，实测到的到期形状要重新写：{uppers}"
    # 声明与常量同源：db 里那句点名文案用的就是这个上界
    assert Version(PYMYSQL_UPPER_BOUND).release == (1, 3)


@needs_pymysql
def test_the_installed_release_is_still_inside_the_bound() -> None:
    """当前发行版还没到期；装上 1.3 的那天这条自己会变红（判据读元数据，不读 __version__）。"""
    from importlib import metadata

    from packaging.version import Version

    release = pymysql_release_version()
    assert release is not None, "读不到 PyMySQL 发行号，上界判定不能凭猜"
    assert release == metadata.version("PyMySQL"), "发行号必须来自发行元数据"
    assert Version(release) < Version(PYMYSQL_UPPER_BOUND), (
        f"PyMySQL {release} 已到/超过上界 {PYMYSQL_UPPER_BOUND}：aiomysql 的 import 会直接坏，"
        "见 E3 的到期形状"
    )


@needs_pymysql
def test_the_dunder_version_is_not_the_release_number() -> None:
    """`pymysql.__version__` 是 1.2.3 里为了骗过 Django 检查写死的假号，别拿它判上界。"""
    import pymysql
    from packaging.version import Version

    release = pymysql_release_version()
    spoofed = str(pymysql.__version__)
    if Version(spoofed) == Version(release or "0"):
        pytest.fail(
            f"__version__ 与发行号现在一致了（都是 {release}）：上游撤掉了那身 Django 兼容伪装。"
            "这条哨兵可以退役（删掉本用例），但 pymysql_release_version() 的元数据口径保持不变。"
        )
    # 按假号判，未到期的环境会被读成已过期——这正是这条判据存在的理由
    assert Version(spoofed) >= Version(PYMYSQL_UPPER_BOUND), (
        f"假号 {spoofed} 如今不再越过 {PYMYSQL_UPPER_BOUND}，这条误判风险已消失，请退役本用例"
    )
    assert pymysql.VERSION_STRING == release


@needs_pymysql
def test_the_expiry_breaks_as_a_named_import_error() -> None:
    """真删别名（子进程，不碰本机环境）→ 必须是我们那句点名 ImportError，且因由是 import 失败。"""
    try:
        import aiomysql.connection  # noqa: F401
    except ImportError:
        pytest.skip("本机没有可用的 aiomysql，到期形状无从对照")

    # 对照：不删别名时这步是成功的（rc=1、打印 OK）。对照不成立就整次判定作废。
    control_rc, control_out = _probe(delete_alias=False)
    assert control_rc == 1 and control_out == "OK", f"对照格不成立（rc={control_rc} out={control_out!r}）"

    rc, out = _probe(delete_alias=True)
    assert rc == 0, f"到期形状没以点名的 ImportError 出现（rc={rc} out={out!r}）"
    assert "escape_bytes_prefixed" in out, out
    assert "已被上游删除" in out, out
    assert PYMYSQL_UPPER_BOUND in out, f"那句要点名在册上界：{out}"
    assert "IMPORT-ERROR" in out and "CAUSE ImportError" in out, f"因由必须是 aiomysql 的 import 失败：{out}"
