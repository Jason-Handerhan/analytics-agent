"""agent_chat_history.turns's BigQuery schema -- one definition, imported by
writer.py (insert_rows/selected_fields) and create_table.py (table creation).
Bare SchemaField objects, no I/O, safe to import eagerly, same reasoning as
app/telemetry/schema.py.
"""
from google.cloud import bigquery

SCHEMA = [
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),  # claims["oid"]
    bigquery.SchemaField("completed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("question", "STRING", mode="REQUIRED"),  
    bigquery.SchemaField("turn_data", "STRING", mode="REQUIRED"),  # json.dumps {question, answer_html, sources, suggested_follow_ups, needs_approval}
]
