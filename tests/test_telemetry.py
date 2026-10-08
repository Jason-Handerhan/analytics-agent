"""Layer 1 test for app/telemetry/writer.py — no real credentials needed (docs/testing.md)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import app.telemetry.writer as writer
from app.config import GCP_PROJECT_ID, TELEMETRY_DATASET, TELEMETRY_TABLE
from app.telemetry.schema import SCHEMA

STARTED = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
COMPLETED = datetime(2026, 9, 18, 12, 0, 5, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize("clarifying_question, expected", [
    ("", None),
    ("Which metric?", "Which metric?"),
])
async def test_write_telemetry_row_success(monkeypatch, clarifying_question, expected):
    """Builds a full row, inserts it, and checks its JSON/native-type shape."""
    fake_client = MagicMock()
    fake_client.insert_rows.return_value = []
    monkeypatch.setattr(writer, "get_bq_client", lambda: fake_client)

    row = writer.build_telemetry_row(
        conversation_id="conv-123",
        user_id="user-oid-456",
        question="What is the recall at 5?",
        answer_markdown="Echo: What is the recall at 5?",
        turn_started_at=STARTED,
        turn_completed_at=COMPLETED,
        filter_context=[{"table": "orders", "column": "region", "values": ["west"]}],
        active_page="Overview",
        tool_calls=[{
            "id": "tc1", "name": "run_bigquery_sql", "args": {"query": "SELECT 1"},
            "query_text": "SELECT 1", "result": [{"a": 1}], "success": True, "error": None,
            "bytes_billed": 1234,
            "started_at": STARTED, "completed_at": COMPLETED,
        }],
        errors=[{
            "stage": "call_tool", "error_type": "tool_error", "message": "boom",
            "occurred_at": STARTED, "tool_call_id": "tc1",
        }],
        prompt_tokens=123,
        completion_tokens=45,
        llm_calls=2,
        bytes_consumed=900_000_000,
        bytes_consumed_baseline=100_000_000,
        iteration_count=1,
        verified=True,
        verification_retry_count=0,
        length_retry_count=0,
        needs_approval=False,
        cost_cap_exceeded=False,
        cancelled=False,
        iteration_cap_hit=False,
        estimated_cost="$0.02",
        pending_queries=[
            {"id": "a", "query": "SELECT 1", "estimated_bytes": 1000},
            {"id": "b", "query": "SELECT department, AVG(reordered) FROM orders GROUP BY department", "estimated_bytes": 5000},
        ],
        largest_pending_query="SELECT department, AVG(reordered) FROM orders GROUP BY department",
        approval_decision=None,
        deferred_dax=[{"id": "c", "dax": 'EVALUATE ROW("x", 1)'}],
        chart_urls=[],
        suggested_follow_ups=["What about last quarter?"],
        all_prose_numeric_claims=[0.42, 100.0],
        clarifying_question=clarifying_question,
    )

    errors = await writer.write_telemetry_row(row)

    assert errors == []
    fake_client.insert_rows.assert_called_once_with(
        f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{TELEMETRY_TABLE}", [row], selected_fields=SCHEMA
    )

    # Shape/format checks -- STRING columns JSON-dumped, datetimes left native
    assert row["conversation_id"] == "conv-123"
    assert row["turn_started_at"] == STARTED
    assert row["turn_completed_at"] == COMPLETED
    assert row["filter_context"] == '[{"table": "orders", "column": "region", "values": ["west"]}]'
    assert row["active_page"] == "Overview"
    assert row["verified"] is True
    assert row["prompt_tokens"] == 123
    assert row["completion_tokens"] == 45
    assert row["estimated_cost"] == "$0.02"
    assert row["bytes_consumed"] == 800_000_000

    # tool_calls: args/result are JSON-dumped; id/query_text pass through
    assert row["tool_calls"][0]["id"] == "tc1"
    assert row["tool_calls"][0]["bytes_billed"] == 1234
    assert row["tool_calls"][0]["args"] == '{"query": "SELECT 1"}'
    assert row["tool_calls"][0]["result"] == '[{"a": 1}]'
    assert row["tool_calls"][0]["started_at"] == STARTED

    # errors: tool_call_id is the join key back to tool_calls.id
    assert row["errors"][0]["tool_call_id"] == "tc1"
    assert row["errors"][0]["occurred_at"] == STARTED

    # pending_query comes from largest_pending_query, not from the list
    assert row["pending_query"] == "SELECT department, AVG(reordered) FROM orders GROUP BY department"
    assert row["pending_queries"] == [
        {"id": "a", "query": "SELECT 1", "estimated_bytes": 1000},
        {"id": "b", "query": "SELECT department, AVG(reordered) FROM orders GROUP BY department", "estimated_bytes": 5000},
    ]
    assert row["deferred_dax"] == [{"id": "c", "dax": 'EVALUATE ROW("x", 1)'}]

    # Stays null regardless of input
    assert row["approval_decision"] is None

    # suggested_follow_ups and all_prose_numeric_claims pass straight through
    assert row["suggested_follow_ups"] == ["What about last quarter?"]
    assert row["all_prose_numeric_claims"] == [0.42, 100.0]

    # clarifying_question: "" on an answer turn becomes NULL, text passes through
    assert row["clarifying_question"] == expected
