"""Layer 1 tests for app/gateway/entry_exit.py -- no real credentials needed
(docs/testing.md). Every function here is pure (plain dicts/BaseMessage in,
plain dicts/BaseMessage out), so nothing is mocked.
"""
import json
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict

from app.config import (
    ANSWER_CODE_BLOCK_BG,
    ANSWER_INLINE_CODE_BG,
    ANSWER_TABLE_HEADER_BG,
)
from app.gateway.entry_exit import (
    GEMINI_THOUGHT_SIGNATURES_KEY,
    PAUSE_REDACTED_MESSAGE,
    STALE_RESULT_MESSAGE,
    _GEMINI_SIGNATURES_STORAGE_KEY,
    _drop_gemini_signatures,
    _rename_gemini_signatures_for_storage,
    _restore_gemini_signatures,
    _strip_thinking_blocks,
    build_agent_response,
    build_history_messages,
    build_human_message,
    build_initial_state,
    build_pending_approval,
    build_updated_history,
    rebuild_paused_messages,
)
from app.orchestrator.state import AgentState


# --- Building a turn: human message and initial state ----------------------------------------


def test_build_human_message():
    """Question plus optional dashboard-page/filter-state grounding."""
    plain = build_human_message("How many orders?", [], None)
    assert isinstance(plain, HumanMessage)
    assert plain.content == "How many orders?"
    assert datetime.fromisoformat(plain.additional_kwargs["turn_start"]).tzinfo is not None   # the turn's start time

    with_page = build_human_message("q", [], "Financial Impact")
    assert "Current dashboard page: Financial Impact" in with_page.content

    with_filters = build_human_message(
        "q", [{"filter_column": "department", "value": "produce"}], None)
    assert "Current filter state:" in with_filters.content
    assert "produce" in with_filters.content


def test_build_initial_state():
    """Every AgentState key present, with correct defaults and seeded messages."""
    started_at = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    state = build_initial_state(
        question="How many orders?",
        conversation_id="conv-1",
        user_id="user-1",
        model="claude-sonnet-5",
        model_provider="anthropic",
        filter_context=[{"filter_column": "department", "value": "produce"}],
        active_page="Financial Impact",
        image_base64=None,
        history_messages=[],
        notes=[],
        task_plan={},
        turn_started_at=started_at,
    )

    # Every AgentState key present, nothing extra, nothing missing
    assert set(state) == set(AgentState.__annotations__)

    assert state["question"] == "How many orders?"
    assert state["conversation_id"] == "conv-1"
    assert state["user_id"] == "user-1"
    assert state["model"] == "claude-sonnet-5"
    assert state["model_provider"] == "anthropic"
    assert state["turn_started_at"] == started_at
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


# --- History in and out ----------------------------------------------------------------------


def test_build_history_messages_ignores_map_key_order():
    """The same stored turn with its nested map keys in a different order (as Firestore
    returns them) rebuilds to identical messages, tool-call args included."""
    spec = {"title": "t", "chart_type": "heatmap", "x_field": "a", "y_field": "b"}
    reordered_spec = {"y_field": "b", "x_field": "a", "chart_type": "heatmap", "title": "t"}

    def stored(chart_spec):
        call = {"name": "generate_chart", "args": {"spec": chart_spec, "source_ref": "ref_1"}, "id": "toolu_1"}
        block = {"type": "tool_use", "id": "toolu_1", "name": "generate_chart", "input": call["args"]}
        return {"messages": messages_to_dict([AIMessage(content=[block], tool_calls=[call])])}

    original = build_history_messages([stored(spec)])
    reordered = build_history_messages([stored(reordered_spec)])
    assert list(original[0].tool_calls[0]["args"]["spec"]) == sorted(spec)
    assert json.dumps(original[0].content) == json.dumps(reordered[0].content)
    assert json.dumps(original[0].tool_calls) == json.dumps(reordered[0].tool_calls)


def _fake_turn(label: str) -> dict:
    """One minimal stored turn -- just enough to be distinguishable and FIFO-trimmable."""
    return {
        "messages": messages_to_dict([HumanMessage(content=label)]),
        "timestamp": datetime.now(timezone.utc),
    }


