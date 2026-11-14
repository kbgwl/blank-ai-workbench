"""#302 变异电池：逐条变异源码，验证每条判据「能失败」。

不是推理「应该红」，而是实测：每条变异打进去后跑测试，断言至少红一条，
且红的都是与该变异同源的判据。变异后恢复原样。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
PY = API / ".venv" / "Scripts" / "python.exe"
SRC_COMMANDS = API / "src" / "blank_api" / "services" / "commands_defs.py"
SRC_SKILLS = API / "src" / "blank_api" / "services" / "skills_defs.py"
TEST = "tests/test_command_allowed_tools_placeholders_v302.py"

# (label, file, old, new, 期望至少红的测试名片段列表)
MUTATIONS = [
    (
        "A_移除_tool_placeholders调用",
        SRC_COMMANDS,
        "    allowed_tools = _tool_placeholders(allowed_tools, None, project_root, plugin_root=plugin_root)",
        "    pass  # 变异：不展开占位符",
        ["project_dir_placeholder_expands", "plugin_root_placeholder_expands", "backslashes_in_project_dir"],
    ),
    (
        "B_命令误展开SKILL_DIR",
        SRC_COMMANDS,
        "_tool_placeholders(allowed_tools, None, project_root, plugin_root=plugin_root)",
        "_tool_placeholders(allowed_tools, path.parent, project_root, plugin_root=plugin_root)",
        ["skill_dir_placeholder_stays_literal"],
    ),
    (
        "C_移除PLUGIN_ROOT展开",
        SRC_SKILLS,
        "    if plugin_root is not None:\n        root = str(plugin_root).replace(\"\\\\\", \"/\")\n        expanded = [item.replace(\"${CLAUDE_PLUGIN_ROOT}\", root) for item in expanded]",
        "    pass  # 变异：不展开 PLUGIN_ROOT",
        ["plugin_root_placeholder_expands", "plugin_command_expands_both"],
    ),
    (
        "D_disallowed_tools也展开",
        SRC_COMMANDS,
        "    disallowed_tools, problem = _tool_list(raw_disallowed)\n    if problem:\n        warnings.append(f\"命令文件 {mask_path(str(path))} 的 disallowed-tools {problem}\")",
        "    disallowed_tools, problem = _tool_list(raw_disallowed)\n    if problem:\n        warnings.append(f\"命令文件 {mask_path(str(path))} 的 disallowed-tools {problem}\")\n    disallowed_tools = _tool_placeholders(disallowed_tools, None, project_root, plugin_root=plugin_root)",
        ["disallowed_tools_placeholders_are_not_expanded"],
    ),
    (
        "E_不归一反斜杠",
        SRC_SKILLS,
        '        project = str(project_root).replace("\\\\", "/")',
        "        project = str(project_root)",
        ["backslashes_in_project_dir_normalized"],
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
