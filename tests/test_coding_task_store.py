from app.coding_task_store import build_coding_task_notification


def test_build_coding_task_notification():
    """A running task has no outcome or branch yet; a completed one carries both from its result."""
    running = {"task_id": "t1", "plan": {"name": "Aisle pacing"}, "status": "running", "progress": "The agent is working...",
               "coding_agent_result": None}
    done = {**running, "status": "completed",
            "coding_agent_result": {"outcome": "succeeded", "branch": "agent/aisle-pacing-t1"}}

    assert build_coding_task_notification(running) == {
        "task_id": "t1", "name": "Aisle pacing", "status": "running", "progress": "The agent is working...",
        "outcome": None, "branch": None}
    finished = build_coding_task_notification(done)
    assert finished["outcome"] == "succeeded" and finished["branch"] == "agent/aisle-pacing-t1"
