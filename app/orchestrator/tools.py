"""Non-MCP tools: run_bigquery_sql, submit_answer, get_measure_dax, get_page_info, search_docs (.claude/rules/tools.md)."""
import asyncio
import concurrent.futures
from datetime import date, time
from decimal import Decimal
from functools import lru_cache
from typing import Literal

import google.auth
import google.auth.impersonated_credentials
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from langchain_core.tools import tool, ToolException
import mistune
from pydantic import BaseModel, Field, create_model, field_validator

from app.config import (
    BIGQUERY_ROW_CAP,
    BIGQUERY_TIMEOUT_SECONDS,
    EMBEDDING_MODEL,
    GCP_PROJECT_ID,
    MAX_ANSWER_TABLE_ROWS,
    MAX_BYTES_BILLED,
    MAX_CLAIM_PRECISION,
    PAGE_INFO_DIR,
    SEARCH_DOCS_MAX_TOP_K,
    SEARCH_DOCS_TOP_K_DEFAULT,
    VECTOR_DB_DATASET,
    VECTOR_DB_TABLE,
    VECTOR_SEARCH_SA_EMAIL,
)
from app.mcp_server.chart_tool import GenerateChartArgs
from app.model_schema import MEASURE_DAX, MEASURE_NAMES


# --- ToolError (shared -- raised here, also used by orchestrator.py's chart dispatch) ---

class ToolError(ToolException):
    """ToolException subclass -- handle_tool_error=True converts it to a ToolMessage."""


# --- run_bigquery_sql -----------------------------------------------------

@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


class BigQuerySqlArgs(BaseModel):
    query: str = Field(description="A SQL query to run against agent_safe -- the only dataset "
                                    "this can reach. Filter or aggregate rather than relying on "
                                    "LIMIT, which doesn't reduce bytes scanned.")


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


async def dry_run(query: str) -> int:
    """Prices a query without running it."""
    def _run() -> int:
        try:
            job = get_bq_client().query(query, job_config=bigquery.QueryJobConfig(dry_run=True))
            return job.total_bytes_processed
        except GoogleAPICallError as e:
            raise ToolError(f"BigQuery error: {e}")
    return await asyncio.to_thread(_run)


# --- submit_answer ---------------------------------------------------------

_markdown_ast = mistune.create_markdown(renderer="ast", plugins=["table"])


def _looks_like_table_separator(line: str) -> bool:
    trimmed_line = line.strip()
    # True only if the line has a pipe and is otherwise just dashes/colons/whitespace
    return "|" in trimmed_line and len(trimmed_line.strip("|-: \t")) == 0


def _check_ragged_table(markdown: str) -> bool:
    """True if the text looks like it has a table but mistune didn't parse one out."""
    has_separator_line = any(_looks_like_table_separator(l) for l in markdown.split("\n"))
    has_real_table = any(block.get("type") == "table" for block in _markdown_ast(markdown))
    return has_separator_line and not has_real_table


def _check_unclosed_fence(markdown: str) -> bool:
    """True if a ``` code fence was opened but never closed."""
    fence_count = sum(1 for line in markdown.split("\n") if line.strip().startswith("```"))
    return bool(fence_count % 2)


