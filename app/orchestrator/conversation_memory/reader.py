import asyncio
import json
from datetime import datetime

from google.cloud import bigquery

from app.config import (
    CHAT_HISTORY_DATASET,
    CONVERSATION_FINDINGS_TABLE,
    DELETED_CONVERSATIONS_TABLE,
    GCP_PROJECT_ID,
    TURN_RANGE_SUMMARIES_TABLE,
)
from app.shared_helpers import get_turn_writer_client

# --- Shared ---------------------------------------------------------------------------------

_FINDINGS_TABLE = f"`{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CONVERSATION_FINDINGS_TABLE}`"


async def _run_query(sql: str, query_parameters: list[bigquery.ScalarQueryParameter]) -> list[dict]:
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)

    def _run() -> list[dict]:
        rows = get_turn_writer_client().query(sql, job_config=job_config).result()
        return [dict(row) for row in rows]
    return await asyncio.to_thread(_run)


# --- Recall: overview and turns -------------------------------------------------------------

# Turn numbers are ROW_NUMBER() over the conversation's verified turns, not stored.
OVERVIEW_SQL = f"""
SELECT ROW_NUMBER() OVER (ORDER BY answered_at) AS turn,
       answered_at, approx_tokens, sources, title, topics, entities, question
FROM {_FINDINGS_TABLE}
WHERE conversation_id = @conversation_id
ORDER BY turn
"""


async def fetch_overview_rows(conversation_id: str) -> list[dict]:
    """One lightweight row per verified turn -- no answer text."""
    rows = await _run_query(OVERVIEW_SQL, [
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
    ])
    for row in rows:
        row["answered_at"] = row["answered_at"].isoformat()
    return rows


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


SUMMARIES_SQL = f"""
SELECT first_turn, last_turn, covers_from, covers_to, title, topics,
       summary_markdown AS summary
FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{TURN_RANGE_SUMMARIES_TABLE}`
WHERE conversation_id = @conversation_id
ORDER BY first_turn
"""


async def fetch_summaries(conversation_id: str) -> list[dict]:
    """One row per compaction summary, in turn order."""
    rows = await _run_query(SUMMARIES_SQL, [
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
    ])
    for row in rows:
        row["covers_from"] = row["covers_from"].isoformat()
        row["covers_to"] = row["covers_to"].isoformat()
    return rows


# --- Compaction -----------------------------------------------------------------------------

COMPACTION_SQL = f"""
SELECT answered_at, question, answer_markdown, verified_numbers, title, topics, entities
FROM {_FINDINGS_TABLE}
WHERE conversation_id = @conversation_id
  AND answered_at > COALESCE(@after, TIMESTAMP '1970-01-01')
  AND answered_at < @before
ORDER BY answered_at
"""


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


# --- Vector Search ---------------------------------------------------------------------------------

# One user's rows, or one conversation's when @conversation_id is set. s
# Excludes soft deleted conversations
_SCOPE_FILTER = f"""user_id = @user_id
  AND conversation_id = COALESCE(@conversation_id, conversation_id)
  AND conversation_id NOT IN (
    SELECT conversation_id FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{DELETED_CONVERSATIONS_TABLE}`)"""

# has_any_tag_like matches case-insensitively on substrings, any of the wanted tags; an
# empty wanted list matches every row.
VECTOR_SEARCH_SQL = f"""
CREATE TEMP FUNCTION has_any_tag_like(tags ARRAY<STRING>, wanted ARRAY<STRING>) AS (
  COALESCE(ARRAY_LENGTH(wanted), 0) = 0 OR EXISTS (
    SELECT 1 FROM UNNEST(tags) AS tag, UNNEST(wanted) AS wanted_tag
    WHERE LOWER(tag) LIKE CONCAT('%', LOWER(wanted_tag), '%'))
);

SELECT answered_at, conversation_id, question, answer_markdown, verified_numbers,
       sources, title, topics, entities, filter_context, active_page, tool_calls,
       COSINE_DISTANCE(embedding, @query_vector) AS distance
FROM {_FINDINGS_TABLE}
WHERE {_SCOPE_FILTER}
  AND ARRAY_LENGTH(embedding) > 0
  AND has_any_tag_like(topics, @topics)
  AND has_any_tag_like(entities, @entities)
ORDER BY distance
LIMIT @top_k
"""


async def fetch_vector_search_hits(
    user_id: str, conversation_id: str | None, query_vector: list[float], top_k: int,
    topics: list[str], entities: list[str], include_tool_calls: bool,
) -> list[dict]:
    """The top_k rows nearest the query vector, closest first, with their distance. Searches one
    conversation, or all of the user's when conversation_id is None. Rows without an embedding
    are skipped, and the embedding itself is not returned. tool_calls only with
    include_tool_calls, with each call's args decoded."""
    rows = await _run_query(VECTOR_SEARCH_SQL, [
        bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
        bigquery.ArrayQueryParameter("query_vector", "FLOAT64", query_vector),
        bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
        bigquery.ArrayQueryParameter("topics", "STRING", topics),
        bigquery.ArrayQueryParameter("entities", "STRING", entities),
    ])
    for row in rows:
        row["answered_at"] = row["answered_at"].isoformat()
        row["filter_context"] = json.loads(row["filter_context"])
        row["distance"] = round(row["distance"], 4)
        if include_tool_calls:
            row["tool_calls"] = [
                {"name": tc["name"], "args": json.loads(tc["args"]), "ref_id": tc["ref_id"]}
                for tc in row["tool_calls"]
            ]
        else:
            del row["tool_calls"]
    return rows


TAG_COUNTS_SQL = f"""
SELECT kind, tag, tag_count FROM (
  SELECT kind, tag, COUNT(*) AS tag_count,
         ROW_NUMBER() OVER (PARTITION BY kind ORDER BY COUNT(*) DESC, tag) AS tag_rank
  FROM (
    SELECT 'topics' AS kind, LOWER(tag) AS tag
    FROM {_FINDINGS_TABLE}, UNNEST(topics) AS tag
    WHERE {_SCOPE_FILTER}
    UNION ALL
    SELECT 'entities' AS kind, LOWER(tag) AS tag
    FROM {_FINDINGS_TABLE}, UNNEST(entities) AS tag
    WHERE {_SCOPE_FILTER})
  GROUP BY kind, tag)
WHERE tag_rank <= @limit
ORDER BY kind, tag_rank
"""


async def fetch_tag_counts(user_id: str, conversation_id: str | None, limit: int) -> dict[str, list[dict]]:
    """The most used topics and entities, lowercased, with how many turns carry each: up to
    `limit` of each, most used first. Same scope as fetch_vector_search_hits."""
    rows = await _run_query(TAG_COUNTS_SQL, [
        bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
        bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ])
    counts = {"topics": [], "entities": []}
    for row in rows:
        counts[row["kind"]].append({"tag": row["tag"], "count": row["tag_count"]})
    return counts
