"""Layer 1 test for app/telemetry/writer.py — no real credentials needed (docs/testing.md)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import app.telemetry.writer as writer
from app.config import GCP_PROJECT_ID, TELEMETRY_DATASET, TELEMETRY_TABLE


@pytest.mark.asyncio
async def test_write_telemetry_row_success(monkeypatch):
    fake_client = MagicMock()
    fake_client.insert_rows_json.return_value = []
    monkeypatch.setattr(writer, "get_bq_client", lambda: fake_client)

    started = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 9, 18, 12, 0, 5, tzinfo=timezone.utc)
    row = writer.build_telemetry_row(
        conversation_id="conv-123",
        user_id="user-oid-456",
        question="What is the recall at 5?",
        answer_markdown="Echo: What is the recall at 5?",
        turn_started_at=started,
        turn_completed_at=completed,
    )

    errors = await writer.write_telemetry_row(row)

    assert errors == []
    fake_client.insert_rows_json.assert_called_once_with(
        f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{TELEMETRY_TABLE}", [row]
    )
    assert row["conversation_id"] == "conv-123"
    assert row["turn_started_at"] == started.isoformat()
    assert row["tool_calls"] == []
    assert row["verified"] is False
