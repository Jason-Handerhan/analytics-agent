import asyncio
import json
from datetime import datetime

from google.cloud import bigquery

from app.config import CHAT_HISTORY_DATASET, CONVERSATION_FINDINGS_TABLE, GCP_PROJECT_ID
from app.shared_helpers import get_turn_writer_client

_FINDINGS_TABLE = f"`{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CONVERSATION_FINDINGS_TABLE}`"

# Turn numbers are ROW_NUMBER() over the conversation's verified turns, not stored.
OVERVIEW_SQL = f"""
SELECT ROW_NUMBER() OVER (ORDER BY answered_at) AS turn,
       answered_at, approx_tokens, sources, title, topics, entities, question
FROM {_FINDINGS_TABLE}
WHERE conversation_id = @conversation_id
ORDER BY turn
"""

TURNS_SQL = f"""
SELECT * FROM (
  SELECT ROW_NUMBER() OVER (ORDER BY answered_at) AS turn,
         answered_at, question, answer_markdown, verified_numbers,
         sources, title, topics, entities, filter_context, active_page, tool_calls
  FROM {_FINDINGS_TABLE}
  WHERE conversation_id = @conversation_id)
WHERE turn BETWEEN @start_turn AND @end_turn
ORDER BY turn
"""

COMPACTION_SQL = f"""
SELECT answered_at, question, answer_markdown, verified_numbers, title, topics, entities
FROM {_FINDINGS_TABLE}
WHERE conversation_id = @conversation_id
  AND answered_at > COALESCE(@after, TIMESTAMP '1970-01-01')
  AND answered_at < @before
ORDER BY answered_at
"""


async def _run_query(sql: str, query_parameters: list[bigquery.ScalarQueryParameter]) -> list[dict]:
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

    def _run() -> list[dict]:
        rows = get_turn_writer_client().query(sql, job_config=job_config).result()
        return [dict(row) for row in rows]
    return await asyncio.to_thread(_run)


async def fetch_overview_rows(conversation_id: str) -> list[dict]:
    """One lightweight row per verified turn -- no answer text."""
    rows = await _run_query(OVERVIEW_SQL, [
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
    ])
    for row in rows:
        row["answered_at"] = row["answered_at"].isoformat()
    return rows


async def fetch_turns(
    conversation_id: str, start_turn: int, end_turn: int, include_tool_calls: bool
) -> list[dict]:
    """Full rows for turns start_turn..end_turn inclusive. tool_calls only with
    include_tool_calls, with each call's args decoded."""
    rows = await _run_query(TURNS_SQL, [
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
        bigquery.ScalarQueryParameter("start_turn", "INT64", start_turn),
        bigquery.ScalarQueryParameter("end_turn", "INT64", end_turn),
    ])
    for row in rows:
        row["answered_at"] = row["answered_at"].isoformat()
        row["filter_context"] = json.loads(row["filter_context"])
        if include_tool_calls:
            row["tool_calls"] = [
                {"name": tc["name"], "args": json.loads(tc["args"]), "ref_id": tc["ref_id"]}
                for tc in row["tool_calls"]
            ]
        else:
            del row["tool_calls"]
    return rows


async def fetch_compaction_rows(
    conversation_id: str, after: datetime | None, before: datetime
) -> list[dict]:
    """The ledger rows answered after `after` (no lower bound when None) and before `before`,
    oldest first, with answered_at as a datetime."""
    return await _run_query(COMPACTION_SQL, [
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
        bigquery.ScalarQueryParameter("after", "TIMESTAMP", after),
        bigquery.ScalarQueryParameter("before", "TIMESTAMP", before),
    ])
