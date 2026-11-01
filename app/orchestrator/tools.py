"""Non-MCP tools: run_bigquery_sql, submit_answer, get_measure_dax, get_page_info, search_docs, get_conversation_findings, generate_beeswarm (.claude/rules/tools.md)."""
import asyncio
import base64
import concurrent.futures
import io
import json
import logging
import textwrap
from datetime import date, time
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

import google.auth
import google.auth.impersonated_credentials
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from langchain_core.tools import tool, InjectedToolArg, ToolException
from matplotlib.figure import Figure
import mistune
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, create_model, field_validator, model_validator
import shap

from app.config import (
    AGENT_SAFE_DATASET,
    BIGQUERY_ROW_CAP,
    BIGQUERY_TIMEOUT_SECONDS,
    EMBEDDING_MODEL,
    GCP_PROJECT_ID,
    MAX_ANSWER_TABLE_ROWS,
    MAX_BYTES_BILLED,
    MAX_CLAIM_PRECISION,
    MEMORY_RECALL_TOKEN_BUDGET,
    PAGE_INFO_DIR,
    PREDICTION_EXPLANATIONS_TABLE,
    SEARCH_DOCS_MAX_TOP_K,
    SEARCH_DOCS_TOP_K_DEFAULT,
    SHAP_FEATURE_DISPLAY_NAMES,
    VECTOR_DB_DATASET,
    VECTOR_DB_TABLE,
    VECTOR_SEARCH_SA_EMAIL,
)
from app.shared_chart import GenerateChartArgs, render_and_upload
from app.model_schema import MEASURE_DAX, MEASURE_NAMES
from app.orchestrator.conversation_memory.embedding import embed_query
from app.orchestrator.conversation_memory.reader import (
    fetch_overview_rows,
    fetch_summaries,
    fetch_tag_counts,
    fetch_turns,
    fetch_vector_search_hits,
)
from app.shared_helpers import is_cancelled, session_doc

logger = logging.getLogger(__name__)


# --- ToolError (shared -- raised here, also used by orchestrator.py's chart dispatch) ---

class ToolError(ToolException):
    """ToolException subclass -- handle_tool_error=True converts it to a ToolMessage."""


# --- run_bigquery_sql -----------------------------------------------------

# UNPIVOT list for the waterfall example, labeled with the dashboard names.
_SHAP_UNPIVOT_COLUMNS = ",\n    ".join(
    f"shap_{feature} AS '{label}'" for feature, label in SHAP_FEATURE_DISPLAY_NAMES.items())

BIGQUERY_FEW_SHOT_EXAMPLES = f'''EXAMPLES

Q: "What's the reorder rate by department?"
SELECT department, ROUND(AVG(reordered), 4) AS reorder_rate, COUNT(*) AS n
FROM `instacart-ml-model.agent_safe.product_order_analysis`
GROUP BY department
ORDER BY reorder_rate DESC
Note: reordered is a 0/1 flag at the order-line grain, so AVG(reordered) grouped by department gives a true per-department reorder rate directly.

Q: "On average, how many distinct products has each user ordered?"
SELECT ROUND(AVG(user_distinct_product_count), 2) AS avg_distinct_products
FROM (
  SELECT DISTINCT user_id, user_distinct_product_count
  FROM `instacart-ml-model.agent_safe.product_order_analysis`
)
Note: user_-prefixed columns (user_distinct_product_count, user_reorder_ratio, user_avg_basket_size, etc.) are precomputed per user and repeat on every order-line row for that user. Averaging one directly over product_order_analysis over-weights users with more order lines -- deduplicate to one row per user first.

Q: "Show me a heatmap of order volume by day of week and hour of day."
SELECT order_dow, order_hour_of_day, COUNT(DISTINCT order_id) AS order_count
FROM `instacart-ml-model.agent_safe.product_order_analysis`
GROUP BY order_dow, order_hour_of_day
Note: product_order_analysis is order-line grain -- COUNT(*) here counts order-lines (inflated by basket size), not orders. Always COUNT(DISTINCT order_id) when a question asks for order counts or order volume.

Q: "Which engineered features correlate most strongly with reorder likelihood?"
SELECT
  CORR(user_reorder_ratio, label_reordered) AS corr_reorder_ratio,
  CORR(user_avg_basket_size, label_reordered) AS corr_avg_basket_size,
  CORR(user_prod_avg_days_between_purchases, label_reordered) AS corr_avg_days_between_purchases
FROM `instacart-ml-model.agent_safe.candidate_reorder_features` TABLESAMPLE SYSTEM (1 PERCENT)
Note: correlation is a statistical estimate -- TABLESAMPLE cuts cost on a multi-million-row table with no meaningful accuracy loss. Compute every correlation in ONE query, not one call per column: splitting it into parallel calls still scans the same total bytes, summed against the same cost cap.

Q: "Show a waterfall chart of how each feature contributes, to the LightGBM Classifier's reorder predictions for Banana on the test split."
WITH steps AS (
  SELECT driver, AVG(contribution) AS amount, AVG(base_value) AS base_value
  FROM (
    SELECT * FROM `instacart-ml-model.agent_safe.prediction_explanations`
    WHERE model = 'LightGBM Classifier' AND split = 'test' AND product_name = 'Banana'
  )
  UNPIVOT (contribution FOR driver IN (
    {_SHAP_UNPIVOT_COLUMNS}))
  GROUP BY driver
)
SELECT 'Base Value' AS driver, ANY_VALUE(base_value) AS amount, TRUE AS is_total, 0 AS sort_order FROM steps
UNION ALL SELECT driver, amount, FALSE, ROW_NUMBER() OVER (ORDER BY ABS(amount) DESC) FROM steps
UNION ALL SELECT 'Final Prediction (Avg Log-Odds)', ANY_VALUE(base_value) + SUM(amount), TRUE, COUNT(*) + 1 FROM steps
ORDER BY sort_order
Note: a waterfall comes from ONE query in long form -- a base row and a final row with is_total = TRUE, one row per feature, and sort_order fixing the step order (rows are drawn in the order given; here largest absolute contribution first). Compute the final row in SQL as base_value plus every averaged contribution. A classifier averages log-odds, so the final bar is the average log-odds, not a probability: converting it with the sigmoid does not give the slice's average probability, because the sigmoid is non-linear. Get that from its own query, AVG(prediction), and report both without converting one into the other. A ranker's contributions and final bar are raw scores -- there is no probability to reconcile.'''


