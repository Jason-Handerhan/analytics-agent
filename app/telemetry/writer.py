import asyncio
from datetime import datetime
from functools import lru_cache

from google.cloud import bigquery

from app.config import GCP_PROJECT_ID, TELEMETRY_DATASET, TELEMETRY_TABLE


@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


def build_telemetry_row(
    conversation_id: str,
    user_id: str,
    question: str,
    answer_markdown: str,
    turn_started_at: datetime,
    turn_completed_at: datetime,
) -> dict:
    """Full agent_telemetry row — the six Phase 1 fields real, everything
    else the documented placeholder default until the phase that populates
    it lands (.claude/rules/telemetry.md)."""
    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "question": question,
        "answer_markdown": answer_markdown,
        "turn_started_at": turn_started_at.isoformat(),
        "turn_completed_at": turn_completed_at.isoformat(),
        "filter_context": None,
        "active_page": None,
        "pending_query": None,
        "estimated_cost": None,
        "approval_decision": None,
        "chart_url": None,
        "verified": False,
        "verification_retry_count": 0,
        "length_retry_count": 0,
        "needs_approval": False,
        "cost_cap_exceeded": False,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "llm_calls": 0,
        "bytes_consumed": 0,
        "iteration_count": 0,
        "cancelled": False,
        "iteration_cap_hit": False,
        "tool_calls": [],
        "claims": [],
        "errors": [],
        "suggested_follow_ups": [],
    }


async def write_telemetry_row(row: dict) -> list:
    """Awaited, never backgrounded (.claude/rules/telemetry.md) — asyncio.to_thread
    hands the blocking BigQuery call to a worker thread, but the caller still
    waits for it, so the response can't be sent until this returns.

    No home yet — called from post_ask in Phase 1; moves to the orchestrator's
    finalize node once the graph exists in Phase 3."""
    table_ref = f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{TELEMETRY_TABLE}"
    return await asyncio.to_thread(get_bq_client().insert_rows_json, table_ref, [row])
