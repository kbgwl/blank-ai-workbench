from blank_api.api.routes.observability import _role


def test_observability_maps_agent_nodes_to_roles():
    assert _role("create_plan") == "planner"
    assert _role("execute_action") == "coder"
    assert _role("review_result") == "reviewer"
    assert _role("run_validation") == "tester"