class SubmitAnswerArgs(BaseModel):
    answer_markdown: str = Field(
        min_length=1,
        description=f"Plain Markdown. Keep any single table to at most {MAX_ANSWER_TABLE_ROWS} rows -- "
                    "show the top results and summarize the rest, aggregate to fewer rows in a new query, "
                    "or use generate_chart instead. Escape a literal | inside a table cell as \\| or "
                    "it's read as an extra column and breaks the table. Never state a hand-rolled "
                    "approximation, range, or derived value (a rank, percent change, difference, ratio, "
                    "average) anywhere, in prose or a table -- add it to the query and re-run, or "
                    "describe the pattern in words with no number. Only run_bigquery_sql/run_dax_query "
                    "numbers belong in a table at all. A number from any other tool may be stated in "
                    "prose, except search_docs -- never state a search_docs number anywhere, in prose "
                    "or a table. Table cell values are checked automatically against this turn's "
                    "run_bigquery_sql/run_dax_query results, at up to "
                    f"{MAX_CLAIM_PRECISION} decimal places. Format numbers for readability -- "
                    "thousands separators (977,542) and 1-2 decimal places by default, since "
                    "verification matches each number at its own precision and rounding never "
                    "causes a mismatch. Use more decimals only when the figure itself needs it, "
                    "e.g. a Recall@5 score (0.6688, not 0.67). Include the chart_url from "
                    "generate_chart here as an image -- the user sees no chart otherwise.")
    all_prose_numeric_claims: list[float] = Field(
        description="Every number stated in prose as fact from a run_bigquery_sql or run_dax_query "
                    "result this turn -- the only two tools checked against. Never include a number "
                    "already in a markdown table -- checked separately. Never include a number from "
                    "any other tool -- it will fail verification.")
    suggested_follow_ups: list[str] = Field(
        default=[],
        description="1-3 short, natural follow-up questions -- include these by default, since they help the user continue the conversation. Leave empty only when nothing natural genuinely fits.")

    @field_validator("answer_markdown")
    def _unescape_newlines(cls, value: str) -> str:
        """Replaces a literal backslash-n with a real newline."""
        return value.replace("\\n", "\n")

    @field_validator("answer_markdown")
    def _strip_leaked_tool_call_tail(cls, value: str) -> str:
        """Drops a leaked </answer_markdown> tag and everything after it."""
        return value.split("</answer_markdown>")[0].rstrip()

    @field_validator("answer_markdown")
    def _check_markdown_structure(cls, value: str) -> str:
        """Rejects a ragged table or an unclosed code fence instead of
        letting either reach the user broken."""
        ragged_table = _check_ragged_table(value)
        unclosed_fence = _check_unclosed_fence(value)
        if ragged_table and unclosed_fence:
            raise ValueError(
                "A markdown table looks malformed, and a ``` code fence was opened but never "
                "closed. Every table row needs the same number of '|'-separated cells as the "
                "header, and every fence needs a matching close.")
        elif ragged_table:
            raise ValueError(
                "A markdown table looks malformed -- every row needs the same number of "
                "'|'-separated cells as the header.")
        elif unclosed_fence:
            raise ValueError("A ``` code fence was opened but never closed.")
        return value


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

    Don't omit a number from all_prose_numeric_claims to dodge verification
    -- it still needs a real run_bigquery_sql or run_dax_query source.
    Verification exists to catch hallucinated or hand-computed numbers, not
    to be routed around.
    """
    return "Answer recorded."

# Surface Pydantic's own message as an actionable ToolMessage instead of crashing the turn.
submit_answer.handle_validation_error = lambda e: str(e)


# --- get_measure_dax --------------------------------------------------------

class GetMeasureDaxArgs(BaseModel):
    measure_names: list[MEASURE_NAMES] = Field(
        description="One or more exact measure names, from the measure registry already in context.")


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


# --- get_page_info -----------------------------------------------------------

PAGES = tuple(sorted(p.stem for p in PAGE_INFO_DIR.glob("*.txt")))
if not PAGES:
    raise RuntimeError(f"No page-info files found in {PAGE_INFO_DIR}.")


class GetPageInfoArgs(BaseModel):
    page_name: Literal[PAGES] = Field(description="Which dashboard page to return the content for.")


@tool(args_schema=GetPageInfoArgs)
async def get_page_info(page_name: Literal[PAGES]) -> dict[str, str]:
    """Whole-page content for one of this dashboard's two pages. Use the
    page named in "Current dashboard page" for what the user is currently
    viewing, or the other one if the question is clearly about it instead.
    """
    content = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
    return {"page_name": page_name, "content": content}

get_page_info.handle_validation_error = lambda e: str(e)


# --- search_docs --------------------------------------------------------------

SEARCH_DOCS_SQL = f"""
SELECT base.chunk_text, base.file_path, base.section, distance
FROM VECTOR_SEARCH(
  TABLE `{GCP_PROJECT_ID}.{VECTOR_DB_DATASET}.{VECTOR_DB_TABLE}`, 'embedding',
  (SELECT ml_generate_embedding_result AS embedding
   FROM ML.GENERATE_EMBEDDING(
     MODEL `{GCP_PROJECT_ID}.{EMBEDDING_MODEL}`,
     (SELECT @query AS content),
     STRUCT(TRUE AS flatten_json_output))),
  top_k => @top_k, distance_type => 'COSINE')
ORDER BY distance
"""


@lru_cache
def get_vector_search_client() -> bigquery.Client:
    """BigQuery client impersonating vector-search-sa -- the only identity
    with read access to vector_db."""
    source_credentials, _ = google.auth.default()
    impersonated = google.auth.impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=VECTOR_SEARCH_SA_EMAIL,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=300,
    )
    return bigquery.Client(project=GCP_PROJECT_ID, credentials=impersonated)


class SearchDocsArgs(BaseModel):
    query: str = Field(description="A natural-language description of what you're looking for in "
                                    "this project's own methodology documentation.")
    top_k: int = Field(default=SEARCH_DOCS_TOP_K_DEFAULT,
                        description="How many matching chunks to retrieve. Clamped to "
                                    f"1..{SEARCH_DOCS_MAX_TOP_K}, never rejected.")


@tool(args_schema=SearchDocsArgs)
async def search_docs(query: str, top_k: int = SEARCH_DOCS_TOP_K_DEFAULT) -> list[dict]:
    """Semantic search over this project's own methodology docs -- README
    content on approach, architecture, evaluation. Authoritative for project
    intent and methodology, never for numbers. Never state a number returned
    here in your answer, even in passing -- doc chunks aren't authoritative
    for numbers; use run_bigquery_sql or run_dax_query for any numeric
    answer. Never include code snippets from this tool in your answer --
    use get_repo_contents instead. top_k is clamped to 1..SEARCH_DOCS_MAX_TOP_K,
    never rejected.
    """
    top_k = max(1, min(top_k, SEARCH_DOCS_MAX_TOP_K))

    def _run() -> list[dict]:
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("query", "STRING", query),
            bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
        ])
        rows = get_vector_search_client().query(SEARCH_DOCS_SQL, job_config=job_config).result()
        return [dict(row) for row in rows]
    return await asyncio.to_thread(_run)

search_docs.handle_validation_error = lambda e: str(e)


# --- generate_chart (model-facing stand-in) -----------------------------------

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
