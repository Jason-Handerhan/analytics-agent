"""agent_chat_history's BigQuery schemas -- imported by writer.py and
create_table.py."""
from google.cloud import bigquery

#Chat history schema
CHAT_HISTORY_SCHEMA = [
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),  # claims["oid"]
    bigquery.SchemaField("completed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("question", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("turn_data", "STRING", mode="REQUIRED"),  # json.dumps {question, answer_html, sources, suggested_follow_ups, needs_approval}
]

# deleted_conversations -- soft-delete markers 
DELETED_CONVERSATIONS_SCHEMA = [
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),  # claims["oid"]
    bigquery.SchemaField("deleted_at", "TIMESTAMP", mode="REQUIRED"),
]
