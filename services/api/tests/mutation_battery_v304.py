"""#304 变异电池：逐条变异源码，验证每条判据「能失败」。

不是推理「应该红」，而是实测：每条变异打进去后跑测试，断言至少红一条，
且红的都是与该变异同源的判据。变异后恢复原样。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
SRC_HOOKS = API / "src" / "blank_api" / "services" / "hooks.py"
SRC_RUNTIME = API / "src" / "blank_api" / "services" / "agent" / "runtime.py"
TEST = "tests/test_session_start_output_v304.py"

# (label, file, old, new, 期望至少红的测试名片段列表)
MUTATIONS = [
    # ---- 解析层：hooks.py 四字段各拔一条 ----
    (
        "A_移除sessionTitle解析",
        SRC_HOOKS,
        '    title = body.get("sessionTitle")\n    if not isinstance(title, str):\n        title = parsed.get("sessionTitle")\n    if isinstance(title, str) and title.strip():\n        body_out["session_title"] = title.strip()[:300]',
        "    pass  # 变异：不解析 sessionTitle",
        ["session_title", "parse_session_title"],
    ),
    (
        "B_移除initialUserMessage解析",
        SRC_HOOKS,
        '    init_msg = body.get("initialUserMessage")\n    if not isinstance(init_msg, str):\n        init_msg = parsed.get("initialUserMessage")\n    if isinstance(init_msg, str) and init_msg.strip():\n        body_out["initial_user_message"] = init_msg.strip()[:MAX_CONTEXT]',
        "    pass  # 变异：不解析 initialUserMessage",
        ["initial_user_message", "parse_initial_user_message"],
    ),
    (
        "C_移除watchPaths解析",
        SRC_HOOKS,
        '    watch = body.get("watchPaths")\n    if not isinstance(watch, list):\n        watch = parsed.get("watchPaths")\n    if isinstance(watch, list) and all(isinstance(item, str) for item in watch):\n        body_out["watch_paths"] = [item.strip() for item in watch if item.strip()]',
        "    pass  # 变异：不解析 watchPaths",
        ["watch_paths", "parse_watch_paths"],
    ),
    (
        "D_移除reloadSkills解析",
        SRC_HOOKS,
        '    reload_skills = body.get("reloadSkills")\n    if reload_skills is not True:\n        reload_skills = parsed.get("reloadSkills")\n    if reload_skills is True:\n        body_out["reload_skills"] = True',
        "    pass  # 变异：不解析 reloadSkills",
        ["reload_skills", "parse_reload_skills"],
    ),
    # ---- 消费层：runtime.py 四字段各拔一条 ----
    (
        "E_移除sessionTitle消费",
        SRC_RUNTIME,
        '        if session_title:\n            async with scope.store() as db:',
        '        if False:\n            async with scope.store() as db:',
        ["updates_thread_title"],
    ),
    (
        "F_移除initialUserMessage注入",
        SRC_RUNTIME,
        "                initial_user_message=initial_user_message,\n            ),",
        "            ),",
        ["prepends_initial_user_message"],
    ),
    (
        "G_移除watchPaths写盘",
        SRC_RUNTIME,
        '        if watch_paths:\n            watch_dir = Path(session.context.root) / ".blank" / "context"',
        '        if False:\n            watch_dir = Path(session.context.root) / ".blank" / "context"',
        ["writes_watch"],
    ),
    (
        "H_移除reloadSkills重扫",
        SRC_RUNTIME,
        '        if reload_skills:\n            root = Path(session.context.root)',
        '        if False:\n            root = Path(session.context.root)',
        ["reloads_skills"],
    ),
]


def _run_test() -> tuple[int, str]:
    result = subprocess.run(
        [str(PY), "-m", "pytest", TEST, "-v", "--tb=no", "-q"],
        cwd=str(API),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def _restore(path: Path, original: str) -> None:
    path.write_text(original, encoding="utf-8")


def main() -> int:
    failures = 0
    for label, path, old, new, expect in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"[SKIP] {label}：变异锚点没找到（源码可能已变）")
            failures += 1
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            rc, output = _run_test()
            if rc == 0:
                print(f"[FAIL] {label}：变异后测试全绿，这条判据是装饰")
                failures += 1
                continue
            red_names = [
                line.split("::")[-1].split()[0]
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            matched = any(
                any(fragment in name for name in red_names) for fragment in expect
            )
            if not matched:
                print(f"[FAIL] {label}：变异后红的测试与期望不同源")
                print(f"  期望至少红: {expect}")
                print(f"  实际红: {red_names}")
                failures += 1
            else:
                print(f"[OK] {label}：变异后如期红（{len(red_names)} 条）")
        finally:
            _restore(path, original)
    # 变异全部恢复后，测试应全绿
    rc, output = _run_test()
    if rc != 0:
        print(f"[FAIL] 变异恢复后测试仍红，恢复不干净")
        failures += 1
    else:
        print(f"[OK] 变异恢复后测试全绿（自检通过）")
    print(f"\n变异电池：{len(MUTATIONS) - failures}/{len(MUTATIONS)} 条变异如期红")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
