"""coding_task_telemetry's BigQuery schema -- one definition, imported by writer.py (insert_rows/selected_fields) and
create_table.py (table creation). Bare SchemaField objects, no I/O. One row per coding task, written when the task completes.
"""
from google.cloud import bigquery

CHECK_FIELDS = [
    bigquery.SchemaField("name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("passed", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("detail", "STRING", mode="NULLABLE"),      # the tail of the output, only when it failed
]

REVIEW_CRITERION_FIELDS = [
    bigquery.SchemaField("criterion", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("passed", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("gap", "STRING", mode="NULLABLE"),         # what is missing, when it failed
]

TOOL_CALL_FIELDS = [
    bigquery.SchemaField("name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("count", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("failed", "INTEGER", mode="REQUIRED"),
]

SCHEMA = [
    # --- Task: who, which conversation, and when ---
    bigquery.SchemaField("task_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),   # joins to agent_telemetry
    bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("completed_at", "TIMESTAMP", mode="REQUIRED"),

    # --- Plan: what the task was asked to do ---
    bigquery.SchemaField("plan_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("plan_goal", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("base_branch", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("is_revision", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("review_requested", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("plan_json", "STRING", mode="REQUIRED"),         # the whole plan, json.dumps'd

    # --- Outcome: how it ended, and what the agent said about it ---
    bigquery.SchemaField("outcome", "STRING", mode="REQUIRED"),           # succeeded | failed | needs_input | stopped
    bigquery.SchemaField("stop_reason", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("branch", "STRING", mode="NULLABLE"),            # null when nothing was pushed
    bigquery.SchemaField("commit", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("started_from_commit", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("dev_moved", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("report", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("assumptions", "STRING", mode="REPEATED"),
    bigquery.SchemaField("blocking_questions", "STRING", mode="REPEATED"),
    bigquery.SchemaField("follow_up_questions", "STRING", mode="REPEATED"),
    bigquery.SchemaField("patch_problems", "STRING", mode="REPEATED"),
    bigquery.SchemaField("result_json", "STRING", mode="REQUIRED"),       # the whole result, json.dumps'd

    # --- The change: the diff and its size ---
    bigquery.SchemaField("patch", "STRING", mode="NULLABLE"),             # this task's own changes as unified diff text, decoded from the patch bytes; null when there is no patch
    bigquery.SchemaField("files_changed", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("lines_added", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("lines_removed", "INTEGER", mode="REQUIRED"),

    # --- Checks: the automated checks on the finished patch ---
    bigquery.SchemaField("checks", "RECORD", mode="REPEATED", fields=CHECK_FIELDS),
    bigquery.SchemaField("checks_failed", "INTEGER", mode="REQUIRED"),

    # --- Reviewer: its verdict, null where it did not run ---
    bigquery.SchemaField("review_ran", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("review_verdict", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("review_rounds", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("review_unverified", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("review_explanation", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("review_criteria", "RECORD", mode="REPEATED", fields=REVIEW_CRITERION_FIELDS),   # the last round only, every criterion

    # --- Usage: tokens and model calls, agent and reviewer ---
    bigquery.SchemaField("agent_input_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("agent_output_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("agent_cache_read_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("agent_cache_write_tokens", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("agent_models", "STRING", mode="REPEATED"),
    bigquery.SchemaField("agent_llm_calls", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("reviewer_input_tokens", "INTEGER", mode="NULLABLE"),    # the reviewer fields are null when it did not run
    bigquery.SchemaField("reviewer_output_tokens", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("reviewer_cache_read_tokens", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("reviewer_cache_write_tokens", "INTEGER", mode="NULLABLE"),
    bigquery.SchemaField("reviewer_models", "STRING", mode="REPEATED"),
    bigquery.SchemaField("reviewer_llm_calls", "INTEGER", mode="NULLABLE"),

    # --- Process: how often the agent used each tool ---
    bigquery.SchemaField("tool_calls", "RECORD", mode="REPEATED", fields=TOOL_CALL_FIELDS),

    # --- Links: the trace, the deployed revision and the sandbox snapshot ---
    bigquery.SchemaField("langsmith_run_id", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("cloud_run_revision", "STRING", mode="NULLABLE"),
    bigquery.SchemaField("sandbox_snapshot_id", "STRING", mode="NULLABLE"),
]
