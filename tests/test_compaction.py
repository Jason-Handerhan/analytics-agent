"""Layer 1 tests for app/orchestrator/compaction.py -- no real credentials needed
(docs/testing.md). Only the two outside calls are patched: the ledger read
(BigQuery) and the summarizer (LLM); everything else in the pipeline runs for real.
"""
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import HumanMessage, messages_to_dict

from app.config import COMPACTION_KEEP_TURNS
from app.orchestrator.compaction import TurnRangeSummary, compact_history_pipeline
from app.orchestrator.state import StoredTurn

BASE_TIME = datetime(2026, 10, 1, tzinfo=timezone.utc)
SUMMARIZED_COUNT = 2
PREVIOUS_LAST_TURN = 4


def _turn(n: int) -> StoredTurn:
    started = BASE_TIME + timedelta(hours=n)
    message = HumanMessage(content=f"question {n}", additional_kwargs={"turn_start": started.isoformat()})
    return StoredTurn(messages=messages_to_dict([message]), timestamp=started)


def _previous_summary() -> StoredTurn:
    covers_to = BASE_TIME - timedelta(hours=1)
    message = HumanMessage(content="recap", additional_kwargs={
        "summary": True, "covers_to": covers_to.isoformat(), "last_turn": PREVIOUS_LAST_TURN})
    return StoredTurn(messages=messages_to_dict([message]), timestamp=covers_to)


def _ledger_row(answered_at: datetime) -> dict:
    return {"answered_at": answered_at, "title": "Orders", "topics": ["orders"], "entities": [],
            "question": "How many orders?", "answer_markdown": "There were 42.", "verified_numbers": [42]}


@pytest.fixture
def patch_outside_calls(monkeypatch):
    """Returns the ledger-read arguments the pipeline passed, and fakes the summarizer."""
    fetch_args = {}
    rows = [_ledger_row(BASE_TIME + timedelta(minutes=10)), _ledger_row(BASE_TIME + timedelta(minutes=20))]

    async def fake_fetch(conversation_id, after, before):
        fetch_args.update(conversation_id=conversation_id, after=after, before=before)
        return rows

    async def fake_summarize(formatted_rows):
        summary = TurnRangeSummary(summary_markdown="Orders were counted.", title="Order counts", topics=["orders"])
        return summary, 111, 22

    monkeypatch.setattr("app.orchestrator.compaction.fetch_compaction_rows", fake_fetch)
    monkeypatch.setattr("app.orchestrator.compaction.summarize_turn_range", fake_summarize)
    return fetch_args, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("has_previous_summary", [False, True])
async def test_compact_history_pipeline_replaces_old_turns_with_summary(patch_outside_calls, has_previous_summary):
    """The newest COMPACTION_KEEP_TURNS turns stay verbatim and the older ones become one summary
    first in the history. The ledger window runs from the previous summary's covers_to (open without
    one) to the oldest kept turn's start, and turn numbers continue from the previous summary's
    last_turn (1 without one)."""
    fetch_args, rows = patch_outside_calls
    turns = [_turn(n) for n in range(SUMMARIZED_COUNT + COMPACTION_KEEP_TURNS)]
    previous = [_previous_summary()] if has_previous_summary else []

    result = await compact_history_pipeline("conv-1", "user-1", previous + turns)

    kept = turns[SUMMARIZED_COUNT:]
    assert result.compacted and not result.failed
    assert (result.prompt_tokens, result.completion_tokens) == (111, 22)
    assert result.stored_history[1:] == kept

    expected_after = BASE_TIME - timedelta(hours=1) if has_previous_summary else None
    first_kept_start = datetime.fromisoformat(kept[0].messages[0]["data"]["additional_kwargs"]["turn_start"])
    assert fetch_args == {"conversation_id": "conv-1", "after": expected_after, "before": first_kept_start}

    first_turn = PREVIOUS_LAST_TURN + 1 if has_previous_summary else 1
    last_turn = first_turn + len(rows) - 1
    summary_kwargs = result.stored_history[0].messages[0]["data"]["additional_kwargs"]
    assert summary_kwargs == {"summary": True, "covers_to": rows[-1]["answered_at"].isoformat(), "last_turn": last_turn}
    assert (result.summary_row["first_turn"], result.summary_row["last_turn"]) == (first_turn, last_turn)
    assert result.summary_row["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_compact_history_pipeline_failure_leaves_history_unchanged(patch_outside_calls, monkeypatch):
    """A summarizer error never loses history or raises: the original list comes back, flagged failed."""
    async def failing_summarize(formatted_rows):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.orchestrator.compaction.summarize_turn_range", failing_summarize)
    history = [_turn(n) for n in range(SUMMARIZED_COUNT + COMPACTION_KEEP_TURNS)]

    result = await compact_history_pipeline("conv-1", "user-1", history)

    assert result.failed and not result.compacted
    assert result.stored_history is history
    assert result.summary_row is None
