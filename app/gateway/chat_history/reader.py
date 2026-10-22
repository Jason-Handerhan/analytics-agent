import asyncio
import json

from google.cloud import bigquery

from app.config import (
    CHAT_HISTORY_DATASET,
    CHAT_HISTORY_TABLE,
    CONVERSATION_LIST_LIMIT,
    CONVERSATION_TITLE_MAX_CHARS,
    DELETED_CONVERSATIONS_TABLE,
    GCP_PROJECT_ID,
)
from app.gateway.models import ConversationSummary, ConversationTurn
from app.orchestrator.shared_helpers import get_turn_writer_client

GET_HISTORY_SQL = f"""
SELECT turn_data
FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CHAT_HISTORY_TABLE}`
WHERE conversation_id = @conversation_id
  AND conversation_id NOT IN (SELECT conversation_id FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{DELETED_CONVERSATIONS_TABLE}`)
ORDER BY completed_at ASC
"""

LIST_CONVERSATIONS_SQL = f"""
WITH ranked_turns AS (
  SELECT conversation_id, question, completed_at,
         ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY completed_at ASC) AS rn,
         MAX(completed_at) OVER (PARTITION BY conversation_id) AS last_activity_at
  FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{CHAT_HISTORY_TABLE}`
  WHERE user_id = @user_id
    AND conversation_id NOT IN (SELECT conversation_id FROM `{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{DELETED_CONVERSATIONS_TABLE}`)
)
SELECT conversation_id, question AS first_question, last_activity_at
FROM ranked_turns
WHERE rn = 1
ORDER BY last_activity_at DESC
LIMIT @limit
"""


def _truncate_title(question: str) -> str:
    if len(question) <= CONVERSATION_TITLE_MAX_CHARS:
        return question
    truncated = question[:CONVERSATION_TITLE_MAX_CHARS - 1]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.rstrip() + "..."


async def list_conversations(user_id: str) -> list[ConversationSummary]:
    """This user's past conversations, most recent first -- one row per
    conversation_id, titled from its first question."""
    def _run() -> list[ConversationSummary]:
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
            bigquery.ScalarQueryParameter("limit", "INT64", CONVERSATION_LIST_LIMIT),
        ])
        rows = get_turn_writer_client().query(LIST_CONVERSATIONS_SQL, job_config=job_config).result()
        return [
            ConversationSummary(
                conversation_id=row["conversation_id"],
                title=_truncate_title(row["first_question"]),
                last_activity_at=row["last_activity_at"],
            )
            for row in rows
        ]
    return await asyncio.to_thread(_run)


async def get_conversation_history(conversation_id: str) -> list[ConversationTurn]:
    """Every turn for one conversation, oldest first """
    def _run() -> list[ConversationTurn]:
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("conversation_id", "STRING", conversation_id),
        ])
        rows = get_turn_writer_client().query(GET_HISTORY_SQL, job_config=job_config).result()
        return [ConversationTurn(**json.loads(row["turn_data"])) for row in rows]
    return await asyncio.to_thread(_run)
