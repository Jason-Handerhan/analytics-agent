from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.coding_task_models import CodingAgentPlan
from app.markdown_display_formatting import build_pull_request_body
from app.orchestrator.coding_tools import build_task_detail, build_task_summary, save_coding_plan


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
    return {"task_id": "t1", "plan": {"name": "Aisle pacing", "goal": "g", "base_branch": "dev", "requirements": ["Add the column", "Expose it"]},
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
    assert "## Requirements\n1. Add the column\n2. Expose it" in body
    task["coding_agent_result"]["review"]["ran"] = False
    assert "## Reviewer agent" not in build_pull_request_body(task)


PLAN_FIELDS = dict(name="Aisle pacing", goal="Add a feature.", scope="Add one column.", description="Add the column.")


def test_dashboard_only_plan_fields():
    """new_pages and audience belong to a dashboard plan only, and a dashboard plan must say how many pages it adds."""
    CodingAgentPlan(subagent_type="code", **PLAN_FIELDS)
    CodingAgentPlan(subagent_type="dashboard", new_pages=0, audience="The ML team", **PLAN_FIELDS)
    for bad in ({"subagent_type": "code", "new_pages": 0}, {"subagent_type": "code", "audience": "The ML team"}, {"subagent_type": "dashboard"}):
        with pytest.raises(ValidationError):
            CodingAgentPlan(**PLAN_FIELDS, **bad)


@pytest.mark.asyncio
async def test_save_coding_plan_returns_the_guide():
    """Asking for a guide returns that type's guide, and touches no saved plan (the call would fail without Firestore)."""
    for name in ("code", "dashboard"):
        guide = await save_coding_plan.ainvoke({"conversation_id": "c1", "guides_read": [], "guide": name})
        assert guide.startswith(f"# Writing a {name} plan")


@pytest.mark.asyncio
async def test_save_coding_plan_needs_its_guide_read_first():
    """A plan is not saved until its type's guide was read this turn (a save that got further would fail without Firestore)."""
    plan = dict(subagent_type="code", **PLAN_FIELDS)
    refused = await save_coding_plan.ainvoke({"conversation_id": "c1", "guides_read": ["dashboard"], "plan": plan})
    assert "Plan not saved" in refused and "guide set to 'code'" in refused
