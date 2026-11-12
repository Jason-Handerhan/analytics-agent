from datetime import datetime, timezone

from app.orchestrator.coding_tools import build_pull_request_body, build_task_detail, build_task_summary


def make_task(outcome=None, branch=None):
    """A stored task document, running when outcome is None and completed otherwise."""
    result = None
    if outcome:
        result = {
            "outcome": outcome, "stop_reason": None, "report": "Added the feature.",
            "files": [{"path": "definitions/a.sqlx", "added": 3, "removed": 1}],
            "checks": [{"name": "compile", "passed": False, "detail": "error at line 4"}],
            "review": {"ran": True, "verdict": "needs_revision", "explanation": "x",
                       "criteria": [{"criterion": "1. ok", "passed": True, "gap": None},
                                    {"criterion": "2. bad", "passed": False, "gap": "missing column"}],
                       "unverified": False},
            "assumptions": [], "blocking_questions": ["Which aisle?"], "follow_up_questions": [],
            "branch": branch, "commit": "abc", "dev_moved": False,
            "token_usage": {"agent": {"input": 1}}, "todos": [{"content": "step", "status": "done"}],
        }
    return {"task_id": "t1", "plan": {"name": "Aisle pacing", "goal": "g", "base_branch": "dev"},
            "status": "completed" if outcome else "running", "progress": "Working...", "coding_agent_result": result,
            "completed_at": datetime(2026, 11, 10, tzinfo=timezone.utc) if outcome else None, "pr_url": None}


def test_build_task_summary():
    """The list row carries file paths and the blocking questions only for a needs_input task, and no token usage."""
    needs_input = build_task_summary(make_task("needs_input"))
    assert needs_input["blocking_questions"] == ["Which aisle?"] and needs_input["files"] == ["definitions/a.sqlx"]
    succeeded = build_task_summary(make_task("succeeded", "agent/aisle-pacing-t1"))
    assert "blocking_questions" not in succeeded and succeeded["completed_at"].startswith("2026-11-10")
    assert "token_usage" not in succeeded and build_task_summary(make_task())["files"] == []


def test_build_task_detail():
    """A running task has only status and progress; a finished one adds the result with only the failed review
    criteria and a compare link from the plan's base branch, and leaves out todos and token usage."""
    assert set(build_task_detail(make_task())) == {"task_id", "name", "status", "progress"}
    detail = build_task_detail(make_task("failed", "agent/aisle-pacing-t1"))
    assert detail["review"]["failed_criteria"] == [{"criterion": "2. bad", "gap": "missing column"}]
    assert detail["compare_url"].endswith("/compare/dev...agent/aisle-pacing-t1")
    assert "todos" not in detail and "token_usage" not in detail
    assert "compare_url" not in build_task_detail(make_task("stopped"))


def test_build_pull_request_body():
    """The body states a failed check with its output and the reviewer's failed criteria, and leaves out the reviewer
    section when it did not run."""
    task = make_task("failed", "agent/aisle-pacing-t1")
    body = build_pull_request_body(task)
    assert "**Outcome:** failed, 0 of 1 checks passed" in body and "- FAILED: compile" in body and "error at line 4" in body
    assert "`definitions/a.sqlx` (+3 -1)" in body and "- 2. bad: missing column" in body
    assert "## Coding agent's report" in body and "Opened from coding task `t1`" in body
    task["coding_agent_result"]["review"]["ran"] = False
    assert "## Reviewer agent" not in build_pull_request_body(task)