@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


class BigQuerySqlArgs(BaseModel):
    query: str = Field(description="A SQL query to run against agent_safe. Filter or aggregate "
                                    "rather than relying on LIMIT, which doesn't reduce bytes scanned.")


def _wait_for_query_result(job: bigquery.QueryJob) -> tuple[list[dict], dict]:
    """Blocks on the query job's completion, enforcing the timeout and row
    cap and converting result rows to JSON-safe types. Synchronous --
    callers run this in a thread."""
    try:
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

    #Row Cap Gaurdrail
    if rows.total_rows > BIGQUERY_ROW_CAP:
        raise ToolError(
            f"Query returned {BIGQUERY_ROW_CAP}+ rows. Add a filter or "
            "aggregate to narrow it.")

    #Future insurance gaurd if date, time, or Decimal types are returned in the rows
    def _convert(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (date, time)):
            return v.isoformat()
        return v

    result_rows = [
        {k: _convert(v) for k, v in dict(row).items()}
        for row in rows
    ]
    return result_rows, {"bytes_billed": job.total_bytes_billed}


async def watch_for_cancel(conversation_id: str) -> None:
    """Polls is_cancelled once a second until a user-requested cancel lands."""
    while not await is_cancelled(conversation_id):
        await asyncio.sleep(1)


def _cancel_job_quietly(job_id: str) -> None:
    """Cancels a BigQuery job, logging instead of raising on failure."""
    try:
        get_bq_client().cancel_job(job_id)
    except Exception as e:
        logger.warning("cancel_job failed for %s: %s", job_id, e)


