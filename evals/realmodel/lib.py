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


# 要花钱的格子必须先钉住姿态：主格与编码格要钩子，对照格要**没有**钩子。
HOOK_CELLS = ("main", "encoding")


def premise_error(*, cells: list[str], reuse: dict[str, str], posture: dict | None) -> str:
    """花钱之前的姿态核对；返回非空串就是「这次别跑」。

    #120 第一次真跑就是栽在这里：启动器手拼的 `BLANK_HOOKS` 是坏 JSON，产品
    fail-closed 读成「没有钩子」，装置照常把三格跑完，11 条钩子判据全红——
    红得对，但那 11 条测的是姿态不是产品，钱白花。姿态未知（留痕文件不在）
    一律不跑：手起的实例不能假定它带了钩子。
    """
    fresh = [cell for cell in cells if not reuse.get(cell)]
    if not fresh:
        return ""
    got = (posture or {}).get("posture")
    if any(cell in HOOK_CELLS for cell in fresh) and got != "hooks":
        where = "留痕文件不在（实例是手起的？姿态未知）" if posture is None else f"读到姿态 {got!r}"
        return (f"要真跑 {fresh}，但 {where}——钩子判据在没有钩子的实例上必红，"
                f"那是白花钱。用 evals/realmodel/launch-instance.sh --with-hooks 起实例")
    if "control" in fresh:
        if posture is None:
            return f"对照格要真跑，但留痕文件不在，姿态未知（用 {POSTURE_HINT}）"
        if got == "hooks":
            return (f"对照格要在**没配钩子**的实例上真跑，当前姿态 {got!r}："
                    f"那条该红的判据红不了，整次点验只能作废（rc 2）")
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
