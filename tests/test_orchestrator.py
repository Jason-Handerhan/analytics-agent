"""Layer 1 tests for app/orchestrator/orchestrator.py -- pure functions and
plain-dict state only, no LLM/BigQuery/MCP calls (docs/testing.md).
"""
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.config import MAX_ANSWER_TABLE_ROWS, MAX_LENGTH_RETRIES, MAX_VERIFY_RETRIES, PENDING_APPROVAL_THRESHOLD
import app.orchestrator.orchestrator as orchestrator
from app.orchestrator.orchestrator import (
    ToolBatch,
    build_tool_call_records_and_messages,
    call_tool_node,
    check_table_rows,
    extract_table_values,
    iteration_cap_update,
    resolve_chart_data,
    resolve_combine_data,
    route_after_agent,
    route_after_call_tool,
    route_after_check_length,
    route_after_verify,
    verify_response,
)
from app.orchestrator.tools import ToolError


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


def test_build_tool_call_records_and_messages():
    """Records, display messages, and errors across a mixed batch: chartable
    success gets a ref_id, a failure gets a TurnError, submit_answer gets a
    message but no record, and other tools get a record with no ref_id."""
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    calls = [
        {"id": "tc1", "name": "run_bigquery_sql", "args": {"query": "SELECT 1"}},
        {"id": "tc2", "name": "run_dax_query", "args": {"dax": "EVALUATE ROW(\"x\", 2)"}},
        {"id": "tc3", "name": "run_bigquery_sql", "args": {"query": "SELECT bad"}},
        {"id": "tc4", "name": "submit_answer", "args": {}},
        {"id": "tc5", "name": "list_repo_files", "args": {}},
    ]
    messages = [
        ToolMessage(content='[{"n": 1}]', name="run_bigquery_sql", tool_call_id="tc1", status="success",
                    artifact={"bytes_billed": 1234}),
        ToolMessage(content='[{"v": 2}]', name="run_dax_query", tool_call_id="tc2", status="success"),
        ToolMessage(content="boom", name="run_bigquery_sql", tool_call_id="tc3", status="error"),
        ToolMessage(content="Answer recorded.", name="submit_answer", tool_call_id="tc4", status="success"),
        ToolMessage(content='["a.py"]', name="list_repo_files", tool_call_id="tc5", status="success"),
    ]
    batch = ToolBatch(calls, messages, now, now, "tool_error")

    result = build_tool_call_records_and_messages(batch, ref_counter=3)

    # submit_answer gets no record; chartable successes take ref_3/ref_4 and advance the counter
    assert result.records == [
        {"id": "tc1", "name": "run_bigquery_sql", "args": {"query": "SELECT 1"},
         "query_text": "SELECT 1", "result": [{"n": 1}], "success": True, "error": None,
         "ref_id": "ref_3", "bytes_billed": 1234, "started_at": now, "completed_at": now},
        {"id": "tc2", "name": "run_dax_query", "args": {"dax": 'EVALUATE ROW("x", 2)'},
         "query_text": 'EVALUATE ROW("x", 2)', "result": [{"v": 2}], "success": True, "error": None,
         "ref_id": "ref_4", "bytes_billed": None, "started_at": now, "completed_at": now},
        {"id": "tc3", "name": "run_bigquery_sql", "args": {"query": "SELECT bad"},
         "query_text": "SELECT bad", "result": "", "success": False, "error": "boom",
         "ref_id": None, "bytes_billed": None, "started_at": now, "completed_at": now},
        {"id": "tc5", "name": "list_repo_files", "args": {},
         "query_text": None, "result": ["a.py"], "success": True, "error": None,
         "ref_id": None, "bytes_billed": None, "started_at": now, "completed_at": now},
    ]
    assert result.ref_counter == 5

    # Only the failed call produces a TurnError, joined to its record by tool_call_id
    assert result.errors == [
        {"stage": "call_tool", "error_type": "tool_error", "message": "boom",
         "occurred_at": now, "tool_call_id": "tc3"},
    ]

    # One display message per call; chartable results carry the reference label
    assert result.display_messages == [
        ToolMessage(content='Reference id for charting this result: ref_3\n[{"n": 1}]',
                    name="run_bigquery_sql", tool_call_id="tc1", status="success"),
        ToolMessage(content='Reference id for charting this result: ref_4\n[{"v": 2}]',
                    name="run_dax_query", tool_call_id="tc2", status="success"),
        messages[2],
        messages[3],
        messages[4],
    ]


@pytest.mark.asyncio
async def test_call_tool_node_submit_answer_branch():
    """A sole submit_answer call is normalized into state: a question turn copies
    the question into answer_markdown and clears follow-ups; an answer turn passes
    its answer and follow-ups straight through."""
    question_call = {"id": "call_q", "name": "submit_answer", "type": "tool_call", "args": {
        "answer_markdown": "", "all_prose_numeric_claims": [],
        "suggested_follow_ups": ["What about last quarter?"],
        "clarifying_question": "Which metric?",
    }}
    question_state = {"messages": [AIMessage(content="", tool_calls=[question_call])],
                      "iteration_count": 0}
    question_update = await call_tool_node(question_state)

    assert question_update["answer_submitted"] is True
    assert question_update["clarifying_question"] == "Which metric?"
    assert question_update["answer_markdown"] == "Which metric?"
    assert question_update["suggested_follow_ups"] == []

    answer_call = {"id": "call_a", "name": "submit_answer", "type": "tool_call", "args": {
        "answer_markdown": "There were 551,399 orders.", "all_prose_numeric_claims": [551399.0],
        "suggested_follow_ups": ["What about last quarter?"], "clarifying_question": "",
    }}
    answer_state = {"messages": [AIMessage(content="", tool_calls=[answer_call])],
                    "iteration_count": 0}
    answer_update = await call_tool_node(answer_state)

    assert answer_update["answer_submitted"] is True
    assert answer_update["clarifying_question"] == ""
    assert answer_update["answer_markdown"] == "There were 551,399 orders."
    assert answer_update["suggested_follow_ups"] == ["What about last quarter?"]
    assert answer_update["all_prose_numeric_claims"] == [551399.0]