@tool(args_schema=BigQuerySqlArgs, response_format="content_and_artifact")
async def run_bigquery_sql(query: str, conversation_id: Annotated[str, InjectedToolArg]) -> tuple[list[dict], dict]:
    """Runs a SQL query against agent_safe (BigQuery) -- upstream/warehouse data:
    raw order and product features, pre-model. Exception: prediction_explanations
    holds row-level model predictions with their SHAP explanations. All other
    post-model questions use run_dax_query.

    Guardrails: results cap at 1,000 rows -- filter or aggregate rather than
    relying on LIMIT, which reduces rows returned but not bytes scanned;
    queries scanning over 1 GiB fail; 40s timeout.
    """
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)
    # client.query() just submits the job -- a network call, but not the slow part.
    job = await asyncio.to_thread(get_bq_client().query, query, job_config=job_config)

    #Schedule Query and Cancel tasks concurrentl
    query_task = asyncio.create_task(asyncio.to_thread(_wait_for_query_result, job))
    cancel_task = asyncio.create_task(watch_for_cancel(conversation_id))

    #Race the two tasks and except gateway timeout to cancel both tasks and the job
    try:
        first_completed, _ = await asyncio.wait({query_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # The gateway's own turn timeout, not the watcher -- clean up the same way either way.
        query_task.cancel()
        cancel_task.cancel()
        _cancel_job_quietly(job.job_id)
        raise

    #If cancel_task won the race, the turn is cancelled by the user -- cancel the query & raise ToolError
    if cancel_task in first_completed:
        query_task.cancel()
        _cancel_job_quietly(job.job_id)
        raise ToolError("Cancelled by user request.")

    #If query_task won the race, cancel the watcher(cancel_task) and return the result
    cancel_task.cancel()
    return await query_task

#Ensures that ToolError exceptions raised in run_bigquery_sql() are converted to a ToolMessage.
run_bigquery_sql.handle_tool_error = True
run_bigquery_sql.description += "\n\n" + BIGQUERY_FEW_SHOT_EXAMPLES

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
        default="",
        description=f"Plain Markdown. Empty only when clarifying_question is set. Keep any single table to at most {MAX_ANSWER_TABLE_ROWS} rows -- "
                    "show the top results and summarize the rest, aggregate to fewer rows in a new query, "
                    "or use generate_chart instead. Escape a literal | inside a table cell as \\| or "
                    "it's read as an extra column and breaks the table. Never state a hand-rolled "
                    "approximation, range, or derived value (a rank, percent change, difference, ratio, "
                    "average) anywhere, in prose or a table -- add it to the query and re-run, or "
                    "describe the pattern in words with no number. Only run_bigquery_sql/run_dax_query "
                    "numbers belong in a table at all. A number from any other tool may be stated in "
                    "prose, except search_docs -- never state a search_docs number anywhere, in prose "
                    "or a table. Table cell values are checked automatically against this turn's "
                    "run_bigquery_sql/run_dax_query results, up to "
                    f"{MAX_CLAIM_PRECISION} decimal places. Format numbers readably -- "
                    "thousands separators (977,542), 1-2 decimals by default (rounding never "
                    "fails verification), more only when the figure needs it (Recall@5: "
                    "0.6688, not 0.67). Include the chart_url from "
                    "generate_chart or generate_beeswarm here as an image -- the user sees no chart otherwise.")
    all_prose_numeric_claims: list[float] = Field(
        description="Every number stated in prose as fact from a run_bigquery_sql or run_dax_query "
                    "result this turn -- the only two tools checked against. Never include a number "
                    "already in a markdown table -- checked separately. Never include a number from "
                    "any other tool -- it will fail verification.")
    suggested_follow_ups: list[str] = Field(
        default=[],
        description="1-3 short, natural follow-up questions -- include these by default, "
                    "since they help the user continue the conversation. Leave empty only when "
                    "nothing natural genuinely fits or clarifying_question is set.")
    clarifying_question: str = Field(
        default="",
        description="Set this INSTEAD of answer_markdown, and leave answer_markdown empty, only when "
                    "the request is genuinely ambiguous and a wrong guess would mislead the user. Ask "
                    "one short question naming the specific choice. Otherwise answer -- do not ask "
                    "about anything a reasonable default covers.")
    title: str = Field(
        default="",
        description="A short label for this turn, 3-8 words, no numbers -- e.g. 'reorder rate by "
                    "department'.")
    topics: list[str] = Field(
        default=[],
        description="Short noun phrases for what this turn is about, no numbers.")
    entities: list[str] = Field(
        default=[],
        description="Specific names this turn is about, written as they appear in the data -- for "
                    "example a department, aisle, product, model or measure, or any other named "
                    "thing. Empty if none.")

    @field_validator("topics", "entities")
    def _clean_tags(cls, value: list[str]) -> list[str]:
        """Strips, drops empties, dedupes in order, and caps the list at 6."""
        cleaned: list[str] = []
        for tag in value:
            tag = tag.strip()
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned[:6]

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

    @model_validator(mode="after")
    def _exactly_one_of_answer_or_question(self) -> "SubmitAnswerArgs":
        """Requires exactly one of answer_markdown or clarifying_question."""
        has_answer = bool(self.answer_markdown.strip())
        has_question = bool(self.clarifying_question.strip())
        if has_answer and has_question:
            raise ValueError("Set only one of answer_markdown or clarifying_question, not both.")
        if not (has_answer or has_question):
            raise ValueError(
                "Set exactly one of answer_markdown (your final answer) or clarifying_question "
                "(one question for the user, only when the request is genuinely ambiguous).")
        return self


@tool(args_schema=SubmitAnswerArgs)
async def submit_answer(answer_markdown: str = "", all_prose_numeric_claims: list[float] | None = None,
                         suggested_follow_ups: list[str] | None = None, clarifying_question: str = "",
                         title: str = "", topics: list[str] | None = None,
                         entities: list[str] | None = None) -> str:
    """Call this with your final answer once you have everything you need or
    with a clarifying question when the request is genuinely ambiguous.
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
_missing_page_images = [p for p in PAGES if not (PAGE_INFO_DIR / f"{p}.png").exists()]
if _missing_page_images:
    raise RuntimeError(f"Missing page-info screenshot(s) for: {_missing_page_images}.")

# The Feature Importance chart's second structural variant -- not part of
# PAGES (that's derived from the .txt files, one per page), so it needs its
# own existence check.
FEATURE_IMPORTANCE_BEESWARM_IMAGE = "Model Performance Beeswarm"
if not (PAGE_INFO_DIR / f"{FEATURE_IMPORTANCE_BEESWARM_IMAGE}.png").exists():
    raise RuntimeError(f"Missing page-info screenshot: {FEATURE_IMPORTANCE_BEESWARM_IMAGE}.png")


class GetPageInfoArgs(BaseModel):
    page_name: Literal[PAGES] = Field(description="Which dashboard page to return the content for.")


def _page_image_base64(image_name: str) -> str:
    return base64.b64encode((PAGE_INFO_DIR / f"{image_name}.png").read_bytes()).decode()


def resolve_image_name(page_name: str, filter_context: list[dict]) -> str:
    """Picks the PNG matching the page's current structural state, where a
    structural (not just data) variant exists."""
    if page_name == "Financial Impact":
        image_name = page_name
    elif page_name == "Model Performance":
        # Uncertain if custom connector strips {Value: ...} wrapper in filter context.
        # unwrap it if present, use the entry as-is otherwise.
        entries = [fc.get("Value", fc) for fc in filter_context]
        selected_plot = next(
            (fc["value"] for fc in entries
             if fc.get("filter_column") == "Feature_Importance_Chart_Types[Chart_Types]"),
            None,
        )
        image_name = FEATURE_IMPORTANCE_BEESWARM_IMAGE if selected_plot == "Beeswarm" else page_name
    return image_name


@tool(args_schema=GetPageInfoArgs)
async def get_page_info(page_name: Literal[PAGES],
                         filter_context: Annotated[list[dict], InjectedToolArg]) -> list[dict]:
    """Whole-page content for one of this dashboard's two pages, plus a
    reference screenshot of its general layout. Use the page named in
    "Current dashboard page" for what the user is currently viewing, or the
    other one if the question is clearly about it instead.

    The screenshot is a static reference for layout only -- not a live
    capture, never a source of numbers. Never mention the screenshot, the
    reference layout, or a "default" state to the user -- it's for your own
    understanding of page structure only. State only the current,
    filter_context-grounded reality: where filter_context has the entry
    named below, trust it over the screenshot.

    Model Performance page:
    - "Recall@5 by Model": title and metric follow the "Evaluation Metric
      Parameter[Evaluation Metric Parameter]" entry (Recall@5, NDCG@5,
      Precision@5, F1@5) -- e.g. "NDCG@5 by Model" if NDCG@5 is selected.
    - "Feature Importance: Mean Absolute SHAP": the title stays fixed, but
      the base model shown (XGBoost Ranker, XGBoost Classifier, LightGBM
      Ranker, LightGBM Classifier) follows "base_models_dimension[model_name]",
      and the plot type ("Bar Plot (Mean Absolute SHAP)" or "Beeswarm")
      follows "Feature_Importance_Chart_Types[Chart_Types]" -- the
      screenshot already reflects that one, so describe whichever plot type
      is shown.

    Financial Impact page: the sensitivity sliders (Conversion Rate, Churn
    Rate, Conversion/AOV/Churn Lift per 10% Recall) and the waterfall
    chart's model selection come from filter_context, not the screenshot --
    ground any DAX for this page in those values.
    """
    content = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
    image_name = resolve_image_name(page_name, filter_context)
    return [
        {"type": "text", "text": f"Page: {page_name}\n\n{content}"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_page_image_base64(image_name)}"}},
    ]

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
    use get_repo_contents instead.
    """
    top_k = max(1, min(top_k, SEARCH_DOCS_MAX_TOP_K))

    def _run() -> list[dict]:
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("query", "STRING", query),
            bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
        ])
        rows = get_vector_search_client().query(SEARCH_DOCS_SQL, job_config=job_config).result()
        return [dict(row) for row in rows]
    try:
        return await asyncio.to_thread(_run)
    except GoogleAPICallError as e:
        raise ToolError(f"Could not search the project docs ({e}). Retry once, or answer without it.") from e

