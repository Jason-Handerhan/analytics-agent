"""Creates telemetry.agent_telemetry from app/telemetry/schema.py's SCHEMA
(.claude/rules/telemetry.md). exists_ok=True on both dataset and table --
safe to re-run, and does NOT alter an already-existing table's schema if
one's already there with different fields (BigQuery can't ALTER a column's
mode or rename it -- a real schema change means dropping and recreating).

Run manually after a schema change:
    uv run python -m app.telemetry.create_table
"""
from google.cloud import bigquery

from app.config import GCP_PROJECT_ID, TELEMETRY_DATASET, TELEMETRY_TABLE
from app.telemetry.schema import SCHEMA

LOCATION = "US" 

client = bigquery.Client(project=GCP_PROJECT_ID)

dataset_ref = bigquery.Dataset(f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}")
dataset_ref.location = LOCATION
dataset = client.create_dataset(dataset_ref, exists_ok=True)
print(f"Dataset ready: {dataset.dataset_id} ({dataset.location})")

table_ref = bigquery.Table(f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{TELEMETRY_TABLE}", schema=SCHEMA)
table = client.create_table(table_ref, exists_ok=True)
print(f"Table ready: {table.project}.{table.dataset_id}.{table.table_id}")

fetched = client.get_table(f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{TELEMETRY_TABLE}")
print(f"{len(fetched.schema)} top-level fields, {fetched.num_rows} rows")