def test_resolve_chart_and_combine_data():
    """Ref resolution into real-tool args for charts and combines, plus every
    resolve error path (unknown, failed, non-chartable, too few refs)."""
    prior = [
        {"name": "run_bigquery_sql", "ref_id": "ref_1", "success": True, "result": [{"a": 1}]},
        {"name": "run_dax_query", "ref_id": "ref_2", "success": True, "result": [{"a": 2}]},
        {"name": "run_bigquery_sql", "ref_id": "ref_3", "success": False, "result": ""},
        {"name": "generate_chart", "ref_id": "ref_4", "success": True, "result": {"chart_url": "x"}},
    ]

    chart_tc = {"id": "call_c", "name": "generate_chart", "args": {
        "source_ref": "ref_1",
        "spec": {"chart_type": "bar", "title": "T", "x_label": "X", "y_label": "Y",
                 "x_field": "a", "y_field": "a"}}}
    chart = resolve_chart_data(chart_tc, prior)
    assert chart == {"id": "call_c", "name": "generate_chart", "args": {"args": {
        "data": [{"a": 1}],
        "spec": {"chart_type": "bar", "title": "T", "x_label": "X", "y_label": "Y",
                 "x_field": "a", "y_field": "a", "sort_order": None, "value_format": "auto"},
    }}}

    combine_tc = {"id": "call_m", "name": "combine_results", "args": {
        "source_refs": ["ref_1", "ref_2"], "method": "stack"}}
    combined = resolve_combine_data(combine_tc, prior)
    assert combined == {"id": "call_m", "name": "combine_results", "args": {"args": {
        "data": [[{"a": 1}], [{"a": 2}]], "method": "stack", "join_key": None, "join_how": "inner",
    }}}

    unknown_chart_tc = {"id": "call_c", "name": "generate_chart", "args": {
        "source_ref": "ref_9",
        "spec": {"chart_type": "bar", "title": "T", "x_label": "X", "y_label": "Y",
                 "x_field": "a", "y_field": "a"}}}
    with pytest.raises(ToolError, match="'ref_9' is not a successfully completed"):
        resolve_chart_data(unknown_chart_tc, prior)

    failed_chart_tc = {"id": "call_c", "name": "generate_chart", "args": {
        "source_ref": "ref_3",
        "spec": {"chart_type": "bar", "title": "T", "x_label": "X", "y_label": "Y",
                 "x_field": "a", "y_field": "a"}}}
    with pytest.raises(ToolError, match="'ref_3' is not a successfully completed"):
        resolve_chart_data(failed_chart_tc, prior)

    too_few_tc = {"id": "call_m", "name": "combine_results", "args": {
        "source_refs": ["ref_1"], "method": "stack"}}
    with pytest.raises(ToolError, match="at least two"):
        resolve_combine_data(too_few_tc, prior)

    non_chartable_tc = {"id": "call_m", "name": "combine_results", "args": {
        "source_refs": ["ref_1", "ref_4"], "method": "stack"}}
    with pytest.raises(ToolError, match="'ref_4' is not a successfully completed"):
        resolve_combine_data(non_chartable_tc, prior)


@pytest.mark.asyncio
async def test_call_tool_node_pauses_over_threshold(monkeypatch):
    """A batch over PENDING_APPROVAL_THRESHOLD pauses before any tool runs, and the
    card's query is the one with the largest estimate."""
    big, small = PENDING_APPROVAL_THRESHOLD, PENDING_APPROVAL_THRESHOLD // 2
    estimates = {"SELECT big": big, "SELECT small": small}

    async def fake_dry_run(query):
        return estimates[query]

    monkeypatch.setattr(orchestrator, "dry_run", fake_dry_run)
    executed = []

    class _RecordingTool:
        async def ainvoke(self, tc):
            executed.append(tc)

    monkeypatch.setitem(orchestrator.ALL_TOOLS, "run_bigquery_sql", _RecordingTool())
    calls = [
        {"id": "c1", "name": "run_bigquery_sql", "type": "tool_call", "args": {"query": "SELECT big"}},
        {"id": "c2", "name": "run_bigquery_sql", "type": "tool_call", "args": {"query": "SELECT small"}},
    ]
    state = {"messages": [AIMessage(content="", tool_calls=calls)], "bytes_consumed": 0, "iteration_count": 0,
             "approved_batch": False}

    update = await call_tool_node(state)

    assert update["needs_approval"] is True
    assert update["largest_pending_query"] == "SELECT big"
    assert update["pending_queries"] == [
        {"id": "c1", "query": "SELECT big", "estimated_bytes": big},
        {"id": "c2", "query": "SELECT small", "estimated_bytes": small},
    ]
    assert executed == []  # the pause runs no tool
