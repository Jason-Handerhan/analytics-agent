"""conversation_memory's BigQuery schemas -- imported by writer.py and
create_table.py."""
from google.cloud import bigquery

# conversation_findings -- one row per verified turn
CONVERSATION_FINDINGS_SCHEMA = [
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),  # claims["oid"]
    bigquery.SchemaField("answered_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("question", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("answer_markdown", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("verified_numbers", "FLOAT64", mode="REPEATED"),
    bigquery.SchemaField("approx_tokens", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("filter_context", "STRING"),  # json.dumps
    bigquery.SchemaField("active_page", "STRING"),
    bigquery.SchemaField("sources", "STRING", mode="REPEATED"),
    bigquery.SchemaField("title", "STRING"),  # tags written by the answering model (submit_answer)
    bigquery.SchemaField("topics", "STRING", mode="REPEATED"),
    bigquery.SchemaField("entities", "STRING", mode="REPEATED"),
    bigquery.SchemaField(
        "tool_calls", "RECORD", mode="REPEATED",
        fields=[
            bigquery.SchemaField("name", "STRING"),
            bigquery.SchemaField("args", "STRING"),  # json.dumps
            bigquery.SchemaField("ref_id", "STRING"),
        ],
    ),
    bigquery.SchemaField("embedding", "FLOAT64", mode="REPEATED"),
    bigquery.SchemaField("embedding_model", "STRING"),
    bigquery.SchemaField("embedded_text", "STRING"),  # the exact text that was embedded
]

# turn_range_summaries -- one row per compaction, summarizing a range of verified turns
TURN_RANGE_SUMMARIES_SCHEMA = [
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),  # claims["oid"]
    bigquery.SchemaField("covers_from", "TIMESTAMP", mode="REQUIRED"),  # answered_at bounds of the summarized rows
    bigquery.SchemaField("covers_to", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("first_turn", "INT64", mode="REQUIRED"),  # conversation_findings turn numbers
    bigquery.SchemaField("last_turn", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("title", "STRING"),
    bigquery.SchemaField("summary_markdown", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("topics", "STRING", mode="REPEATED"),
    bigquery.SchemaField("approx_tokens", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
]
