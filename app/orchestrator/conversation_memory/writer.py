import asyncio
import json
import logging
from datetime import datetime

from app.config import (
    CHAT_HISTORY_DATASET,
    CONVERSATION_FINDINGS_TABLE,
    GCP_PROJECT_ID,
    TURN_RANGE_SUMMARIES_TABLE,
)
from app.orchestrator.conversation_memory.schema import (
    CONVERSATION_FINDINGS_SCHEMA,
    TURN_RANGE_SUMMARIES_SCHEMA,
)
from app.orchestrator.state import ToolCallRecord
from app.shared_helpers import get_turn_writer_client

logger = logging.getLogger(__name__)


def _serialize_tool_call(tc: ToolCallRecord) -> dict:
    """Matches the tool_calls RECORD in CONVERSATION_FINDINGS_SCHEMA -- no result."""
    return {
        "name": tc["name"],
        "args": json.dumps(tc["args"]),
        "ref_id": tc["ref_id"],
    }


def build_findings_row(
    conversation_id: str,
    user_id: str,
    answered_at: datetime,
    question: str,
    answer_markdown: str,
    verified_numbers: list[float],
    filter_context: list[dict],
    active_page: str | None,
    sources: list[str],
    title: str,
    topics: list[str],
    entities: list[str],
    tool_calls: list[ToolCallRecord],
) -> dict:
    """Full conversation_findings row. `verified_numbers` is built by the caller
    (all_prose_numeric_claims + extract_table_values) -- that helper lives in
    orchestrator.py, which imports this package."""
    approx_tokens = (len(question) + len(answer_markdown) + len(json.dumps(verified_numbers))) // 4
    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "answered_at": answered_at,
        "question": question,
        "answer_markdown": answer_markdown,
        "verified_numbers": verified_numbers,
        "approx_tokens": approx_tokens,
        "filter_context": json.dumps(filter_context),
        "active_page": active_page,
        "sources": sources,
        "title": title or None,
        "topics": topics,
        "entities": entities,
        "tool_calls": [
            _serialize_tool_call(tc) for tc in tool_calls
            if tc["success"] and tc["name"] != "submit_answer"
        ],
        "embedding": [],
        "embedding_model": None,
    }


async def write_findings_row(row: dict) -> None:
    """Writes a conversation_findings row. Logs a failure instead of raising --
    a missing ledger row must not cost the user their answer."""
    table_ref = f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CONVERSATION_FINDINGS_TABLE}"
    try:
        errors = await asyncio.to_thread(
            get_turn_writer_client().insert_rows, table_ref, [row],
            selected_fields=CONVERSATION_FINDINGS_SCHEMA,
        )
    except Exception:
        logger.exception("conversation_findings write failed for %s", row["conversation_id"])
        return
    if errors:
        logger.error("conversation_findings rejected row for %s: %s", row["conversation_id"], errors)


def build_summary_row(
    conversation_id: str,
    user_id: str,
    covers_from: datetime,
    covers_to: datetime,
    first_turn: int,
    last_turn: int,
    title: str,
    summary_markdown: str,
    topics: list[str],
    created_at: datetime,
) -> dict:
    """Full turn_range_summaries row. The summary is context only, not a numeric source."""
    approx_tokens = (len(title) + len(summary_markdown)) // 4
    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "covers_from": covers_from,
        "covers_to": covers_to,
        "first_turn": first_turn,
        "last_turn": last_turn,
        "title": title or None,
        "summary_markdown": summary_markdown,
        "topics": topics,
        "approx_tokens": approx_tokens,
        "created_at": created_at,
    }


async def write_summary_row(row: dict) -> None:
    """Writes a turn_range_summaries row. Logs a failure instead of raising --
    a missing summary row must not fail the turn."""
    table_ref = f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{TURN_RANGE_SUMMARIES_TABLE}"
    try:
        errors = await asyncio.to_thread(
            get_turn_writer_client().insert_rows, table_ref, [row],
            selected_fields=TURN_RANGE_SUMMARIES_SCHEMA,
        )
    except Exception:
        logger.exception("turn_range_summaries write failed for %s", row["conversation_id"])
        return
    if errors:
        logger.error("turn_range_summaries rejected row for %s: %s", row["conversation_id"], errors)
