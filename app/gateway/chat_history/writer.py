import asyncio
import json
from datetime import datetime

from app.config import CHAT_HISTORY_DATASET, CHAT_HISTORY_TABLE, GCP_PROJECT_ID
from app.gateway.chat_history.schema import SCHEMA
from app.orchestrator.shared_helpers import get_turn_writer_client


def build_turn_row(
    conversation_id: str,
    user_id: str,
    question: str,
    completed_at: datetime,
    answer_html: str,
    sources: list[str],
    suggested_follow_ups: list[str],
    needs_approval: bool,
) -> dict:
    """One agent_chat_history.turns row. turn_data holds raw AgentResponse
    shapes -- sources/suggested_follow_ups unjoined, needs_approval as written.
    No live_turns override on read: a resolved approval always produces its
    own later row, so the last row's needs_approval reflects live reality by
    construction (app/gateway/chat_history/reader.py)."""
    turn_data = {
        "question": question,
        "answer_html": answer_html,
        "sources": sources,
        "suggested_follow_ups": suggested_follow_ups,
        "needs_approval": needs_approval,
    }
    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "completed_at": completed_at,
        "question": question,
        "turn_data": json.dumps(turn_data),
    }


async def write_turn_row(row: dict) -> list:
    """Writes a chat-history row to BigQuery. Written unconditionally, every
    turn including ones where needs_approval was True at write time
    (docs/frontend.md's "Chat history" section has the full reasoning)."""
    table_ref = f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CHAT_HISTORY_TABLE}"
    return await asyncio.to_thread(get_turn_writer_client().insert_rows, table_ref, [row], selected_fields=SCHEMA)
