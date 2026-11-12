"""Creates telemetry.coding_task_telemetry from schema.py's SCHEMA. The telemetry dataset must exist (app/telemetry/create_table.py creates it).
Run manually after a schema change:
    uv run python -m coding_agent_app.telemetry.create_table
"""
from google.cloud import bigquery

from app.config import CODING_TASK_TELEMETRY_TABLE, GCP_PROJECT_ID, TELEMETRY_DATASET
from coding_agent_app.telemetry.schema import SCHEMA


def main() -> None:
    client = bigquery.Client(project=GCP_PROJECT_ID)

    table_id = f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{CODING_TASK_TELEMETRY_TABLE}"
    table = client.create_table(bigquery.Table(table_id, schema=SCHEMA), exists_ok=True)
    print(f"Table ready: {table.project}.{table.dataset_id}.{table.table_id}")

    fetched = client.get_table(table_id)
    print(f"{len(fetched.schema)} top-level fields, {fetched.num_rows} rows")


if __name__ == "__main__":
    main()
