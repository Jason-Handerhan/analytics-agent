"""Creates agent_chat_history.conversation_findings and .turn_range_summaries from
app/orchestrator/conversation_memory/schema.py. Safe to re-run -- an existing table
is left as it is. Run manually after adding a table:
    uv run python -m app.orchestrator.conversation_memory.create_table
"""
from google.cloud import bigquery

from app.config import (
    CHAT_HISTORY_DATASET,
    CONVERSATION_FINDINGS_TABLE,
    GCP_PROJECT_ID,
    TURN_RANGE_SUMMARIES_TABLE,
)
from app.orchestrator.conversation_memory.schema import (
    CONVERSATION_FINDINGS_SCHEMA,
    TURN_RANGE_SUMMARIES_SCHEMA,
)


def _create_table(client: bigquery.Client, table_name: str, schema: list) -> None:
    table_id = f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{table_name}"
    clustering_fields = ["user_id", "conversation_id"]

    table_ref = bigquery.Table(table_id, schema=schema)
    table_ref.clustering_fields = clustering_fields
    table = client.create_table(table_ref, exists_ok=True)
    print(f"Table ready: {table.project}.{table.dataset_id}.{table.table_id} "
          f"(clustered on {clustering_fields})")

    fetched = client.get_table(table_id)
    print(f"{len(fetched.schema)} top-level fields, {fetched.num_rows} rows")


def main() -> None:
    client = bigquery.Client(project=GCP_PROJECT_ID)
    _create_table(client, CONVERSATION_FINDINGS_TABLE, CONVERSATION_FINDINGS_SCHEMA)
    _create_table(client, TURN_RANGE_SUMMARIES_TABLE, TURN_RANGE_SUMMARIES_SCHEMA)


if __name__ == "__main__":
    main()