search_docs.handle_tool_error = True
search_docs.handle_validation_error = lambda e: str(e)


# --- get_conversation_findings -------------------------------------------------

GET_CONVERSATION_FINDINGS_DESCRIPTION = f"""Recalls this conversation's earlier verified answers from a
ledger that outlasts your message history. Use it to summarize or revisit earlier findings, or to
quote a number from an earlier turn. Numbers sitting in your message history can't be cited in an
answer; only numbers returned by view="turns" can.

view="overview": takes no other arguments. Returns the number of verified turns, their sizes in
tokens, how many of the newest turns fit in one call, a summary of each earlier stretch (turn range,
title, topics, text), and each turn's number, date, sources, title, topics, entities and question.
Use it to choose which turns to recall. Numbers in the overview, summaries included, cannot pass
verification and must not be cited in any answer.

view="turns": returns full rows (question, answer, verified numbers, sources, title, topics,
entities, filters) for turns start_turn..end_turn (1-based, inclusive; give both), or for the newest
last_n turns. With no range it returns every turn. One call returns at most
{MEMORY_RECALL_TOKEN_BUDGET:,} tokens; a request over that fails and returns the overview so you can
narrow it."""

OVERVIEW_QUESTION_MAX_CHARS = 150


class GetConversationFindingsArgs(BaseModel):
    view: Literal["overview", "turns"] = Field(
        description="'overview' for a summary of the conversation's verified turns, 'turns' for their full rows.")
    start_turn: int | None = Field(
        default=None, description="First turn to return (1-based). Give with end_turn, only with view='turns'.")
    end_turn: int | None = Field(
        default=None, description="Last turn to return (inclusive). Give with start_turn, only with view='turns'.")
    last_n: int | None = Field(
        default=None, description="Return the newest N turns. Only with view='turns', never with start_turn/end_turn.")
    include_tool_calls: bool = Field(
        default=False,
        description="Add the name and args of each turn's tool calls (no results). Leave False unless you "
                    "need them. Only with view='turns'.")

    @model_validator(mode="after")
    def _check_argument_combination(self) -> "GetConversationFindingsArgs":
        """Rejects arguments that don't belong with the chosen view."""
        has_range = self.start_turn is not None or self.end_turn is not None
        if self.view == "overview":
            if has_range or self.last_n is not None or self.include_tool_calls:
                raise ValueError("view='overview' takes no other arguments. Use view='turns' to select turns.")

        if self.view == "turns":
            if self.last_n is not None:
                if has_range:
                    raise ValueError("Use last_n on its own, or start_turn and end_turn -- not both.")
                if self.last_n < 1:
                    raise ValueError("last_n must be at least 1.")
            elif has_range:
                if self.start_turn is None or self.end_turn is None:
                    raise ValueError("Give both start_turn and end_turn, or neither.")
                if not 1 <= self.start_turn <= self.end_turn:
                    raise ValueError("Turn numbers are 1-based, and start_turn must not be after end_turn.")
        return self


def build_conversation_overview(rows: list[dict], summaries: list[dict], token_budget: int) -> dict:
    """Summary of a conversation's verified turns: totals, how many of the newest
    turns fit token_budget, the compaction summaries, and one entry per turn."""
    recent_turns_that_fit = 0
    tokens = 0
    for row in reversed(rows):
        tokens += row["approx_tokens"]
        if tokens > token_budget:
            break
        recent_turns_that_fit += 1
    return {
        "total_turns": len(rows),
        "total_approx_tokens": sum(row["approx_tokens"] for row in rows),
        "token_budget": token_budget,
        "max_recent_turns_within_budget": recent_turns_that_fit,
        "first_answered_at": rows[0]["answered_at"] if rows else None,
        "last_answered_at": rows[-1]["answered_at"] if rows else None,
        "summaries": summaries,
        "turns": [
            {
                "turn": row["turn"],
                "answered_at": row["answered_at"],
                "approx_tokens": row["approx_tokens"],
                "sources": row["sources"],
                "title": row["title"],
                "topics": row["topics"],
                "entities": row["entities"],
                "question": (row["question"] if len(row["question"]) <= OVERVIEW_QUESTION_MAX_CHARS
                             else row["question"][:OVERVIEW_QUESTION_MAX_CHARS - 3] + "..."),
            }
            for row in rows
        ],
    }


def resolve_turn_range(
    total_turns: int, start_turn: int | None, end_turn: int | None, last_n: int | None
) -> tuple[int, int]:
    """Inclusive (start, end) turn numbers, from arguments the args model has already validated:
    last_n, both start_turn and end_turn, or none of them for the whole conversation."""
    if total_turns == 0:
        raise ToolError("This conversation has no verified turns yet.")
    if last_n is not None:
        return max(1, total_turns - last_n + 1), total_turns
    if start_turn is None:
        return 1, total_turns
    if end_turn > total_turns:
        raise ToolError(
            f"This conversation has {total_turns} verified turns; start_turn and end_turn must be within 1-{total_turns}.")
    return start_turn, end_turn


def _over_budget_error(overview: dict, start: int, end: int, tokens: int) -> ToolError:
    """Over-budget request, with the overview to narrow it from."""
    return ToolError(
        f"Turns {start}-{end} are about {tokens:,} tokens, over the {overview['token_budget']:,}-token "
        f"limit for one call. Narrow the request using this overview:\n{json.dumps(overview)}")


