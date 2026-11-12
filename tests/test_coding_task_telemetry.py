import json
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent_app.orchestration import count_tool_calls
from coding_agent_app.telemetry.schema import SCHEMA
from coding_agent_app.telemetry.writer import RunDetails, build_coding_task_row


def make_task(base_branch="dev", reviewer_ran=True):
    """A completed task document as Firestore stores it. The reviewer's tokens, verdict and criteria are empty when it did not run."""
    reviewer_tokens = {"input": 40, "output": 4, "cache_read": 30, "cache_write": 6} if reviewer_ran else None
    result = {
        "outcome": "failed", "stop_reason": None, "report": "Added the feature.",
        "files": [{"path": "definitions/a.sqlx", "added": 3, "removed": 1}, {"path": "definitions/b.sqlx", "added": 5, "removed": 0}],
        "checks": [{"name": "compile", "passed": True, "detail": None}, {"name": "pytest", "passed": False, "detail": "1 failed"}],
        "patch_problems": [], "assumptions": ["a1"], "blocking_questions": [], "follow_up_questions": ["q1"],
        "branch": "agent/aisle-pacing-t1", "commit": "abc", "started_from_commit": "def", "dev_moved": False,
        "review": {"ran": reviewer_ran, "verdict": "needs_revision" if reviewer_ran else None, "rounds": 2 if reviewer_ran else 0,
                   "explanation": "missing column" if reviewer_ran else "", "unverified": False,
                   "criteria": [{"criterion": "1. ok", "passed": True, "gap": None},
                                {"criterion": "2. bad", "passed": False, "gap": "missing column"}] if reviewer_ran else []},
        "token_usage": {"agent": {"input": 100, "output": 10, "cache_read": 80, "cache_write": 20}, "reviewer": reviewer_tokens},
        "todos": [],
    }
    moment = datetime(2026, 11, 11, tzinfo=timezone.utc)
    return {"task_id": "t1", "conversation_id": "c1", "user_id": "u1", "created_at": moment, "started_at": moment, "completed_at": moment,
            "plan": {"name": "Aisle pacing", "goal": "g", "base_branch": base_branch, "review": reviewer_ran}, "coding_agent_result": result}


def make_details(patch_bytes=b"diff --git a/x b/x\n"):
    return RunDetails(patch_bytes=patch_bytes, tool_calls=[{"name": "execute", "count": 7, "failed": 1}], agent_llm_calls=12,
                      reviewer_llm_calls=4, agent_models=["claude-x"], reviewer_models=["claude-x"], langsmith_run_id="run-1",
                      sandbox_snapshot_id="snap-1")


def test_row_matches_schema_and_flattens_the_task():
    """The row has exactly the schema's columns, sums the files and checks, reads the tokens by role, and decodes the patch."""
    row = build_coding_task_row(make_task(), make_details())
    assert set(row) == {field.name for field in SCHEMA}
    assert (row["files_changed"], row["lines_added"], row["lines_removed"], row["checks_failed"]) == (2, 8, 1, 1)
    assert (row["agent_input_tokens"], row["reviewer_input_tokens"], row["review_rounds"]) == (100, 40, 2)
    assert (row["agent_llm_calls"], row["reviewer_llm_calls"]) == (12, 4)
    assert row["patch"] == "diff --git a/x b/x\n" and row["is_revision"] is False and row["review_criteria"][1]["gap"] == "missing column"
    assert json.loads(row["plan_json"])["name"] == "Aisle pacing" and json.loads(row["result_json"])["outcome"] == "failed"


def test_row_without_reviewer_patch_or_dev_base(monkeypatch):
    """No reviewer leaves its token and call columns and its explanation null, empty run details give a null patch and no tool calls,
    a branch base is a revision, and a patch byte that is not UTF-8 is replaced rather than failing."""
    monkeypatch.setenv("K_REVISION", "coding-agent-00007")
    row = build_coding_task_row(make_task(base_branch="agent/aisle-pacing-t1", reviewer_ran=False), RunDetails())
    assert row["reviewer_input_tokens"] is None and row["reviewer_llm_calls"] is None and row["review_explanation"] is None
    assert row["review_criteria"] == [] and row["patch"] is None and row["tool_calls"] == [] and row["agent_llm_calls"] == 0
    assert row["is_revision"] is True and row["cloud_run_revision"] == "coding-agent-00007"
    assert "�" in build_coding_task_row(make_task(), make_details(patch_bytes=b"+caf\xe9\n"))["patch"]


def test_count_tool_calls():
    """Calls are counted per tool in the order each tool first appears, and an errored ToolMessage counts as a failed call of the
    tool whose call id it answers."""
    messages = [
        HumanMessage(content="the task"),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "c1"}, {"name": "read_file", "args": {}, "id": "c2"}]),
        ToolMessage(content="boom", tool_call_id="c1", status="error"),
        ToolMessage(content="text", tool_call_id="c2"),
        AIMessage(content="", tool_calls=[{"name": "execute", "args": {}, "id": "c3"}]),
        ToolMessage(content="ok", tool_call_id="c3"),
    ]
    assert count_tool_calls(messages) == [{"name": "execute", "count": 2, "failed": 1}, {"name": "read_file", "count": 1, "failed": 0}]
    assert count_tool_calls([]) == []
