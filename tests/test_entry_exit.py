"""Layer 1 tests for app/gateway/entry_exit.py -- no real credentials needed
(docs/testing.md). Every function here is pure (plain dicts/BaseMessage in,
plain dicts/BaseMessage out), so nothing is mocked.
"""
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, ToolMessage, messages_from_dict, messages_to_dict

from app.config import HISTORY_TURN_COUNT
from app.gateway.entry_exit import (
    PAUSE_REDACTED_MESSAGE,
    STALE_RESULT_MESSAGE,
    build_agent_response,
    build_history_messages,
    build_human_message,
    build_initial_state,
    build_pending_approval,
    build_updated_history,
    rebuild_paused_messages,
)
from app.orchestrator.state import AgentState


def test_build_human_message():
    """Question plus optional dashboard-page/filter-state grounding."""
    plain = build_human_message("How many orders?", [], None)
    assert isinstance(plain, HumanMessage)
    assert plain.content == "How many orders?"

    with_page = build_human_message("q", [], "Financial Impact")
    assert "Current dashboard page: Financial Impact" in with_page.content

    with_filters = build_human_message(
        "q", [{"filter_column": "department", "value": "produce"}], None)
    assert "Current filter state:" in with_filters.content
    assert "produce" in with_filters.content


def test_build_initial_state():
    """Every AgentState key present, with correct defaults and seeded messages."""
    state = build_initial_state(
        question="How many orders?",
        conversation_id="conv-1",
        user_id="user-1",
        filter_context=[{"filter_column": "department", "value": "produce"}],
        active_page="Financial Impact",
        image_base64=None,
        history_messages=[],
    )

    # Every AgentState key present, nothing extra, nothing missing
    assert set(state) == set(AgentState.__annotations__)

    assert state["question"] == "How many orders?"
    assert state["conversation_id"] == "conv-1"
    assert state["user_id"] == "user-1"
    assert isinstance(state["turn_started_at"], datetime)
    assert state["history_messages"] == []
    assert len(state["messages"]) == 1
    assert isinstance(state["messages"][0], HumanMessage)
    assert "Financial Impact" in state["messages"][0].content

    # Fresh-turn defaults
    assert state["tool_calls"] == []
    assert state["iteration_count"] == 0
    assert state["verified"] is False
    assert state["bytes_consumed"] == 0
    assert state["bytes_consumed_baseline"] == 0
    assert state["answer_submitted"] is False
    assert state["answer_markdown"] == ""
    assert state["clarifying_question"] == ""
    assert state["chart_urls"] == []
    assert state["pending_queries"] == []
    assert state["estimated_cost"] is None
    assert state["paused_at"] is None


def test_build_agent_response():
    """Projects final state into AgentResponse; empty vs. non-empty pending_queries."""
    base_state = {
        "answer_markdown": "The answer.",
        "sources": ["Query warehouse"],
        "needs_approval": False,
        "chart_urls": ["https://example.com/chart.png"],
        "suggested_follow_ups": ["What about last quarter?"],
        "iteration_cap_hit": False,
        "pending_queries": [],
        "estimated_cost": None,
        "cost_cap_exceeded": False,
        "largest_pending_query": None,
    }
    response = build_agent_response(base_state)
    assert response.answer_markdown == "The answer."
    assert response.chart_urls == ["https://example.com/chart.png"]
    assert response.suggested_follow_ups == ["What about last quarter?"]
    assert response.pending_query is None

    # Non-empty pending_queries -- the most recently added one
    with_pending = {**base_state, "largest_pending_query": "SELECT 2", "pending_queries": [
        {"id": "a", "query": "SELECT 1"}, {"id": "b", "query": "SELECT 2"},
    ]}
    assert build_agent_response(with_pending).pending_query == "SELECT 2"


# build_pending_approval's field contract is covered by
# test_consume_graph_replaces_doc_with_pending_approval_on_pause (test_gateway.py) --
# its only caller, already tested with a full equality check against every field.


def test_build_pending_approval_redacts_with_pause_message():
    """Uses PAUSE_REDACTED_MESSAGE, not STALE_RESULT_MESSAGE -- the shared
    redaction logic itself is already covered by
    test_build_updated_history_redacts_trims_and_round_trips."""
    bq_result = ToolMessage(content='[{"count": 42}]', name="run_bigquery_sql",
                             tool_call_id="call_1", status="success")
    state = {
        "conversation_id": "conv-1", "question": "How many orders?",
        "filter_context": [], "active_page": None,
        "messages": [bq_result],
        "pending_queries": [], "deferred_dax": [], "tool_calls": [],
        "iteration_count": 0, "bytes_consumed": 0, "estimated_cost": None,
        "paused_at": datetime.now(timezone.utc),
    }
    pending = build_pending_approval(state)
    reconstructed = messages_from_dict(pending["messages"])
    assert reconstructed[0].content == PAUSE_REDACTED_MESSAGE