@tool(args_schema=GetConversationFindingsArgs, description=GET_CONVERSATION_FINDINGS_DESCRIPTION)
async def get_conversation_findings(
    view: Literal["overview", "turns"],
    conversation_id: Annotated[str, InjectedToolArg],
    start_turn: int | None = None,
    end_turn: int | None = None,
    last_n: int | None = None,
    include_tool_calls: bool = False,
) -> dict | list[dict]:
    try:
        overview_rows, summaries = await asyncio.gather(
            fetch_overview_rows(conversation_id), fetch_summaries(conversation_id))
        overview = build_conversation_overview(overview_rows, summaries, MEMORY_RECALL_TOKEN_BUDGET)
        if view == "overview":
            return overview

        start, end = resolve_turn_range(overview["total_turns"], start_turn, end_turn, last_n)
        approx_tokens = sum(row["approx_tokens"] for row in overview_rows[start - 1:end])
        if approx_tokens > MEMORY_RECALL_TOKEN_BUDGET:
            raise _over_budget_error(overview, start, end, approx_tokens)

        turns = await fetch_turns(conversation_id, start, end, include_tool_calls)
        # approx_tokens doesn't count tool calls, so check the real size once they're in.
        if include_tool_calls:
            approx_tokens = len(json.dumps(turns)) // 4
            if approx_tokens > MEMORY_RECALL_TOKEN_BUDGET:
                raise _over_budget_error(overview, start, end, approx_tokens)
        return turns
    except GoogleAPICallError as e:
        raise ToolError(f"Could not read the conversation ledger ({e}). Retry once, or answer without it.") from e

get_conversation_findings.handle_tool_error = True
get_conversation_findings.handle_validation_error = lambda e: str(e)


# --- search_conversation_findings -------------------------------------------------

SEARCH_TOP_K_DEFAULT = 5
SEARCH_TOP_K_MAX = 10
TAG_LIST_LIMIT = 300   # most used topics and entities returned per kind

SEARCH_CONVERSATION_FINDINGS_DESCRIPTION = """Semantic (vector) search over earlier verified answers, for when you \
don't know which turn covered something. It matches on meaning, not exact wording, so describe what you're \
after. To read turns by position, use get_conversation_findings. Only numbers returned here can be cited in an \
answer, not numbers in your message history.

mode="search" returns the closest turns with their question, answer, verified numbers, date and a distance \
(smaller is closer). Hits from other conversations can be old: give their date, since the data may have \
changed and chart links may have expired.

mode="tags" lists the topics and entities in use, with counts.

Filters hide turns whose tags are worded differently, so add one only when unfiltered hits miss a specific \
product, department or model, etc., and take the wording from mode="tags"."""


class SearchConversationFindingsArgs(BaseModel):
    mode: Literal["search", "tags"] = Field(
        description="'search' runs a semantic (vector) search for the query. "
                    "'tags' lists the topics and entities available for filtering.")
    scope: Literal["conversation", "user"] = Field(
        default="conversation",
        description="'conversation' searches only the current conversation. "
                    "'user' searches all of this user's conversations, past ones included.")
    query: str = Field(
        default="", description="What to look for, in plain language. Required with mode='search'.")
    top_k: int = Field(
        default=SEARCH_TOP_K_DEFAULT,
        description=f"How many turns to return, up to {SEARCH_TOP_K_MAX}. Only with mode='search'.")
    topics: list[str] = Field(
        default=[], description="Keep only turns with a matching topic (any of these). Only with mode='search'.")
    entities: list[str] = Field(
        default=[], description="Keep only turns with a matching entity (any of these). Only with mode='search'.")
    include_tool_calls: bool = Field(
        default=False,
        description="Add each hit's tool call names and args (no results). Leave False unless you need "
                    "them. Only with mode='search'.")

    @model_validator(mode="after")
    def _check_argument_combination(self) -> "SearchConversationFindingsArgs":
        """Rejects arguments that don't belong with the chosen mode."""
        if self.mode == "tags":
            if self.query or self.topics or self.entities or self.include_tool_calls:
                raise ValueError("mode='tags' takes only scope. Use mode='search' to search.")
        else:
            if not self.query.strip():
                raise ValueError("mode='search' needs a query.")
            if not 1 <= self.top_k <= SEARCH_TOP_K_MAX:
                raise ValueError(f"top_k must be between 1 and {SEARCH_TOP_K_MAX}.")
        return self


@tool(args_schema=SearchConversationFindingsArgs, description=SEARCH_CONVERSATION_FINDINGS_DESCRIPTION)
async def search_conversation_findings(
    mode: Literal["search", "tags"],
    conversation_id: Annotated[str, InjectedToolArg],
    user_id: Annotated[str, InjectedToolArg],
    scope: Literal["conversation", "user"] = "conversation",
    query: str = "",
    top_k: int = SEARCH_TOP_K_DEFAULT,
    topics: list[str] = [],
    entities: list[str] = [],
    include_tool_calls: bool = False,
) -> dict | list[dict]:
    searched_conversation_id = conversation_id if scope == "conversation" else None
    try:
        if mode == "tags":
            return await fetch_tag_counts(user_id, searched_conversation_id, TAG_LIST_LIMIT)

        try:
            query_vector = await embed_query(query)
        except Exception as e:
            raise ToolError(f"Could not embed the search query ({e}). Retry, or use get_conversation_findings "
                            "to read turns by position.") from e
        return await fetch_vector_search_hits(
            user_id, searched_conversation_id, query_vector, top_k,
            topics, entities, include_tool_calls)
    except GoogleAPICallError as e:
        raise ToolError(f"Could not read the conversation ledger ({e}). Retry once, or answer without it.") from e

search_conversation_findings.handle_tool_error = True
search_conversation_findings.handle_validation_error = lambda e: str(e)


# --- manage_tasks -----------------------------------------------------------------

TASKS_MAX_COUNT = 30
TASK_MAX_CHARS = 500    # 30 tasks at 500 characters is at most about 3,750 tokens injected per turn
TASK_SCOPE_MAX_CHARS = 800

