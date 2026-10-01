"""Layer 1 tests for app/orchestrator/orchestrator.py -- pure functions and
plain-dict state only, no LLM/BigQuery/MCP calls (docs/testing.md).
"""
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage

from app.config import MAX_ANSWER_TABLE_ROWS, MAX_LENGTH_RETRIES, MAX_VERIFY_RETRIES
from app.orchestrator.orchestrator import (
    AgentState,
    build_agent_response,
    build_human_message,
    build_initial_state,
    check_table_rows,
    extract_table_values,
    iteration_cap_update,
    route_after_agent,
    route_after_call_tool,
    route_after_check_length,
    route_after_verify,
    verify_response,
)


# Entry/exit helpers

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
    assert state["answer_submitted"] is False
    assert state["answer_markdown"] == ""
    assert state["chart_urls"] == []
    assert state["pending_queries"] == []
    assert state["estimated_cost"] is None


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
    }
    response = build_agent_response(base_state)
    assert response.answer_markdown == "The answer."
    assert response.chart_urls == ["https://example.com/chart.png"]
    assert response.suggested_follow_ups == ["What about last quarter?"]
    assert response.pending_query is None

    # Non-empty pending_queries -- the most recently added one
    with_pending = {**base_state, "pending_queries": [
        {"id": "a", "query": "SELECT 1"}, {"id": "b", "query": "SELECT 2"},
    ]}
    assert build_agent_response(with_pending).pending_query == "SELECT 2"


# Guardrails

def test_check_table_rows():
    """True only when every table's row count is within MAX_ANSWER_TABLE_ROWS."""
    within_cap = "| A |\n|---|\n" + "\n".join(f"| {i} |" for i in range(MAX_ANSWER_TABLE_ROWS))
    assert check_table_rows(within_cap) is True

    over_cap = "| A |\n|---|\n" + "\n".join(f"| {i} |" for i in range(MAX_ANSWER_TABLE_ROWS + 1))
    assert check_table_rows(over_cap) is False

    # Two-column table with no leading/trailing "|" on any row
    no_edge_pipes = "A | B\n---|---\n" + "\n".join(f"{i} | {i}" for i in range(MAX_ANSWER_TABLE_ROWS + 1))
    assert check_table_rows(no_edge_pipes) is False


def test_extract_table_values():
    """Pulls every numeric table cell, normalizing formatting; ignores non-table text."""
    # Comma/currency normalization, negative numbers
    md = """| Department | Reorder Rate | Revenue |
|---|---|---|
| Dairy Eggs | 66.8% | $6,250.00 |
| Bakery | -5.2 | $100 |"""
    assert extract_table_values(md) == [66.8, 6250.0, -5.2, 100.0]

    # Bold-formatted cells
    md_bold = """| Department | Reorder Rate |
|---|---|
| **Dairy Eggs** | **66.8%** |"""
    assert extract_table_values(md_bold) == [66.8]

    # Prose containing "|" but no real table
    assert extract_table_values("The ratio is a|b, 42 percent maybe.") == []


def test_verify_response():
    """Unsubmitted, verified, and unmatched-claim(s) cases."""
    tool_calls = [
        {"name": "run_bigquery_sql", "success": True,
         "result": [{"department": "dairy eggs", "reorder_rate": 0.6676, "n": 551399}]},
        # Number in a non-query tool's result
        {"name": "read_repo_file", "success": True,
         "result": [{"content": "MAX_ROWS = 1000"}]},
    ]

    # No submit_answer call -- empty answer_markdown
    unsubmitted = {"answer_markdown": "", "all_prose_numeric_claims": [],
                   "suggested_follow_ups": [], "tool_calls": tool_calls}
    verified, message = verify_response(unsubmitted)
    assert verified is False
    assert "No valid answer was submitted" in message

    # Table match and percentage-scaled prose claim
    good = {
        "answer_markdown": "| Count |\n|---|\n| 551399 |",
        "all_prose_numeric_claims": [66.8],
        "suggested_follow_ups": [],
        "tool_calls": tool_calls,
    }
    assert verify_response(good) == (True, None)

    # Claim with no matching pool value
    bad = {
        "answer_markdown": "Some answer.",
        "all_prose_numeric_claims": [42.0],
        "suggested_follow_ups": [],
        "tool_calls": tool_calls,
    }
    verified, message = verify_response(bad)
    assert verified is False
    assert "42.0" in message and "do not match any tool result this turn" in message

    # Multiple unmatched claims -- all reported together, not just the first
    multi_bad = {
        "answer_markdown": "Some answer.",
        "all_prose_numeric_claims": [42.0, 99.0],
        "suggested_follow_ups": [],
        "tool_calls": tool_calls,
    }
    verified, message = verify_response(multi_bad)
    assert verified is False
    assert "42.0" in message and "99.0" in message


