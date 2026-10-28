"""真模型点验装置的公共层：前提自检、判定表、退出码纪律。

退出码抄 `evals/dialect_e2e.py` 的同一条纪律，装置不许自己说谎：

    0 = 判据全绿；**或**这一格今天没法判且如实念 [SKIP]（没给凭据/没起实例）
    1 = 判据红（产品问题）
    2 = 装置失效：给了却连不上、前提缺失却仍想判、对照格没红

对照格没红必须是 2 而不是 0：那说明主格那条判据在任何一次运行里都成立，
它没有测到任何东西，整次点验作废（见 docs/REA-WAVE5-CLAUDE-CODE.md §13）。

判据一律写成「事件流进、判定出」的纯函数，这样它能离线喂假数据自检，
不必每改一条判据就烧一次真模型的钱。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

EXIT_OK = 0
EXIT_RED = 1
EXIT_INCONCLUSIVE = 2

DEFAULT_BASE = "http://127.0.0.1:8061"
# 实例活着与否只探这一条：它同时回数据库状态（见 Client.reachable）
HEALTH_PATH = "/health"
POSTURE_HINT = "evals/realmodel/launch-instance.sh（要钩子姿态就加 --with-hooks）"
AGENT_ID_RE = re.compile(r"agent-[0-9a-f]{8}")
# #119 注入块的包装语，与 services/agent/graph.py:630 的文案逐字一致；
# 文案改了这里要跟着改，否则 floor 判据会静默变宽（装置自检 D7 钉住这一条）。
WAKE_WRAPPER = "后台钩子以退出码 2 结束："
# #123 提交事件的原文指纹只登记 sha256 前 12 位（`runtime.py::_prompt_sha`），
# 判据认这个形状——长度或字符集变了就说明账不是同一套，读数不能算对上。
SHA12_RE = re.compile(r"^[0-9a-f]{12}$")

# 一格 = 一份任务 id + 一台实例。上一批这张表是隐形的：任务 id 的变量名
# 在闸门与各格各写一遍（#120 的 M20），实例的 base 更是全装置只有一个，
# 于是「对照格在另一台没配钩子的实例上与主格同刻真跑」根本表达不出来。
# 变量名只在这里出现一次，其余一律查这张表。
CELLS: dict[str, dict[str, str]] = {
    "main": {"task": "RM_TASK_MAIN", "base": "RM_BASE_MAIN", "home": "RM_HOME_MAIN"},
    "encoding": {"task": "RM_TASK_ENC", "base": "RM_BASE_ENC", "home": "RM_HOME_ENC"},
    "control": {"task": "RM_TASK_CTL", "base": "RM_BASE_CTL", "home": "RM_HOME_CTL"},
}
# 要花钱的格子必须先钉住姿态：主格与编码格要钩子，对照格要**没有**钩子。
HOOK_CELLS = ("main", "encoding")


@dataclass
class Config:
    base: str
    home: Path
    provider_id: str

    @property
    def api(self) -> str:
        return f"{self.base}/api/v1"

    @property
    def port(self) -> str:
        return self.base.rsplit(":", 1)[-1].split("/")[0]

    @property
    def health_url(self) -> str:
        return f"{self.api}{HEALTH_PATH}"

    @property
    def posture_file(self) -> Path:
        """启动器留下的姿态文件（`launch-instance.sh` 写，装置花钱前读）。"""
        return self.home / f"instance-posture-{self.port}.json"

    def posture(self) -> dict | None:
        if not self.posture_file.is_file():
            return None
        try:
            raw = json.loads(self.posture_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"posture": "坏掉的留痕文件"}
        return raw if isinstance(raw, dict) else {"posture": "留痕文件不是对象"}

    @property
    def workspace(self) -> Path:
        return self.home / "ws-v119"

    @property
    def worktree_root(self) -> Path:
        return self.home / "worktrees"

    @property
    def hook_log(self) -> Path:
        return self.home / "hook-events.log"

    def target(self) -> "Target":
        return Target(base=self.base, port=self.port, posture=self.posture(),
                      posture_file=self.posture_file.name)


@dataclass
class Target:
    """一格实际要落在哪台实例上，以及那台实例自证的姿态。"""

    base: str
    port: str
    posture: dict | None
    posture_file: str = ""


def config_from_env() -> Config:
    """凭据与实例环境一律从外部注入，仓库里不落一个字节。

    `BLANK_REALMODEL_HOME` 指向的那份目录里有 settings.json 与 secret.key，
    它们**永远不进仓库**；装置只读路径，不读内容。
    """
    return Config(
        base=os.environ.get("BLANK_REALMODEL_BASE", DEFAULT_BASE).rstrip("/"),
        home=Path(os.environ.get("BLANK_REALMODEL_HOME", "E:/tmp/realmodel")),
        provider_id=os.environ.get("BLANK_REALMODEL_PROVIDER", "").strip(),
    )


def cell_config(cell: str) -> Config:
    """一格一台实例：`RM_BASE_<格>` / `RM_HOME_<格>` 覆盖全局，没给就沿用全局那台。

    对照格要的是**没配钩子**的实例，与主格不可能同为一台；上一批只有一个全局 base，
    所以它只能复用早前那一次的读数（文档 §12 记的就是这条欠账）。
    home 也要跟着分：两台实例共用一份 `run.db` 会互相写，`secret.key` 才需要复用同一把。
    """
    spec = CELLS[cell]
    base = os.environ.get(spec["base"], "").strip()
    home = os.environ.get(spec["home"], "").strip()
    root = config_from_env()
    return Config(
        base=(base or root.base).rstrip("/"),
        home=Path(home) if home else root.home,
        provider_id=root.provider_id,
    )


def reuse_map() -> dict[str, str]:
    return {cell: reused(cell) for cell in CELLS}


def reused(cell: str) -> str:
    """这一格要不要花钱：给了任务 id 就是复用早前那一次的读数。

    变量名只有 `CELLS` 这一处定义——闸门与各格都查它。上一版两套名字
    （`RM_TASK_CONTROL` / `RM_TASK_CTL`）把「复用对照格」判成装置失效。
    """
    return os.environ.get(CELLS[cell]["task"], "").strip()


class Client:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # 本机注册表里有系统代理，连 127.0.0.1 也会被交给它（见记忆 env-e-drive-toolchain）
        self.http = httpx.Client(timeout=120.0, trust_env=False)

    def reachable(self) -> bool:
        """探的是 `/api/v1/health`，不是裸 `/health`。

        裸 `/health` 在这个应用上返回 404，而 404 也算「有东西在听」——那等于
        端口上挂着任何一座别的服务器，装置都会认定实例在线，然后一路 404 崩栈。
        `/api/v1/health` 还顺带回了数据库状态，起没起、库连没连一目了然。
        """
        try:
            r = self.http.get(self.cfg.health_url)
            return r.status_code < 400
        except httpx.HTTPError:
            return False

    def post(self, path: str, payload: dict | None = None) -> dict:
        r = self.http.post(f"{self.cfg.api}{path}", json=payload or {})
        r.raise_for_status()
        return r.json()

    def get(self, path: str):
        r = self.http.get(f"{self.cfg.api}{path}")
        r.raise_for_status()
        return r.json()

    def events(self, task_id: str) -> list[dict]:
        return self.get(f"/tasks/{task_id}/events?limit=2000")

    def wait_terminal(self, task_id: str, budget: float) -> str:
        deadline = time.time() + budget
        while time.time() < deadline:
            status = self.get(f"/tasks/{task_id}")["status"]
            if status not in ("queued", "running"):
                return status
            time.sleep(5)
        return self.get(f"/tasks/{task_id}")["status"]


class Recorder:
    """判定表：每条都记「PASS/FAIL + 读到的实物」，红的名字进 fails。"""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.names: list[str] = []
        self.fails: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.names.append(name)
        self.reads.append(f"{'PASS' if ok else 'FAIL'} {name} :: {detail}")
        if not ok:
            self.fails.append(name)
        return bool(ok)

    def report(self, title: str) -> None:
        print(f"=== {title} ===")
        for line in self.reads:
            print(line)
        print(f"\n红 {len(self.fails)} / 判据 {len(self.reads)}")
        if self.fails:
            print("红集合：" + json.dumps(self.fails, ensure_ascii=False))


# ---------------------------------------------------------------- 花钱之前的姿态闸门


def premise_error(*, cells: list[str], reuse: dict[str, str],
                  targets: dict[str, Target]) -> str:
    """花钱之前按**每格自己那台实例**的自证核对姿态；返回非空串就是「这次别跑」。

    #120 第一次真跑栽在姿态上：启动器手拼的 `BLANK_HOOKS` 是坏 JSON，产品 fail-closed
    读成「没有钩子」，装置把三格跑完、11 条钩子判据全红，钱白花。那一版只会看
    全局那台实例的留痕，于是「主格在 :8061 带钩子、对照格在 :8062 不带钩子」这种
    同刻并跑根本没法表达——只能复用早前无钩子那一次的读数。现在按 base 分组：
    同一台既要领钩子格又要领对照格，直接拦（一份实例给不出两种姿态）。
    """
    fresh = [cell for cell in cells if not reuse.get(cell)]
    if not fresh:
        return ""
    groups: dict[str, list[str]] = {}
    for cell in fresh:
        groups.setdefault(targets[cell].base, []).append(cell)
    for base, group in groups.items():
        target = targets[group[0]]
        got = (target.posture or {}).get("posture")
        hook_cells = [cell for cell in group if cell in HOOK_CELLS]
        plain_cells = [cell for cell in group if cell not in HOOK_CELLS]
        if hook_cells and plain_cells:
            return (f"{base} 这一台同时要领钩子格 {hook_cells} 和对照格 {plain_cells}："
                    f"一份实例给不出两种姿态。对照格请用 {CELLS['control']['base']} "
                    f"指到另一台（{POSTURE_HINT}，不带 --with-hooks）")
        if hook_cells and got != "hooks":
            where = ("留痕文件不在（实例是手起的？姿态未知）"
                     if target.posture is None else f"读到姿态 {got!r}")
            return (f"要真跑 {hook_cells}，但 {base} {where}——钩子判据在没有钩子的实例上必红，"
                    f"那是白花钱。用 evals/realmodel/launch-instance.sh --with-hooks 起实例")
        if plain_cells:
            if target.posture is None:
                return (f"对照格 {plain_cells} 要真跑，但 {base} 的留痕文件不在，"
                        f"姿态未知（用 {POSTURE_HINT}）")
            if got != "no-hooks":
                # 只认启动器自证的那一个值：坏文件、半截配置、手起的实例都算"姿态未知"。
                # 对照格的读数靠的就是"这台确实没有钩子"，姿态不明时它红也不是证据、
                # 绿也不是证据，钱花了只换来一份没法引用的读数。
                return (f"对照格 {plain_cells} 要真跑，但 {base} 的留痕读到姿态 {got!r}"
                        f"（要的是 'no-hooks'）：那条该红的判据红不了，整次点验只能作废（rc 2）")
    return ""


def decide_exit_code(
    *,
    skip: str = "",
    abort: str = "",
    fails: list[str] | tuple[str, ...] = (),
    control_red: bool | None = None,
) -> int:
    """退出码的唯一真源——单测直接打这个函数，不靠真跑一次看颜色。"""
    if abort:
        return EXIT_INCONCLUSIVE
    if skip:
        return EXIT_OK
    if control_red is False:
        return EXIT_INCONCLUSIVE
    return EXIT_RED if fails else EXIT_OK


def print_gate(*, skip: str = "", abort: str = "", rc: int) -> None:
    if skip:
        print(f"[SKIP] 今天没法判：{skip}\n"
              f"       这不是通过。给了 BLANK_REALMODEL_PROVIDER 且实例在线才会真判。")
    if abort:
        print(f"[ABORT] 装置失效：{abort}\n        这也不是通过。")
    label = {EXIT_OK: "绿/如实跳过", EXIT_RED: "判据红", EXIT_INCONCLUSIVE: "没法判"}[rc]
    print(f"RC={rc}（{label}）")


# ---------------------------------------------------------------- 取数小工具
# 一律「键不在 → None，值为 0 → 0」，不许用 `or -1` 把真 0 读成"没说"。


def arg_of(payload: dict, key: str) -> str:
    """tool.requested 的 arguments 可能是 dict，也可能是模型原样传过来的 JSON 串。"""
    raw = payload.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return ""
    if not isinstance(raw, dict):
        return ""
    val = raw.get(key)
    return "" if val is None else str(val)


def int_or_none(obj: dict, key: str) -> int | None:
    if key not in obj:
        return None
    val = obj[key]
    return None if val is None else int(val)


def of_type(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("type") == name]


def by_tool(events: list[dict], kind: str, tool: str) -> list[dict]:
    return [e for e in of_type(events, kind) if e["payload"].get("tool") == tool]