MANAGE_TASKS_DESCRIPTION = """Saves the plan for a request that is too large to finish in one turn, or that \
the user asks you to plan, so it survives across turns. If you expect a request to need more than about \
eight queries, charts and lookups in total, don't try to answer it all: save a plan, show it to the user \
as a numbered list (not a table), and work through it with them. If you find partway through a turn that \
the work is bigger than you expected, stop, save the plan, and give the user what you have so far. Each \
call replaces the whole task list, so send every task with its current status, and update statuses in the \
same turn as the work, before you submit your answer. The saved plan is shown to you at the start of each \
turn. When everything is done, or the user drops the plan, clear the whole plan, scope included, by \
sending an empty task list.

Don't record findings in tasks. A task list is context, not a verified source: any number in an answer \
must come from this turn's queries or from get_conversation_findings, so a figure copied from a task \
would fail verification.

Sizing tasks is what matters most. Each task is one deliverable the user would recognize (for example a \
chart with its takeaway), completed in a single turn, which has about two minutes and a few tool calls. \
A task includes everything it needs: the queries, the charts built from them, and a short explanation. \
Never split a pipeline across tasks (one task to pull data, another to chart it): results from earlier \
turns are not available, so the second task would have to pull the data again. Work one task per turn by \
default; when the next tasks are small and share the same data, do them together in one turn and mark \
each done. A task that needs more than a handful of queries and charts is too big: split it into tasks \
that are each self-contained, for example by question, by segment, or into groups of two or three \
related charts."""


class TaskItem(BaseModel):
    text: str = Field(
        description="What to do in this task, written so a later turn can carry it out from this text and "
                    "the plan's scope alone. Say the question and the output you want, and include getting "
                    "its own data. If it builds on an earlier finding, name the finding instead of assuming "
                    "the data is still there. Leave out anything already in the scope. Name the work, not a "
                    "result.\n"
                    "Good: Reorder rate by department, as a bar chart sorted high to low, with a "
                    "two-sentence takeaway.\n"
                    "Not a task: Pull the department data. (A separate chart task would have to pull it "
                    "again.)")
    status: Literal["todo", "in_progress", "done"] = Field(
        description="'todo', 'in_progress' (at most one task), or 'done' (its deliverable has been "
                    "delivered to the user).")


class ManageTasksArgs(BaseModel):
    scope: str | None = Field(
        default=None,
        description="Context that applies to every task: the model and split, filters, definitions, and "
                    "output conventions. Put it here once so tasks don't repeat it. Send it only when you "
                    "set or change it; leave it out to keep the saved scope, or send an empty string to "
                    "clear it.")
    tasks: list[TaskItem] = Field(
        description="The complete task list, in order. Replaces the saved list; send an empty list to "
                    "clear the whole plan.")

    @field_validator("scope")
    def _check_scope(cls, scope: str | None) -> str | None:
        """Strips the scope and rejects one over the cap."""
        if scope is None:
            return None
        scope = scope.strip()
        if len(scope) > TASK_SCOPE_MAX_CHARS:
            raise ValueError(f"The scope is {len(scope)} characters; keep it under {TASK_SCOPE_MAX_CHARS}.")
        return scope

    @field_validator("tasks")
    def _check_tasks(cls, tasks: list[TaskItem]) -> list[TaskItem]:
        """Rejects a list over the caps, a blank or overlong task, or more than one in progress."""
        if len(tasks) > TASKS_MAX_COUNT:
            raise ValueError(f"A task list holds at most {TASKS_MAX_COUNT} tasks; merge or drop some.")
        for number, task in enumerate(tasks, start=1):
            task.text = task.text.strip()
            if not task.text:
                raise ValueError(f"Task {number} is blank.")
            if len(task.text) > TASK_MAX_CHARS:
                raise ValueError(
                    f"Task {number} is {len(task.text)} characters; keep each under {TASK_MAX_CHARS}.")
        if sum(task.status == "in_progress" for task in tasks) > 1:
            raise ValueError("More than one task is in progress; mark only the one you are working on.")
        return tasks


@tool(args_schema=ManageTasksArgs, description=MANAGE_TASKS_DESCRIPTION)
async def manage_tasks(
    tasks: list[TaskItem],
    conversation_id: Annotated[str, InjectedToolArg],
    scope: str | None = None,
) -> str:
    if tasks:
        update = {"task_plan.tasks": [task.model_dump() for task in tasks]}
        if scope is not None:
            update["task_plan.scope"] = scope
    else:
        update = {"task_plan": {}}
    try:
        await session_doc(conversation_id).update(update)
    except GoogleAPICallError as e:
        raise ToolError(f"Could not save the task plan ({e}). Retry once, or tell the user.") from e
    if not tasks:
        return "Cleared the task plan."
    done = sum(task.status == "done" for task in tasks)
    return f"Saved the task plan ({done} of {len(tasks)} done)."

manage_tasks.handle_tool_error = True
manage_tasks.handle_validation_error = lambda e: str(e)


# --- generate_beeswarm -----------------------------------------------------------

BEESWARM_SAMPLE_ROWS = 30_000
BEESWARM_MIN_ROWS = 300   # smallest slice worth plotting
BEESWARM_FIGSIZE = (9.0, 7.5)
NULL_FILL_VALUE = 999   # what the ML pipeline COALESCEs an undefined feature to
NULL_FILLED_FEATURES = (
    "label_user_product_days_since_last_purchased",
    "user_prod_avg_days_between_purchases",
    "user_product_nearing_reorder_ratio",
)

BEESWARM_FEATURES = list(SHAP_FEATURE_DISPLAY_NAMES)
_BEESWARM_TABLE = f"`{GCP_PROJECT_ID}.{AGENT_SAFE_DATASET}.{PREDICTION_EXPLANATIONS_TABLE}`"

# raw_<feature>, shap_<feature> for each feature
_BEESWARM_COLUMNS = [column for feature in BEESWARM_FEATURES for column in (f"raw_{feature}", f"shap_{feature}")]

