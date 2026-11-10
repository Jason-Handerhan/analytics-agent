"""The coding agent's tools, run in the job process: list_tables, get_table_schema, dry_run_sql, and the reviewer's show_changes (docs/coding-agent.md)."""
import asyncio
import json
import logging
import shlex
from functools import lru_cache
from typing import Literal

from google.api_core.exceptions import ClientError, NotFound
from google.cloud import bigquery
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

from app.config import CODING_AGENT_DATASETS, GCP_PROJECT_ID

logger = logging.getLogger(__name__)


# --- BigQuery access (shared by list_tables, get_table_schema and dry_run_sql) ---------------------------

@lru_cache
def get_bq_client() -> bigquery.Client:
    """The job runs as the coding agent's service account, so default credentials are that account. The notebook points
    this at a client that impersonates it, so what it tests is the account's own access."""
    return bigquery.Client(project=GCP_PROJECT_ID)


def bigquery_failure(error: Exception) -> ToolException:
    """A BigQuery call failed for a reason that is not about the agent's request. Every tool sets handle_tool_error, so the
    agent gets this as an error message and can retry or carry on; this agent's framework would stop the run on any other
    exception from a tool (the main agent's own loop catches ToolError, deep agents' tool node does not)."""
    logger.error("BigQuery tool call failed", exc_info=error)
    return ToolException(f"BigQuery call failed ({type(error).__name__}: {error}). This is a problem with BigQuery or its "
                         "access, not with your request. Retry once, or carry on without this tool.")


# --- list_tables ----------------------------------------------------------------------------------------

LIST_TABLES_DESCRIPTION = """List the tables in a BigQuery dataset with each table's description and column count.
This is what exists in BigQuery today, which can lag the repo's SQL until the pipeline runs."""


class ListTablesArgs(BaseModel):
    dataset: Literal[CODING_AGENT_DATASETS] = Field(description="The dataset to list.")


@tool(args_schema=ListTablesArgs, description=LIST_TABLES_DESCRIPTION)
async def list_tables(dataset: Literal[CODING_AGENT_DATASETS]) -> str:
    client = get_bq_client()
    try:
        dataset_tables = await asyncio.to_thread(list, client.list_tables(f"{GCP_PROJECT_ID}.{dataset}"))
        tables = await asyncio.gather(*(asyncio.to_thread(client.get_table, item.reference) for item in dataset_tables))
    except Exception as error:
        raise bigquery_failure(error) from error
    return json.dumps([{"table": table.table_id, "description": table.description, "columns": len(table.schema)} for table in tables], ensure_ascii=False)


list_tables.handle_tool_error = True


# --- get_table_schema -----------------------------------------------------------------------------------

GET_TABLE_SCHEMA_DESCRIPTION = """Show a table's description, row count and each column's name, type, mode and description,
as materialized in BigQuery today.
Use it to confirm a column exists and its type before you rely on it."""


class GetTableSchemaArgs(BaseModel):
    dataset: Literal[CODING_AGENT_DATASETS] = Field(description="The dataset the table is in.")
    table: str = Field(description="The table name, without the dataset.")


@tool(args_schema=GetTableSchemaArgs, description=GET_TABLE_SCHEMA_DESCRIPTION)
async def get_table_schema(dataset: Literal[CODING_AGENT_DATASETS], table: str) -> str:
    client = get_bq_client()
    try:
        found = await asyncio.to_thread(client.get_table, f"{GCP_PROJECT_ID}.{dataset}.{table}")
    except NotFound:
        try:
            dataset_tables = await asyncio.to_thread(list, client.list_tables(f"{GCP_PROJECT_ID}.{dataset}"))
        except Exception as error:
            raise bigquery_failure(error) from error
        names = [item.table_id for item in dataset_tables]
        raise ToolException(f"Table '{table}' not found in {dataset}. Tables there: {', '.join(names)}")
    except Exception as error:
        raise bigquery_failure(error) from error
    return json.dumps({"table": f"{dataset}.{table}", "description": found.description, "rows": found.num_rows,
                       "columns": [field.to_api_repr() for field in found.schema]}, ensure_ascii=False)


get_table_schema.handle_tool_error = True


# --- dry_run_sql (built per run, because it reads the run's sandbox) ------------------------------------

DRY_RUN_SQL_DESCRIPTION = """Check with BigQuery that the SQL in a file is valid, and see the columns and types it would return,
without running it. Nothing executes and no rows come back. Pass a file that the sqlx skill's compose_query.py wrote
(/skills/sqlx/SKILL.md): SQL of a table you changed only validates once that script has built it."""


class DryRunSqlArgs(BaseModel):
    sql_file: str = Field(description="Path of a composed SQL file printed by compose_query.py, "
                                      "such as /tmp/composed/final_ml_features_table.sql.")


def build_dry_run_sql(sandbox):
    @tool(args_schema=DryRunSqlArgs, description=DRY_RUN_SQL_DESCRIPTION)
    async def dry_run_sql(sql_file: str) -> str:
        file_contents = await asyncio.to_thread(sandbox.run, f"cat {shlex.quote(sql_file)}")
        if file_contents.exit_code != 0:
            raise ToolException(f"Cannot read {sql_file}: {file_contents.stderr.strip()}. Build the SQL with the sqlx skill, "
                                "or check the path with ls /tmp/composed.")
        sql = file_contents.stdout
        try:
            job = await asyncio.to_thread(get_bq_client().query, sql, job_config=bigquery.QueryJobConfig(dry_run=True))
        except ClientError as error:  # the query is wrong, or reads a table outside the allowed datasets
            raise ToolException(f"Invalid query in {sql_file}: {error.message}") from error
        except Exception as error:
            raise bigquery_failure(error) from error
        return json.dumps({"sql_file": sql_file, "statement_type": job.statement_type, "bytes_processed": job.total_bytes_processed,
                           "columns": [field.to_api_repr() for field in job.schema or []]})

    dry_run_sql.handle_tool_error = True
    return dry_run_sql


# --- show_changes (the reviewer's tool) -----------------------------------------------------------------

def build_show_changes(sandbox, work_dir: str, sandbox_start_commit: str):
    """The one tool that is built per run, because it needs that run's sandbox and start commit."""

    @tool
    def show_changes() -> str:
        """Show every change made to the repo so far as one diff against the commit the work started from, including new files."""
        sandbox.run(f"git -C {work_dir} add -A")
        return sandbox.run(f"git -C {work_dir} diff --cached {sandbox_start_commit}").stdout

    return show_changes