def test_build_updated_history_redacts_and_round_trips():
    """One realistic turn exercises selective redaction (success-gated, not
    just name-gated) and round-tripping the result back through
    build_history_messages."""
    history_messages = [_fake_turn("question 0")]

    bq_result = ToolMessage(content='[{"count": 42}]', name="run_bigquery_sql",
                             tool_call_id="call_1", status="success")
    other_result = ToolMessage(content="some doc chunk", name="search_docs",
                                tool_call_id="call_2", status="success")
    failed_call = ToolMessage(content="BigQuery error: bad column", name="run_bigquery_sql",
                               tool_call_id="call_3", status="error")
    answer = ToolMessage(content="Answer recorded.", name="submit_answer",
                          tool_call_id="call_4", status="success")
    agent_call = AIMessage(
        content=[{"type": "thinking", "thinking": "deciding what to query"}],
        additional_kwargs={GEMINI_THOUGHT_SIGNATURES_KEY: {"call_1": "sig-blob"}},
        tool_calls=[{"name": "run_bigquery_sql", "args": {"query": "SELECT 1"},
                    "id": "call_1", "type": "tool_call"}],
    )

    state = {"messages": [
        HumanMessage(content="How many orders?"),
        agent_call, bq_result, other_result, failed_call, answer,
    ]}

    updated = build_updated_history(history_messages, state)
    assert len(updated) == 2

    reconstructed = build_history_messages(updated)

    human_contents = [msg.content for msg in reconstructed if isinstance(msg, HumanMessage)]
    assert human_contents == ["question 0", "How many orders?"]

    # Selective redaction -- success-gated, not just name-gated.
    tool_messages = {msg.tool_call_id: msg for msg in reconstructed if isinstance(msg, ToolMessage)}
    assert tool_messages["call_1"].content == STALE_RESULT_MESSAGE
    assert tool_messages["call_2"].content == STALE_RESULT_MESSAGE
    assert tool_messages["call_3"].content == "BigQuery error: bad column"
    assert tool_messages["call_4"].content == "Answer recorded."

    # Thinking block and Gemini's thought-signature key are both gone --
    # never resumed once this turn is finished.
    reconstructed_ai = next(msg for msg in reconstructed if isinstance(msg, AIMessage))
    assert reconstructed_ai.content == []
    assert GEMINI_THOUGHT_SIGNATURES_KEY not in reconstructed_ai.additional_kwargs


def test_strip_thinking_blocks():
    """Drops a thinking block entirely, keeps a reasoning block (OpenAI's
    tool-calling shape), strips a leftover extras key from any block that
    remains, and leaves non-dict content and non-AIMessage messages alone."""
    ai_msg = AIMessage(content=[
        {"type": "thinking", "thinking": "working through it"},
        {"type": "reasoning", "summary": [], "content": [], "encrypted_content": "abc"},
        {"type": "text", "text": "The answer is 42", "extras": {"signature": "xyz"}},
    ])
    mixed_msg = AIMessage(content=["a loose string block", {"type": "text", "text": "ok"}])
    tool_msg = ToolMessage(content="untouched", name="run_bigquery_sql",
                           tool_call_id="tc1", status="success")

    stripped = _strip_thinking_blocks([ai_msg, mixed_msg, tool_msg])

    assert stripped[0].content == [
        {"type": "reasoning", "summary": [], "content": [], "encrypted_content": "abc"},
        {"type": "text", "text": "The answer is 42"},
    ]
    assert stripped[1].content == ["a loose string block", {"type": "text", "text": "ok"}]
    assert stripped[2] is tool_msg


def test_gemini_signature_drop_and_round_trip():
    """_drop_gemini_signatures removes the Firestore-reserved key for
    history; _rename_gemini_signatures_for_storage/_restore_gemini_signatures
    round-trip it unchanged for PendingApproval's live resume."""
    def make_message():
        return AIMessage(content=[], additional_kwargs={
            "function_call": {"name": "submit_answer", "arguments": "{}"},
            GEMINI_THOUGHT_SIGNATURES_KEY: {"call_1": "sig-blob"},
        })

    dropped = _drop_gemini_signatures(messages_to_dict([make_message()]))
    kwargs = dropped[0]["data"]["additional_kwargs"]
    assert GEMINI_THOUGHT_SIGNATURES_KEY not in kwargs
    assert kwargs["function_call"] == {"name": "submit_answer", "arguments": "{}"}

    renamed = _rename_gemini_signatures_for_storage(messages_to_dict([make_message()]))
    renamed_kwargs = renamed[0]["data"]["additional_kwargs"]
    assert GEMINI_THOUGHT_SIGNATURES_KEY not in renamed_kwargs
    assert renamed_kwargs[_GEMINI_SIGNATURES_STORAGE_KEY] == {"call_1": "sig-blob"}

    restored = _restore_gemini_signatures(renamed)
    restored_kwargs = restored[0]["data"]["additional_kwargs"]
    assert restored_kwargs[GEMINI_THOUGHT_SIGNATURES_KEY] == {"call_1": "sig-blob"}
    assert _GEMINI_SIGNATURES_STORAGE_KEY not in restored_kwargs


# --- Approval pause and resume ---------------------------------------------------------------
# build_pending_approval's field contract is covered by
# test_consume_graph_replaces_doc_with_pending_approval_on_pause (test_gateway.py) --
# its only caller, already tested with a full equality check against every field.


