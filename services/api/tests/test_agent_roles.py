from blank_api.services.agent.graph import role_for_node


def test_agent_nodes_have_stable_model_roles():
    assert role_for_node("create_plan") == "planner"
    assert role_for_node("execute_action") == "coder"
    assert role_for_node("review_result") == "reviewer"
    assert role_for_node("run_validation") == "coder"