def _fake_turn(label: str) -> dict:
    """One minimal stored turn -- just enough to be distinguishable and FIFO-trimmable."""
    return {
        "messages": messages_to_dict([HumanMessage(content=label)]),
        "timestamp": datetime.now(timezone.utc),
    }


def test_build_updated_history_redacts_trims_and_round_trips():
    """One realistic turn exercises all three behaviors together: selective
    redaction (success-gated, not just name-gated), FIFO trim to
    HISTORY_TURN_COUNT, and round-tripping the result back through
    build_history_messages."""
    # One more prior turn than the cap allows -- the oldest must be dropped.
    history_messages = [_fake_turn(f"question {n}") for n in range(HISTORY_TURN_COUNT + 1)]

    bq_result = ToolMessage(content='[{"count": 42}]', name="run_bigquery_sql",
                             tool_call_id="call_1", status="success")
    other_result = ToolMessage(content="some doc chunk", name="search_docs",
                                tool_call_id="call_2", status="success")
    failed_call = ToolMessage(content="BigQuery error: bad column", name="run_bigquery_sql",
                               tool_call_id="call_3", status="error")
    answer = ToolMessage(content="Answer recorded.", name="submit_answer",
                          tool_call_id="call_4", status="success")

    state = {"messages": [
        HumanMessage(content="How many orders?"),
        bq_result, other_result, failed_call, answer,
    ]}

    updated = build_updated_history(history_messages, state)

    # FIFO trim: HISTORY_TURN_COUNT + 1 prior turns plus this one -- only
    # the most recent HISTORY_TURN_COUNT survive.
    assert len(updated) == HISTORY_TURN_COUNT

    reconstructed = build_history_messages(updated)

    # The oldest prior turn was really dropped, not just over-counted away.
    human_contents = [msg.content for msg in reconstructed if isinstance(msg, HumanMessage)]
    assert "question 0" not in human_contents
    assert "How many orders?" in human_contents

    # Selective redaction -- success-gated, not just name-gated.
    tool_messages = {msg.tool_call_id: msg for msg in reconstructed if isinstance(msg, ToolMessage)}
    assert tool_messages["call_1"].content == STALE_RESULT_MESSAGE
    assert tool_messages["call_2"].content == STALE_RESULT_MESSAGE
    assert tool_messages["call_3"].content == "BigQuery error: bad column"
    assert tool_messages["call_4"].content == "Answer recorded."


def test_rebuild_paused_messages():
    """A numeric success gets its labeled content restored from the
    ToolCallRecord (same ref_id); a numeric error, a non-numeric success,
    and submit_answer all pass through exactly as stored."""
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    tool_calls = [
        {"id": "tc1", "name": "run_bigquery_sql", "args": {"query": "SELECT 1"},
         "query_text": "SELECT 1", "result": [{"n": 1}], "success": True, "error": None,
         "ref_id": "ref_3", "bytes_billed": 1234, "started_at": now, "completed_at": now},
        {"id": "tc3", "name": "run_bigquery_sql", "args": {"query": "SELECT bad"},
         "query_text": "SELECT bad", "result": "", "success": False, "error": "boom",
         "ref_id": None, "bytes_billed": None, "started_at": now, "completed_at": now},
        {"id": "tc4", "name": "search_docs", "args": {}, "query_text": None,
         "result": ["chunk"], "success": True, "error": None,
         "ref_id": None, "bytes_billed": None, "started_at": now, "completed_at": now},
    ]
    redacted = "Result redacted during this turn's approval pause."
    messages = [
        HumanMessage(content="How many orders?"),
        ToolMessage(content=redacted, name="run_bigquery_sql", tool_call_id="tc1", status="success"),
        ToolMessage(content="boom", name="run_bigquery_sql", tool_call_id="tc3", status="error"),
        ToolMessage(content=redacted, name="search_docs", tool_call_id="tc4", status="success"),
        ToolMessage(content="Answer recorded.", name="submit_answer", tool_call_id="tc5", status="success"),
    ]

    # Takes the stored (Firestore) dict form directly, like build_history_messages does.
    rebuilt = rebuild_paused_messages(messages_to_dict(messages), tool_calls)

    assert rebuilt[0] == messages[0]
    assert rebuilt[1] == ToolMessage(
        content='Reference id for charting this result: ref_3\n[{"n": 1}]',
        name="run_bigquery_sql", tool_call_id="tc1", status="success")
    assert rebuilt[2] == messages[2]
    assert rebuilt[3] == messages[3]
    assert rebuilt[4] == messages[4]