def test_build_pending_approval_redacts_with_pause_message():
    """Uses PAUSE_REDACTED_MESSAGE, not STALE_RESULT_MESSAGE -- the shared
    redaction logic itself is already covered by
    test_build_updated_history_redacts_trims_and_round_trips."""
    bq_result = ToolMessage(content='[{"count": 42}]', name="run_bigquery_sql",
                             tool_call_id="call_1", status="success")
    agent_call = AIMessage(content=[],
                           additional_kwargs={GEMINI_THOUGHT_SIGNATURES_KEY: {"call_1": "sig-blob"}})
    state = {
        "conversation_id": "conv-1", "model": "claude-sonnet-5", "model_provider": "anthropic",
        "question": "How many orders?",
        "filter_context": [], "active_page": None,
        "messages": [agent_call, bq_result],
        "pending_queries": [], "deferred_dax": [], "tool_calls": [],
        "iteration_count": 0, "bytes_consumed": 0, "estimated_cost": None,
        "paused_at": datetime.now(timezone.utc),
    }
    pending = build_pending_approval(state)
    reconstructed = messages_from_dict(pending["messages"])
    assert reconstructed[1].content == PAUSE_REDACTED_MESSAGE

    # Gemini's thought-signature key is stored under a Firestore-safe name,
    # not dropped -- resuming the live tool call needs the real value back.
    assert reconstructed[0].additional_kwargs[_GEMINI_SIGNATURES_STORAGE_KEY] == {"call_1": "sig-blob"}
    assert GEMINI_THOUGHT_SIGNATURES_KEY not in reconstructed[0].additional_kwargs


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
    agent_call = AIMessage(
        content=[], additional_kwargs={GEMINI_THOUGHT_SIGNATURES_KEY: {"tc1": "sig-blob"}},
        tool_calls=[{"name": "run_bigquery_sql", "args": {"query": "SELECT 1"},
                    "id": "tc1", "type": "tool_call"}],
    )
    messages = [
        HumanMessage(content="How many orders?"),
        agent_call,
        ToolMessage(content=redacted, name="run_bigquery_sql", tool_call_id="tc1", status="success"),
        ToolMessage(content="boom", name="run_bigquery_sql", tool_call_id="tc3", status="error"),
        ToolMessage(content=redacted, name="search_docs", tool_call_id="tc4", status="success"),
        ToolMessage(content="Answer recorded.", name="submit_answer", tool_call_id="tc5", status="success"),
    ]

    # Simulates the real pipeline: build_pending_approval already renamed
    # Gemini's thought-signature key for Firestore before this was stored.
    stored = _rename_gemini_signatures_for_storage(messages_to_dict(messages))
    rebuilt = rebuild_paused_messages(stored, tool_calls)

    assert rebuilt[0] == messages[0]
    rebuilt_ai = rebuilt[1]
    assert rebuilt_ai.additional_kwargs[GEMINI_THOUGHT_SIGNATURES_KEY] == {"tc1": "sig-blob"}
    assert _GEMINI_SIGNATURES_STORAGE_KEY not in rebuilt_ai.additional_kwargs
    assert rebuilt[2] == ToolMessage(
        content='Reference id for charting this result: ref_3\n[{"n": 1}]',
        name="run_bigquery_sql", tool_call_id="tc1", status="success")
    assert rebuilt[3] == messages[3]
    assert rebuilt[4] == messages[4]
    assert rebuilt[5] == messages[5]


# --- The response: build_agent_response ------------------------------------------------------


def test_build_agent_response():
    """Projects final state into AgentResponse; empty vs. non-empty pending_queries;
    answer_html renders the real custom styling for tables, code, and images."""
    answer_markdown = (
        "The answer.\n\n"
        "| Metric | Value |\n"
        "|---|---|\n"
        "| Recall@5 | 0.68 |\n\n"
        "```python\nprint('hi')\n```\n\n"
        "Inline `code` here, and a chart: ![chart](https://example.com/chart.png)"
    )
    base_state = {
        "answer_markdown": answer_markdown,
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
    assert response.answer_markdown == answer_markdown
    assert response.chart_urls == ["https://example.com/chart.png"]
    assert response.suggested_follow_ups == ["What about last quarter?"]
    assert response.pending_query is None

    # answer_html rendering assertions
    html = response.answer_html
    assert "<table" in html and "max-width: 100%" in html
    assert ANSWER_TABLE_HEADER_BG in html  # table header background, unique to the header cell style
    assert '<pre style="display: inline-block' in html  # code block shrinks to content width
    assert ANSWER_CODE_BLOCK_BG in html
    assert '<code style="font-family:' in html and ANSWER_INLINE_CODE_BG in html
    assert '<img src="https://example.com/chart.png"' in html and "max-width: 100%" in html

    # Pending Query test (comes from largest pending query)
    with_pending = {**base_state, "largest_pending_query": "SELECT 2", "pending_queries": [
        {"id": "a", "query": "SELECT 1"}, {"id": "b", "query": "SELECT 2"},
    ]}
    assert build_agent_response(with_pending).pending_query == "SELECT 2"