# Routing

def test_route_after_agent():
    """Tool call -> call_tool; no tool call -> check_length."""
    with_tool_call = {"messages": [AIMessage(content="", tool_calls=[
        {"name": "run_bigquery_sql", "args": {}, "id": "1", "type": "tool_call"}])]}
    assert route_after_agent(with_tool_call) == "call_tool"

    without_tool_call = {"messages": [AIMessage(content="done")]}
    assert route_after_agent(without_tool_call) == "check_length"


def test_route_after_call_tool():
    """Cancelled/needs_approval -> finalize; submitted -> check_length; else -> agent."""
    base = {"cancelled": False, "needs_approval": False, "answer_submitted": False}

    assert route_after_call_tool({**base, "cancelled": True}) == "finalize"
    assert route_after_call_tool({**base, "needs_approval": True}) == "finalize"
    assert route_after_call_tool({**base, "answer_submitted": True}) == "check_length"
    assert route_after_call_tool(base) == "agent"


def test_route_after_check_length():
    """Displayable -> verify; over cap with retries left -> agent; exhausted -> finalize."""
    short_answer = {"answer_markdown": "Short answer.", "length_retry_count": 0}
    assert route_after_check_length(short_answer) == "verify"

    # Table row count over cap, well under the character cap
    long_table = {
        "answer_markdown": "| A |\n|---|\n" + "\n".join(f"| {i} |" for i in range(30)),
        "length_retry_count": 0,
    }
    assert route_after_check_length(long_table) == "agent"

    exhausted = {**long_table, "length_retry_count": MAX_LENGTH_RETRIES}
    assert route_after_check_length(exhausted) == "finalize"


def test_route_after_verify():
    """Verified -> finalize; not verified with retries left -> agent; exhausted -> finalize."""
    assert route_after_verify({"verified": True, "verification_retry_count": 0}) == "finalize"

    retrying = {"verified": False, "verification_retry_count": 0}
    assert route_after_verify(retrying) == "agent"

    exhausted = {"verified": False, "verification_retry_count": MAX_VERIFY_RETRIES}
    assert route_after_verify(exhausted) == "finalize"


# Iteration cap guardrail

def test_iteration_cap_update():
    """Every call in the batch is declined, with matching records/errors."""
    tool_calls = [
        {"id": "tc1", "name": "run_bigquery_sql", "args": {"query": "SELECT 1"}},
        {"id": "tc2", "name": "run_dax_query", "args": {"dax": 'EVALUATE ROW("x", 1)'}},
    ]
    result = iteration_cap_update(tool_calls, iteration_count=10)

    assert result["iteration_count"] == 11
    assert result["iteration_cap_hit"] is True

    # Every call in the batch is declined, none actually run
    assert all(m.status == "error" for m in result["messages"])

    # Each decline gets a matching ToolCallRecord and TurnError, joined by tool_call_id
    assert [r["id"] for r in result["tool_calls"]] == ["tc1", "tc2"]
    assert all(r["success"] is False for r in result["tool_calls"])
    assert [e["tool_call_id"] for e in result["errors"]] == ["tc1", "tc2"]
    assert all(e["error_type"] == "iteration_cap_hit" for e in result["errors"])
