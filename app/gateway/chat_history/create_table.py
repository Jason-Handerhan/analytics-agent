"""Creates agent_chat_history.turns and agent_chat_history.deleted_conversations
from app/gateway/chat_history/schema.py's CHAT_HISTORY_SCHEMA/DELETED_CONVERSATIONS_SCHEMA.
Run manually after a schema change:
    uv run python -m app.gateway.chat_history.create_table
"""
from google.cloud import bigquery

from app.config import (
    CHAT_HISTORY_DATASET,
    CHAT_HISTORY_TABLE,
    DELETED_CONVERSATIONS_TABLE,
    GCP_PROJECT_ID,
)
from app.gateway.chat_history.schema import CHAT_HISTORY_SCHEMA, DELETED_CONVERSATIONS_SCHEMA

LOCATION = "US"


def _create_table(client: bigquery.Client, table_id: str, schema: list, clustering_fields: list[str]) -> None:
    table_ref = bigquery.Table(f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{table_id}", schema=schema)
    table_ref.clustering_fields = clustering_fields
    table = client.create_table(table_ref, exists_ok=True)
    print(f"Table ready: {table.project}.{table.dataset_id}.{table.table_id} "
          f"(clustered on {clustering_fields})")

    fetched = client.get_table(f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}.{table_id}")
    print(f"{len(fetched.schema)} top-level fields, {fetched.num_rows} rows")


def main() -> None:
    client = bigquery.Client(project=GCP_PROJECT_ID)

    dataset_ref = bigquery.Dataset(f"{GCP_PROJECT_ID}.{CHAT_HISTORY_DATASET}")
    dataset_ref.location = LOCATION
    dataset = client.create_dataset(dataset_ref, exists_ok=True)
    print(f"Dataset ready: {dataset.dataset_id} ({dataset.location})")

    _create_table(client, CHAT_HISTORY_TABLE, CHAT_HISTORY_SCHEMA, ["user_id", "conversation_id"])
    _create_table(client, DELETED_CONVERSATIONS_TABLE, DELETED_CONVERSATIONS_SCHEMA, ["conversation_id"])


if __name__ == "__main__":
    main()