# Packs a row into one CSV string, 5 significant digits; NULL becomes empty.
_BEESWARM_PACKED_ROW = ", ".join(
    f"IFNULL(FORMAT('%.5g', CAST({column} AS FLOAT64)), '')" for column in _BEESWARM_COLUMNS)

# Hash-ordered sample. An empty filter list arrives as NULL, so COALESCE(ARRAY_LENGTH(..), 0) = 0 skips it.
_BEESWARM_SAMPLE_SQL = f"""
SELECT ARRAY_TO_STRING([{_BEESWARM_PACKED_ROW}], ',') AS row_csv
FROM {_BEESWARM_TABLE}
WHERE split = @split
  AND model = @model
  AND (COALESCE(ARRAY_LENGTH(@departments), 0) = 0 OR LOWER(department) IN UNNEST(@departments))
  AND (COALESCE(ARRAY_LENGTH(@aisles), 0) = 0 OR LOWER(aisle) IN UNNEST(@aisles))
  AND (COALESCE(ARRAY_LENGTH(@product_names), 0) = 0 OR LOWER(product_name) IN UNNEST(@product_names))
ORDER BY FARM_FINGERPRINT(CONCAT(
  CAST(user_id AS STRING), '-', CAST(anchor_order_number AS STRING), '-',
  CAST(candidate_product_id AS STRING)))
LIMIT @n
"""

# Mean |SHAP| per feature, row count, and which requested names exist (empty when that filter is off).
_BEESWARM_IMPORTANCE_SQL = f"""
SELECT
  {", ".join(f"AVG(ABS(shap_{feature})) AS {feature}" for feature in BEESWARM_FEATURES)},
  COUNT(*) AS n_rows,
  ARRAY_AGG(DISTINCT IF(COALESCE(ARRAY_LENGTH(@departments), 0) = 0, NULL, LOWER(department)) IGNORE NULLS) AS matched_departments,
  ARRAY_AGG(DISTINCT IF(COALESCE(ARRAY_LENGTH(@aisles), 0) = 0, NULL, LOWER(aisle)) IGNORE NULLS) AS matched_aisles,
  ARRAY_AGG(DISTINCT IF(COALESCE(ARRAY_LENGTH(@product_names), 0) = 0, NULL, LOWER(product_name)) IGNORE NULLS) AS matched_product_names
FROM {_BEESWARM_TABLE}
WHERE split = @split
  AND model = @model
  AND (COALESCE(ARRAY_LENGTH(@departments), 0) = 0 OR LOWER(department) IN UNNEST(@departments))
  AND (COALESCE(ARRAY_LENGTH(@aisles), 0) = 0 OR LOWER(aisle) IN UNNEST(@aisles))
  AND (COALESCE(ARRAY_LENGTH(@product_names), 0) = 0 OR LOWER(product_name) IN UNNEST(@product_names))
"""


class GenerateBeeswarmArgs(BaseModel):
    split: Literal["validation", "test"] = Field(description="Dataset split to plot.")
    model: Literal["LightGBM Classifier", "XGBoost Classifier",
                   "LightGBM Ranker", "XGBoost Ranker"] = Field(
        description="Which model's SHAP values to plot.")
    title: str = Field(description="Chart title. Name the slice if one is selected.")
    departments: list[str] | None = Field(default=None, description="Only plot these departments.")
    aisles: list[str] | None = Field(default=None, description="Only plot these aisles.")
    product_names: list[str] | None = Field(default=None, description="Only plot these products.")


def _lowercased(names: list[str] | None) -> list[str]:
    return sorted({name.lower() for name in names or []})


def _fetch_beeswarm_data(split: str, model: str, departments: list[str] | None,
                         aisles: list[str] | None, product_names: list[str] | None) -> tuple[pd.DataFrame, np.ndarray]:
    """(sampled rows, mean |SHAP| per feature in BEESWARM_FEATURES order), both for the requested slice."""
    departments, aisles, product_names = _lowercased(departments), _lowercased(aisles), _lowercased(product_names)
    query_parameters = [
        bigquery.ScalarQueryParameter("split", "STRING", split),
        bigquery.ScalarQueryParameter("model", "STRING", model),
        bigquery.ArrayQueryParameter("departments", "STRING", departments),
        bigquery.ArrayQueryParameter("aisles", "STRING", aisles),
        bigquery.ArrayQueryParameter("product_names", "STRING", product_names),
        bigquery.ScalarQueryParameter("n", "INT64", BEESWARM_SAMPLE_ROWS),
    ]

    def submit(sql: str):
        return get_bq_client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=query_parameters))

    try:
        # Submit both queries first, so they run at the same time.
        sample_job = submit(_BEESWARM_SAMPLE_SQL)
        importance_job = submit(_BEESWARM_IMPORTANCE_SQL)

        # The importance query aggregates the whole slice, so it returns exactly one row.
        [importance_row] = importance_job.result()

        # Reject a bad request before downloading the sample.
        requested_and_matched = [
            ("departments", departments, importance_row["matched_departments"]),
            ("aisles", aisles, importance_row["matched_aisles"]),
            ("product_names", product_names, importance_row["matched_product_names"]),
        ]
        for filter_name, requested, matched in requested_and_matched:
            missing = sorted(set(requested) - set(matched or []))
            if missing:
                raise ToolError(
                    f"No {filter_name.replace('_', ' ')} match {missing} for {model} on the {split} split. Names "
                    "must match values in prediction_explanations (case doesn't matter) -- look them up with run_bigquery_sql.")
        if importance_row["n_rows"] < BEESWARM_MIN_ROWS:
            raise ToolError(
                f"Only {importance_row['n_rows']} predictions match this model, split, and filters; a beeswarm "
                f"needs at least {BEESWARM_MIN_ROWS}. Try a broader slice (an aisle's department, or no filter).")

        sample_rows = sample_job.result()
        df = pd.read_csv(io.StringIO("\n".join(row[0] for row in sample_rows)), header=None, names=_BEESWARM_COLUMNS)
    except GoogleAPICallError as e:
        raise ToolError(f"BigQuery error: {e}")
    return df, np.array([importance_row[feature] for feature in BEESWARM_FEATURES])


