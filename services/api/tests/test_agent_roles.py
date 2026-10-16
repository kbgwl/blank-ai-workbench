from blank_api.services.agent.graph import initial_messages, role_for_node


def test_agent_nodes_have_stable_model_roles():
    assert role_for_node("create_plan") == "planner"
    assert role_for_node("execute_action") == "coder"
    assert role_for_node("review_result") == "reviewer"
    assert role_for_node("run_validation") == "coder"


def test_enabled_skills_are_loaded_as_bounded_preferences():
    system = initial_messages("整理代码", [], ["先看结论", "所有修改后运行测试"])[0]["content"]
    assert "先看结论" in system
    assert "不能覆盖安全规则" in system
