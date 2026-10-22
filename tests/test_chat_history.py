"""Layer 1 tests for app/gateway/chat_history/ -- no real credentials needed
(docs/testing.md). The SQL itself (grouping, ordering) is Layer 2, proven
live in notebooks/test_chat_history_endpoints.ipynb -- these cover the pure
logic on our side of the BigQuery client: row assembly, query parameters,
and the write-side/read-side shape agreement.
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import app.gateway.chat_history.reader as reader
import app.gateway.chat_history.writer as writer
from app.config import (
    CHAT_HISTORY_DATASET,
    CHAT_HISTORY_TABLE,
    CONVERSATION_LIST_LIMIT,
    CONVERSATION_TITLE_MAX_CHARS,
    DELETED_CONVERSATIONS_TABLE,
    GCP_PROJECT_ID,
)
from app.gateway.chat_history.schema import CHAT_HISTORY_SCHEMA, DELETED_CONVERSATIONS_SCHEMA

COMPLETED = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


# --- _truncate_title ---------------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    # At the limit -- unchanged (<=, not <).
    ("x" * CONVERSATION_TITLE_MAX_CHARS, "x" * CONVERSATION_TITLE_MAX_CHARS),
    # Cutoff lands inside the trailing word -- dropped whole, not kept half-formed.
    ("x" * (CONVERSATION_TITLE_MAX_CHARS - 5) + " " + "y" * 10,
     "x" * (CONVERSATION_TITLE_MAX_CHARS - 5) + "..."),
    # No spaces at all (one run-on token) -- falls back to the hard cut.
    ("x" * (CONVERSATION_TITLE_MAX_CHARS + 10),
     "x" * (CONVERSATION_TITLE_MAX_CHARS - 1) + "..."),
])
def test_truncate_title(question, expected):
    assert reader._truncate_title(question) == expected


# --- build_turn_row / write_turn_row -----------------------------------------

@pytest.mark.asyncio
async def test_write_turn_row_success(monkeypatch):
    """Builds a full row, inserts it, and checks its JSON/native-type shape."""
    fake_client = MagicMock()
    fake_client.insert_rows.return_value = []
    monkeypatch.setattr(writer, "get_turn_writer_client", lambda: fake_client)

    row = writer.build_turn_row(
        conversation_id="conv-123",
        user_id="user-oid-456",
        question="What is the recall at 5?",
        completed_at=COMPLETED,
        answer_html="<p>0.6688</p>",
        sources=["Query warehouse"],
        suggested_follow_ups=["What about last quarter?"],
        needs_approval=False,
    )

    errors = await writer.write_turn_row(row)

    assert errors == []
    fake_client.insert_rows.assert_called_once_with(
        f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CHAT_HISTORY_TABLE}", [row], selected_fields=CHAT_HISTORY_SCHEMA
    )

    assert row["conversation_id"] == "conv-123"
    assert row["user_id"] == "user-oid-456"
    assert row["completed_at"] == COMPLETED
    assert row["question"] == "What is the recall at 5?"

    turn_data = json.loads(row["turn_data"])
    assert turn_data == {
        "question": "What is the recall at 5?",
        "answer_html": "<p>0.6688</p>",
        "sources": ["Query warehouse"],
        "suggested_follow_ups": ["What about last quarter?"],
        "needs_approval": False,
    }


# --- build_deletion_row / write_deletion_row ---------------------------------

@pytest.mark.asyncio
async def test_write_deletion_row_success(monkeypatch):
    """Builds a deletion-marker row and inserts it into the separate
    deleted_conversations table -- not the turns table."""
    fake_client = MagicMock()
    fake_client.insert_rows.return_value = []
    monkeypatch.setattr(writer, "get_turn_writer_client", lambda: fake_client)

    row = writer.build_deletion_row(
        conversation_id="conv-123",
        user_id="user-oid-456",
        deleted_at=COMPLETED,
    )

    errors = await writer.write_deletion_row(row)

    assert errors == []
    fake_client.insert_rows.assert_called_once_with(
        f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{DELETED_CONVERSATIONS_TABLE}", [row],
        selected_fields=DELETED_CONVERSATIONS_SCHEMA
    )
    assert row == {
        "conversation_id": "conv-123",
        "user_id": "user-oid-456",
        "deleted_at": COMPLETED,
    }


# --- list_conversations / get_conversation_history ---------------------------

def _fake_client_returning(rows):
    client = MagicMock()
    client.query.return_value.result.return_value = rows
    return client


@pytest.mark.asyncio
async def test_list_conversations_passes_params_and_maps_rows(monkeypatch):
    fake_client = _fake_client_returning([
        {"conversation_id": "conv-1", "first_question": "What's the reorder rate?", "last_activity_at": COMPLETED},
    ])
    monkeypatch.setattr(reader, "get_turn_writer_client", lambda: fake_client)

    result = await reader.list_conversations("user-oid-456")

    (sql,), kwargs = fake_client.query.call_args
    params = {p.name: (p.type_, p.value) for p in kwargs["job_config"].query_parameters}
    assert params == {"user_id": ("STRING", "user-oid-456"), "limit": ("INT64", CONVERSATION_LIST_LIMIT)}
    assert f"NOT IN (SELECT conversation_id FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{DELETED_CONVERSATIONS_TABLE}`)" in sql

    assert len(result) == 1
    assert result[0].conversation_id == "conv-1"
    assert result[0].title  # truncation itself is test_truncate_title's job, not this one's
    assert result[0].last_activity_at == COMPLETED


@pytest.mark.asyncio
async def test_get_conversation_history_passes_params_and_decodes_rows(monkeypatch):
    turn_data = {
        "question": "What's the reorder rate?",
        "answer_html": "<p>22%</p>",
        "sources": ["Query warehouse"],
        "suggested_follow_ups": [],
        "needs_approval": False,
    }
    fake_client = _fake_client_returning([{"turn_data": json.dumps(turn_data)}])
    monkeypatch.setattr(reader, "get_turn_writer_client", lambda: fake_client)

    result = await reader.get_conversation_history("conv-1")

    (sql,), kwargs = fake_client.query.call_args
    params = {p.name: (p.type_, p.value) for p in kwargs["job_config"].query_parameters}
    assert params == {"conversation_id": ("STRING", "conv-1")}
    assert f"NOT IN (SELECT conversation_id FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{DELETED_CONVERSATIONS_TABLE}`)" in sql

    assert len(result) == 1
    assert result[0].question == turn_data["question"]
    assert result[0].answer_html == turn_data["answer_html"]
    assert result[0].sources == turn_data["sources"]
    assert result[0].needs_approval is False
