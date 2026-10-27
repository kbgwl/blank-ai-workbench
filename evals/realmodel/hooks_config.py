"""钩子配置的生成与自检：启动器只从这里取字符串，判据不许自己拼 JSON。

这一格是 #120 真跑里翻的车：`launch-instance.sh` 用 `printf` 手拼 `BLANK_HOOKS`，
命令里的引号没转义，产品侧 `HookRunner.from_env` 按 fail-closed 把整串读成
「没有钩子」。三格跑完，主格与编码格的钩子判据全红，钱花了、什么都没测到。
生成换成 `json.dumps`，并且把「过产品自己的解析器」钉成判据（见
`services/api/tests/test_realmodel_device_v120.py`）。

用法：

    python evals/realmodel/hooks_config.py --python <解释器> --dir <钩子目录> --print
    python evals/realmodel/hooks_config.py --python <解释器> --dir <钩子目录> --check

`--check` 退出码：0 = 姿态可用；2 = 配置或夹具坏了（启动器据此拒绝起实例）。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

# 三格点验要的三个事件；async 只允许挂在 post_tool_use 上
# （pre_tool_use 的 async 会被产品忽略，写在这儿等于教装置一个错的形状）
ASYNC_EVENTS = ("post_tool_use",)
SYNC_EVENTS = ("worktree_create", "worktree_remove")
FIXTURES = {
    "post_tool_use": "post_tool_use.py",
    "worktree_create": "worktree_create.py",
    "worktree_remove": "worktree_remove.py",
}


def command_of(python_exe: str, script: Path) -> str:
    """两个 token 各自带引号：路径里可能有空格，裸拼会被 shell 劈成三截。"""
    return f'"{Path(python_exe).as_posix()}" "{script.as_posix()}"'


def hooks_json(python_exe: str, hooks_dir: Path) -> str:
    specs: dict[str, list[dict]] = {}
    for event, name in FIXTURES.items():
        spec: dict = {"command": command_of(python_exe, hooks_dir / name)}
        if event in ASYNC_EVENTS:
            spec["async"] = True
        specs[event] = [spec]
    return json.dumps(specs, ensure_ascii=False)


def check(raw: str, python_exe: str, hooks_dir: Path) -> list[str]:
    """返回问题清单，空表就是能起。真解析器那一关在测试里，这里只管形状与夹具。"""
    problems: list[str] = []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [f"BLANK_HOOKS 不是合法 JSON：{exc}"]
    if not isinstance(parsed, dict):
        return [f"顶层要是对象，读到 {type(parsed).__name__}"]
    for event in (*ASYNC_EVENTS, *SYNC_EVENTS):
        specs = parsed.get(event)
        if not isinstance(specs, list) or not specs:
            problems.append(f"缺事件 {event}")
            continue
        for spec in specs:
            command = str(spec.get("command") or "")
            if not command:
                problems.append(f"{event} 有一条没有 command")
                continue
            try:
                tokens = shlex.split(command.replace("\\", "/"))
            except ValueError as exc:  # 引号没闭合就是这里露头
                problems.append(f"{event} 的 command 引号坏了：{exc} :: {command[:80]}")
                continue
            if len(tokens) != 2:
                problems.append(f"{event} 的 command 应是「解释器 + 脚本」两段，拆成 {len(tokens)} 段：{command[:80]}")
            elif not Path(tokens[1]).is_file():
                problems.append(f"{event} 指向的夹具不存在：{tokens[1]}")
        wanted_async = event in ASYNC_EVENTS
        if bool(specs[0].get("async")) is not wanted_async:
            problems.append(
                f"{event} 的 async 应是 {wanted_async}，实际 {specs[0].get('async')!r}")
    for event in parsed:
        if event not in FIXTURES:
            problems.append(f"多出一个装置不认识的事件 {event}（读数会漏判）")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--print", dest="do_print", action="store_true")
    ap.add_argument("--check", dest="do_check", action="store_true")
    args = ap.parse_args(argv)
    raw = hooks_json(args.python, args.dir)
    if args.do_print:
        print(raw)
    if args.do_check:
        problems = check(raw, args.python, args.dir)
        if problems:
            for line in problems:
                print(f"[ABORT] 钩子姿态坏了：{line}", file=sys.stderr)
            return 2
        print("[OK] 三个事件齐、async 只在 post_tool_use、夹具都在", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