def _render_beeswarm(df: pd.DataFrame, importance: np.ndarray, model: str, title: str) -> Figure:
    raw_values = df[[f"raw_{feature}" for feature in BEESWARM_FEATURES]].astype(float)
    null_filled_columns = [f"raw_{feature}" for feature in NULL_FILLED_FEATURES]
    raw_values[null_filled_columns] = raw_values[null_filled_columns].replace(NULL_FILL_VALUE, np.nan)
    explanation = shap.Explanation(
        values=df[[f"shap_{feature}" for feature in BEESWARM_FEATURES]].to_numpy(dtype=float),
        data=raw_values.to_numpy(),
        feature_names=list(SHAP_FEATURE_DISPLAY_NAMES.values()))

    fig = Figure(figsize=BEESWARM_FIGSIZE)
    ax = fig.subplots()
    shap.plots.beeswarm(explanation, max_display=len(BEESWARM_FEATURES),
                        order=np.argsort(-importance), ax=ax, plot_size=None, show=False)
    
    ax.set_title(textwrap.fill(title, 60), fontsize=15, fontweight="bold", pad=34)

    ax.text(0.5, 1.03, f"n = {len(df):,} predictions · gray = no value for this feature", transform=ax.transAxes,
            ha="center", va="bottom", fontsize=12, color="#555555")
    
    score_unit = "log-odds" if model.endswith("Classifier") else "raw score"
    ax.set_xlabel(f"SHAP value ({score_unit})", fontsize=13)

        
    fig.tight_layout()
    
    return fig


@tool(args_schema=GenerateBeeswarmArgs)
async def generate_beeswarm(
    split: str, model: str, title: str, departments: list[str] | None = None,
    aisles: list[str] | None = None, product_names: list[str] | None = None, *,
    bucket_name: Annotated[str, InjectedToolArg],
    storage_backend: Annotated[str, InjectedToolArg],
    expiration_hours: Annotated[float, InjectedToolArg],
    access_token: Annotated[str, InjectedToolArg],
    service_account_email: Annotated[str, InjectedToolArg],
) -> list[dict]:
    """Renders a SHAP beeswarm plot for one model and split, optionally limited to listed
    departments, aisles, or products (exact names, any case); features are then ranked
    within that slice. Returns the image URL, plus the image itself so you can describe
    what it shows. Gray points mean the feature had no value for that prediction. The plot
    isn't a number source -- don't state values read from it. To quote how many predictions
    it shows, run SELECT LEAST(COUNT(*), {sample_rows}) over the same split, model, and
    filters in prediction_explanations. Small slices, a single product especially, rest on
    fewer predictions: note that their rankings are noisier."""
    df, importance = await asyncio.to_thread(
        _fetch_beeswarm_data, split, model, departments, aisles, product_names)
    fig = await asyncio.to_thread(_render_beeswarm, df, importance, model, title)
    url, png = await asyncio.to_thread(render_and_upload, fig, bucket_name, storage_backend,
                                       expiration_hours, access_token, service_account_email)
    return [
        {"type": "text", "text": json.dumps({"chart_url": url})},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode()}"}},
    ]

generate_beeswarm.handle_tool_error = True
generate_beeswarm.description = generate_beeswarm.description.format(sample_rows=BEESWARM_SAMPLE_ROWS)
generate_beeswarm.handle_validation_error = lambda e: str(e)


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
    image URL. See the spec type's own description for its exact shape and
    grain. Check whether the spec type offers a sort option and use it whenever a sorted view
    would present the data more clearly -- don't default to unsorted just
    because the query didn't order it.
    """
    raise NotImplementedError(
        "generate_chart's real MCP tool object handles dispatch -- this "
        "stand-in exists only so bind_tools() advertises a different schema.")


# --- combine_results (model-facing stand-in) -----------------------------------

class CombineResultsToolCallArgs(BaseModel):
    source_refs: list[str] = Field(
        ...,
        description="Reference ids of two or more earlier results to combine -- "
                     "e.g. ['ref_1', 'ref_2']. Copy them exactly as shown; never "
                     "invent one. For 'join', the first ref is the left table.")
    method: Literal["stack", "join"] = Field(
        ..., description="'stack' appends rows -- use when every input has the "
                           "same columns but covers a different slice. 'join' "
                           "merges rows on join_key into wider rows -- use when "
                           "inputs have different columns about the same entities.")
    join_key: list[str] | None = Field(
        default=None,
        description="One or more column names to join on -- required when "
                     "method is 'join', must exist in every input. Omit for 'stack'.")
    join_how: Literal["inner", "left"] = Field(
        default="inner",
        description="'inner' (default) keeps only rows that match in every "
                     "input. 'left' keeps every row from the first input, "
                     "filling unmatched numeric columns with 0 and unmatched "
                     "categorical columns with null.")

    @model_validator(mode="after")
    def _at_least_two_refs(self) -> "CombineResultsToolCallArgs":
        if len(self.source_refs) < 2:
            raise ValueError(
                f"combine_results needs at least two source_refs, got "
                f"{len(self.source_refs)}. Reference another earlier result, or "
                "call generate_chart on this one directly.")
        return self


@tool("combine_results", args_schema=CombineResultsToolCallArgs)
async def combine_results_call_standin(**kwargs) -> None:
    """Stacks or joins several earlier tool results into one new reference
    id, usable anywhere a source_ref is accepted (generate_chart, or
    another combine_results call).

    Use it when one query can't answer the question as-is: (a) the result
    would be too large or too costly, so split the columns into narrower
    queries and combine them on a shared key (join) or stack same-shaped
    slices (stack); (b) you need to compare a BigQuery result with a DAX
    result, so run each and join them on a shared key. Don't use it for
    data that didn't come from earlier tool calls.
    """
    raise NotImplementedError(
        "combine_results' real MCP tool object handles dispatch -- this "
        "stand-in exists only so bind_tools() advertises a different schema.")
