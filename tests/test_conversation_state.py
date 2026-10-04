"""Layer 1 test for app/gateway/entry_exit.py's conversation-history
functions -- no real credentials needed (docs/testing.md). Both functions
are pure (plain dicts/BaseMessage in, plain dicts/BaseMessage out), so
nothing here is mocked.
"""
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, ToolMessage, messages_to_dict

from app.config import HISTORY_TURN_COUNT
from app.gateway.entry_exit import (
    STALE_RESULT_MESSAGE,
    build_history_messages,
    build_updated_history,
)


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
