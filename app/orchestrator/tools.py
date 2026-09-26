"""Non-MCP tools: run_bigquery_sql and submit_answer (.claude/rules/tools.md)."""
import asyncio
import concurrent.futures
from datetime import date, time
from decimal import Decimal
from functools import lru_cache

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from langchain_core.tools import tool, ToolException
from pydantic import BaseModel, Field

from app.config import (
    BIGQUERY_ROW_CAP,
    BIGQUERY_TIMEOUT_SECONDS,
    GCP_PROJECT_ID,
    MAX_ANSWER_TABLE_ROWS,
    MAX_BYTES_BILLED,
)


@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


class ToolError(ToolException):
    """ToolException subclass -- handle_tool_error=True converts it to a ToolMessage."""


class BigQuerySqlArgs(BaseModel):
    query: str


@tool(args_schema=BigQuerySqlArgs)
async def run_bigquery_sql(query: str) -> list[dict]:
    """Runs a SQL query against agent_safe (BigQuery) -- upstream/warehouse data:
    raw order and product features, pre-model. The only dataset this can reach
    (IAM-scoped).
    
    Guardrails: results cap at 1,000 rows -- filter or aggregate rather than
    relying on LIMIT, which reduces rows returned but not bytes scanned;
    queries scanning over 1 GiB fail; 40s timeout.
    """

    # bigquery.Client.query() is synchronous, run it in a thread to avoid blocking the event loop.
    def _run() -> list[dict]:
        job_config = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)
        job = get_bq_client().query(query, job_config=job_config)
        try:
            # ONE call -- max_results here, not a second .result() call, which
            # would re-fetch from scratch and defeat the cap.
            rows = job.result(timeout=BIGQUERY_TIMEOUT_SECONDS, max_results=BIGQUERY_ROW_CAP)
        except concurrent.futures.TimeoutError:
            get_bq_client().cancel_job(job.job_id)
            raise ToolError(
                f"Query exceeded the {BIGQUERY_TIMEOUT_SECONDS}s timeout and was "
                "cancelled. Narrow the query (add a filter or aggregate) and try again.")
        except GoogleAPICallError as e:
            #Exceeds maximum_bytes_billed limit
            if any(err.get("reason") == "bytesBilledLimitExceeded" for err in e.errors):
                raise ToolError(
                    f"Query would scan more than the {MAX_BYTES_BILLED / 1024**3:.0f} GiB "
                    "safety limit. Add a filter, select fewer columns, or aggregate to "
                    "reduce the data scanned.")
            # Any other BigQuery-side failure (bad SQL, etc.)
            raise ToolError(f"BigQuery error: {e}")

        # total_rows is the query's true total, unaffected by max_results --
        # fail loudly rather than silently hand back a partial result.
        if rows.total_rows > BIGQUERY_ROW_CAP:
            raise ToolError(
                f"Query returned {BIGQUERY_ROW_CAP}+ rows. Add a filter or "
                "aggregate to narrow it.")

        #Future insurance gaurd if date, time, or Decimal types are returned in the rows. Convert them to JSON-serializable types.
        #As written, build_tool_call_record() fails loudly if it encounters a non-serializable type.
        #This is intentional to ensure inconsistent, hard to work with, formats don't sneak through
        # to Tool_Call_Record.result in telemetry

        def _convert(v):
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, (date, time)):
                return v.isoformat()
            return v

        return [
            {k: _convert(v) for k, v in dict(row).items()}
            for row in rows
        ]
    return await asyncio.to_thread(_run)

#Ensures that ToolError exceptions raised in run_bigquery_sql() are converted to ToolMessage and handled by
# the agent instead of crashing the agent.
run_bigquery_sql.handle_tool_error = True


class SubmitAnswerArgs(BaseModel):
    answer_markdown: str = Field(
        min_length=1,
        description=f"Plain Markdown. Keep any single table to at most {MAX_ANSWER_TABLE_ROWS} rows -- summarize the rest or use generate_chart instead.")
    all_prose_numeric_claims: list[float] = Field(
        description="Every number stated in prose as fact. Never include a number that already appears in a markdown table.")
    suggested_follow_ups: list[str] = Field(
        default=[],
        description="1-3 short, natural follow-up questions, only when one would genuinely help. Leave empty otherwise.")


@tool(args_schema=SubmitAnswerArgs)
async def submit_answer(answer_markdown: str, all_prose_numeric_claims: list[float],
                         suggested_follow_ups: list[str]) -> str:
    """Call this with your final answer once you have everything you need.
    This is how you respond to the user -- do not write your answer as
    plain text. Do not batch this with other tool calls -- if you do,
    the submission is ignored and the loop continues.
    """
    return "Answer recorded."


async def dry_run(query: str) -> int:
    """Prices a query without running it."""
    def _run() -> int:
        job = get_bq_client().query(query, job_config=bigquery.QueryJobConfig(dry_run=True))
        return job.total_bytes_processed
    return await asyncio.to_thread(_run)
