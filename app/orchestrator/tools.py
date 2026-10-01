"""Non-MCP tools: run_bigquery_sql, submit_answer, get_measure_dax (.claude/rules/tools.md)."""
import asyncio
import concurrent.futures
from datetime import date, time
from decimal import Decimal
from functools import lru_cache

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from langchain_core.tools import tool, ToolException
from pydantic import BaseModel, Field, create_model

from app.config import (
    BIGQUERY_ROW_CAP,
    BIGQUERY_TIMEOUT_SECONDS,
    GCP_PROJECT_ID,
    MAX_ANSWER_TABLE_ROWS,
    MAX_BYTES_BILLED,
)
from app.mcp_server.chart_tool import GenerateChartArgs
from app.model_schema import MEASURE_DAX, MEASURE_NAMES


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

#Ensures that ToolError exceptions raised in run_bigquery_sql() are converted to a ToolMessage.
run_bigquery_sql.handle_tool_error = True

# Surface Pydantic's own message as an actionable ToolMessage instead of crashing the turn.
run_bigquery_sql.handle_validation_error = lambda e: str(e)


class SubmitAnswerArgs(BaseModel):
    answer_markdown: str = Field(
        min_length=1,
        description=f"Plain Markdown. Keep any single table to at most {MAX_ANSWER_TABLE_ROWS} rows -- "
                    "show the top results and summarize the rest, aggregate to fewer rows in a new query, "
                    "or use generate_chart instead. Include the chart_url from generate_chart here as an "
                    "image -- the user sees no chart otherwise. Escape a literal | inside a table cell as "
                    "\\| or it's read as an extra column and breaks the table.")
    all_prose_numeric_claims: list[float] = Field(
        description="Every number stated in prose as fact. Never include a number that already appears in a markdown table -- table cells are checked separately, automatically, not exempt from verification.")
    suggested_follow_ups: list[str] = Field(
        default=[],
        description="1-3 short, natural follow-up questions -- include these by default, since they help the user continue the conversation. Leave empty only when nothing natural genuinely fits.")


@tool(args_schema=SubmitAnswerArgs)
async def submit_answer(answer_markdown: str, all_prose_numeric_claims: list[float],
                         suggested_follow_ups: list[str]) -> str:
    """Call this with your final answer once you have everything you need.
    This is how you respond to the user -- a plain-text reply will not be
    delivered and the turn will be asked to retry. If you can't fully
    answer within a reasonable number of steps, submit the best partial
    answer available and say plainly it's partial, rather than presenting
    it as complete. Do not batch this with other tool calls -- if you do,
    the submission is ignored and the loop continues.
    """
    return "Answer recorded."

# Surface Pydantic's own message as an actionable ToolMessage instead of crashing the turn.
submit_answer.handle_validation_error = lambda e: str(e)


async def dry_run(query: str) -> int:
    """Prices a query without running it."""
    def _run() -> int:
        job = get_bq_client().query(query, job_config=bigquery.QueryJobConfig(dry_run=True))
        return job.total_bytes_processed
    return await asyncio.to_thread(_run)


class GetMeasureDaxArgs(BaseModel):
    measure_names: list[MEASURE_NAMES]


@tool(args_schema=GetMeasureDaxArgs)
async def get_measure_dax(measure_names: list[MEASURE_NAMES]) -> dict[str, str]:
    """The DAX body for one or more measures, by exact name -- includes any
    measure referenced inside that DAX, one hop, not recursive.
    Measure names come from the in context measure registry.
    """
    result = {name: MEASURE_DAX[name] for name in measure_names}
    for dax in list(result.values()):
        # Find DAX for any measure referenced inside requested measure DAX
        for candidate_name, candidate_dax in MEASURE_DAX.items():
            if candidate_name not in result and f"[{candidate_name}]" in dax:
                result[candidate_name] = candidate_dax
    return result

get_measure_dax.handle_validation_error = lambda e: str(e)


_chart_spec_field = GenerateChartArgs.model_fields["spec"]

GenerateChartToolCallArgs = create_model(
    "GenerateChartToolCallArgs",
    source_ref=(str, Field(
        ..., description="The reference id printed alongside the run_bigquery_sql or "
                          "run_dax_query result you want to chart -- e.g. 'ref_1'. Copy it "
                          "exactly as shown in that tool's result; never invent one.")),
    spec=(_chart_spec_field.annotation, _chart_spec_field),
)


@tool("generate_chart", args_schema=GenerateChartToolCallArgs)
async def chart_tool_call_standin(**kwargs) -> None:
    """Renders a chart from an earlier tool call's result and returns its
    image URL. source_ref must be the reference id printed alongside the
    run_bigquery_sql or run_dax_query result you want to chart (e.g.
    'ref_1') -- copy it exactly as shown; never invent one. See the spec
    type's own description for its exact shape and grain.
    """
    raise NotImplementedError(
        "generate_chart's real MCP tool object handles dispatch -- this "
        "stand-in exists only so bind_tools() advertises a different schema.")
