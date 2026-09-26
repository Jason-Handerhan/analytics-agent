"""agent_telemetry's BigQuery schema -- one definition, imported by writer.py
(insert_rows/selected_fields) and create_table.py (table creation). Bare
SchemaField objects, no I/O, safe to import eagerly (.claude/rules/telemetry.md).
"""
from google.cloud import bigquery

TOOL_CALL_FIELDS = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),         # tool_call_id --
                                                                    # direct join target
                                                                    # for errors.tool_call_id
    bigquery.SchemaField("name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("completed_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("args", "STRING", mode="REQUIRED"),       # json.dumps'd
    bigquery.SchemaField("query_text", "STRING", mode="NULLABLE"), # SQL/DAX only
    bigquery.SchemaField("result", "STRING", mode="REQUIRED"),     # json.dumps'd
    bigquery.SchemaField("success", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("error", "STRING", mode="NULLABLE"),
]

ERROR_FIELDS = [
    bigquery.SchemaField("stage", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("error_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("message", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("occurred_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("tool_call_id", "STRING", mode="NULLABLE"),  # direct join to
                                                                       # tool_calls.id;
                                                                       # null for a failure
                                                                       # with no single call
]

# {id, query} / {id, dax} -- matches AgentState.pending_queries/deferred_dax
# exactly, so a paused turn's pending queries stay correlatable back to their
# originating tool_calls entry -- same reasoning as errors.tool_call_id, not a
# flat REPEATED STRING that loses the id.
PENDING_QUERY_FIELDS = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("query", "STRING", mode="REQUIRED"),
]

DEFERRED_DAX_FIELDS = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("dax", "STRING", mode="REQUIRED"),
]

SCHEMA = [
    # --- Required, real from Phase 1 ---
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),        # claims["oid"]
    bigquery.SchemaField("question", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("answer_markdown", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("turn_started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("turn_completed_at", "TIMESTAMP", mode="REQUIRED"),

    # --- Nullable: genuinely absent until Phase 3, absence is meaningful ---
    bigquery.SchemaField("filter_context", "STRING", mode="NULLABLE"),  # json.dumps'd list[dict]
    bigquery.SchemaField("active_page", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("pending_query", "STRING", mode="NULLABLE"),   # the largest pending query
    bigquery.SchemaField("estimated_cost", "STRING", mode="NULLABLE"),  # display dollars, not numeric
    bigquery.SchemaField("approval_decision", "STRING", mode="NULLABLE"),  # "approved" | "rejected"
    bigquery.SchemaField("chart_url", "STRING", mode="NULLABLE"),

    # --- Required, writer defaults to False/0 until Phase 3 ---
    bigquery.SchemaField("verified", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("verification_retry_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("length_retry_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("needs_approval", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("cost_cap_exceeded", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("prompt_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("completion_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("llm_calls", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("bytes_consumed", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("iteration_count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("cancelled", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("iteration_cap_hit", "BOOLEAN", mode="REQUIRED"),

    # --- Repeated: empty by default, no NULLABLE/REQUIRED distinction applies ---
    bigquery.SchemaField("tool_calls", "RECORD", mode="REPEATED", fields=TOOL_CALL_FIELDS),
    bigquery.SchemaField("errors", "RECORD", mode="REPEATED", fields=ERROR_FIELDS),
    bigquery.SchemaField("suggested_follow_ups", "STRING", mode="REPEATED"),
    # From submit_answer -- top-level for direct queryability, same reasoning as query_text.
    bigquery.SchemaField("all_prose_numeric_claims", "FLOAT", mode="REPEATED"),
    bigquery.SchemaField("pending_queries", "RECORD", mode="REPEATED", fields=PENDING_QUERY_FIELDS),
    bigquery.SchemaField("deferred_dax", "RECORD", mode="REPEATED", fields=DEFERRED_DAX_FIELDS),
]
