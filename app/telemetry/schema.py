"""agent_telemetry's BigQuery schema -- one definition, imported by writer.py
(insert_rows/selected_fields) and create_table.py (table creation). Bare
SchemaField objects, no I/O.
"""
from google.cloud import bigquery

TOOL_CALL_FIELDS = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),         # join target for errors.tool_call_id
    bigquery.SchemaField("name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("completed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("args", "STRING", mode="REQUIRED"),       # json.dumps'd
    bigquery.SchemaField("query_text", "STRING", mode="NULLABLE"), # SQL/DAX only
    bigquery.SchemaField("result", "STRING", mode="REQUIRED"),     # json.dumps'd
    bigquery.SchemaField("success", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("error", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("bytes_billed", "INTEGER", mode="NULLABLE"),  # BigQuery calls only
]

ERROR_FIELDS = [
    bigquery.SchemaField("stage", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("error_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("message", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("occurred_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("tool_call_id", "STRING", mode="NULLABLE"),  # join to tool_calls.id; null if no single call
]

# Queries held for approval; id matches the tool_calls entry that requested it.
PENDING_QUERY_FIELDS = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("query", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("estimated_bytes", "INTEGER", mode="NULLABLE"),  # dry-run estimate
]

# Vestigial: carried on the pending approval but never populated.
DEFERRED_DAX_FIELDS = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("dax", "STRING", mode="REQUIRED"),
]

SCHEMA = [
    # --- Turn: who asked what, with which model, and when ---
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),        # claims["oid"]
    bigquery.SchemaField("model", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("model_provider", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("question", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("answer_markdown", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("turn_started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("turn_completed_at", "TIMESTAMP", mode="REQUIRED"),

    # --- Dashboard context the question was asked in ---
    bigquery.SchemaField("filter_context", "STRING", mode="NULLABLE"),  # json.dumps'd list[dict]
    bigquery.SchemaField("active_page", "STRING", mode="NULLABLE"),

    # --- Answer: whether it was verified and what came with it ---
    bigquery.SchemaField("verified", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("verification_retry_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("length_retry_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("clarifying_question", "STRING", mode="NULLABLE"),  # set only on question turns
    bigquery.SchemaField("suggested_follow_ups", "STRING", mode="REPEATED"),
    bigquery.SchemaField("all_prose_numeric_claims", "FLOAT", mode="REPEATED"),  # numbers the model declared in prose
    bigquery.SchemaField("chart_urls", "STRING", mode="REPEATED"),

    # --- Tool calls and errors ---
    bigquery.SchemaField("tool_calls", "RECORD", mode="REPEATED", fields=TOOL_CALL_FIELDS),
    bigquery.SchemaField("errors", "RECORD", mode="REPEATED", fields=ERROR_FIELDS),

    # --- Cost and usage ---
    bigquery.SchemaField("prompt_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("completion_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("llm_calls", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("bytes_consumed", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("iteration_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("iteration_cap_hit", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("cost_cap_exceeded", "BOOLEAN", mode="REQUIRED"),

    # --- Approval and cancellation ---
    bigquery.SchemaField("needs_approval", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("approval_decision", "STRING", mode="NULLABLE"),  # "approved" | "rejected"
    bigquery.SchemaField("pending_query", "STRING", mode="NULLABLE"),   # the largest pending query
    bigquery.SchemaField("pending_queries", "RECORD", mode="REPEATED", fields=PENDING_QUERY_FIELDS),
    bigquery.SchemaField("deferred_dax", "RECORD", mode="REPEATED", fields=DEFERRED_DAX_FIELDS),
    bigquery.SchemaField("estimated_cost", "STRING", mode="NULLABLE"),  # display dollars, not numeric
    bigquery.SchemaField("cancelled", "BOOLEAN", mode="REQUIRED"),

    # --- Compaction ---
    bigquery.SchemaField("compacted", "BOOLEAN", mode="NULLABLE"),
    bigquery.SchemaField("compaction_failed", "BOOLEAN", mode="NULLABLE"),
    bigquery.SchemaField("compaction_prompt_tokens", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("compaction_completion_tokens", "INTEGER", mode="NULLABLE"),
]
