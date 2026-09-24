import asyncio
import json
from datetime import datetime
from functools import lru_cache

from google.cloud import bigquery

from app.config import GCP_PROJECT_ID, TELEMETRY_DATASET, TELEMETRY_TABLE


@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


def _serialize_tool_call(tc: dict) -> dict:
    """Matches TOOL_CALL_FIELDS -- args/result are STRING columns, JSON-dumped."""
    return {
        "id": tc["id"],
        "name": tc["name"],
        "started_at": tc["started_at"].isoformat(),
        "completed_at": tc["completed_at"].isoformat(),
        "args": json.dumps(tc["args"]),
        "query_text": tc["query_text"],
        "result": json.dumps(tc["result"]),
        "success": tc["success"],
        "error": tc["error"],
    }


def _serialize_error(e: dict) -> dict:
    """Matches ERROR_FIELDS."""
    return {
        "stage": e["stage"],
        "error_type": e["error_type"],
        "message": e["message"],
        "occurred_at": e["occurred_at"].isoformat(),
        "tool_call_id": e["tool_call_id"],
    }


def build_telemetry_row(
    conversation_id: str,
    user_id: str,
    question: str,
    answer_markdown: str,
    turn_started_at: datetime,
    turn_completed_at: datetime,
    filter_context: list[dict],
    active_page: str | None,
    tool_calls: list[dict],
    errors: list[dict],
    prompt_tokens: int,
    completion_tokens: int,
    llm_calls: int,
    bytes_consumed: int,
    iteration_count: int,
    verified: bool,
    verification_retry_count: int,
    length_retry_count: int,
    needs_approval: bool,
    cost_cap_exceeded: bool,
    cancelled: bool,
    iteration_cap_hit: bool,
    estimated_cost: str | None,
    pending_queries: list[dict],
    deferred_dax: list[dict],
    chart_url: str | None,
) -> dict:
    """Full agent_telemetry row. `pending_query`, `approval_decision`, and
    `suggested_follow_ups` stay null/empty -- nothing produces them yet
    (route_entry/execute_approved, the approval response path, and Phase 4
    respectively), so there's no AgentState field to pass through for them."""
    pending_query = max((pq["query"] for pq in pending_queries), key=len, default=None)
    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "question": question,
        "answer_markdown": answer_markdown,
        "turn_started_at": turn_started_at.isoformat(),
        "turn_completed_at": turn_completed_at.isoformat(),
        "filter_context": json.dumps(filter_context),
        "active_page": active_page,
        "pending_query": pending_query,
        "pending_queries": pending_queries,
        "deferred_dax": deferred_dax,
        "estimated_cost": estimated_cost,
        "approval_decision": None,
        "chart_url": chart_url,
        "verified": verified,
        "verification_retry_count": verification_retry_count,
        "length_retry_count": length_retry_count,
        "needs_approval": needs_approval,
        "cost_cap_exceeded": cost_cap_exceeded,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "llm_calls": llm_calls,
        "bytes_consumed": bytes_consumed,
        "iteration_count": iteration_count,
        "cancelled": cancelled,
        "iteration_cap_hit": iteration_cap_hit,
        "tool_calls": [_serialize_tool_call(tc) for tc in tool_calls],
        "errors": [_serialize_error(e) for e in errors],
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
