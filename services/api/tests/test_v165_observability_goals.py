"""v165 §15：活跃目标/后台任务/队列长度的同一个可观测读数。

这一层最容易骗人的是「孤儿」：v0.9.1 的任务本来就没有 supervisor 段，
把它们算进去会让读数永远是脏的，运维就再也不会相信这一页。
"""
from __future__ import annotations

from tests.test_v165_supervisor import _project, _thread, _turn  # noqa: F401 (复用建库夹具)

API = "/api/v1"


async def test_goals_endpoint_reports_empty_state_without_a_goal(client):
    response = await client.get(f"{API}/observability/goals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["goals"] == [] and body["background_tasks"] == []
    assert body["queue_length"] == 0
    assert body["worker"]["healthy"] is True and body["worker"]["stale_leases"] == 0
    assert body["orphans"] == 0


async def test_goals_endpoint_lists_active_goals_and_links_the_task_three_ways(client):
    project = await _project()
    thread = await _thread(project.id)
    turn = await _turn(client, thread.id, "给登录接口加上限流并跑测试")
    assert turn.status_code == 201, turn.text
    task_id = turn.json()["id"]

    body = (await client.get(f"{API}/observability/goals")).json()
    assert len(body["goals"]) == 1
    goal = body["goals"][0]
    assert goal["conversation_id"] == thread.id
    assert goal["current_task_id"] == task_id
    assert goal["automation_mode"] in ("manual", "assist", "autonomous")
    linked = [item for item in body["background_tasks"] if item["task_id"] == task_id]
    assert linked and linked[0]["goal_id"] == goal["goal_id"]
    assert linked[0]["conversation_id"] == thread.id
    assert body["orphans"] == 0
