from blank_api.services.tools.base import approval_key


def test_approval_key_binds_tool_and_arguments():
    first = approval_key("run_shell", {"command": "pnpm test"})
    assert first == approval_key("run_shell", {"command": "pnpm test"})
    assert first != approval_key("run_shell", {"command": "rm -rf ."})
    assert first != approval_key("apply_patch", {"command": "pnpm test"})
