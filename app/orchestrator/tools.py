"""Non-MCP tools: run_bigquery_sql, submit_answer, get_measure_dax, get_page_info, search_docs (.claude/rules/tools.md)."""
import asyncio
import base64
import concurrent.futures
import logging
from datetime import date, time
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

import google.auth
import google.auth.impersonated_credentials
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from langchain_core.tools import tool, InjectedToolArg, ToolException
import mistune
from pydantic import BaseModel, Field, create_model, field_validator, model_validator

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
from app.orchestrator.shared_helpers import is_cancelled

logger = logging.getLogger(__name__)


# --- ToolError (shared -- raised here, also used by orchestrator.py's chart dispatch) ---

class ToolError(ToolException):
    """ToolException subclass -- handle_tool_error=True converts it to a ToolMessage."""


# --- run_bigquery_sql -----------------------------------------------------

BIGQUERY_FEW_SHOT_EXAMPLES = '''EXAMPLES

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

Q: "Do users who reorder products more quickly have a higher reorder rate for that product?"
SELECT
  CASE WHEN user_prod_avg_days_between_purchases < 7 THEN 'under 7 days' ELSE '7+ days' END AS pace_bucket,
  ROUND(AVG(label_reordered), 4) AS reorder_rate,
  COUNT(*) AS n
FROM `instacart-ml-model.agent_safe.candidate_reorder_features`
WHERE user_prod_avg_days_between_purchases IS NOT NULL
GROUP BY pace_bucket
Note: candidate_reorder_features is the right table for correlating an engineered feature against reorder likelihood (label_reordered) -- product_order_analysis is order-line grain with no candidate/label structure for this kind of question.

Q: "Which engineered features correlate most strongly with reorder likelihood?"
SELECT
  CORR(user_reorder_ratio, label_reordered) AS corr_reorder_ratio,
  CORR(user_avg_basket_size, label_reordered) AS corr_avg_basket_size,
  CORR(user_prod_avg_days_between_purchases, label_reordered) AS corr_avg_days_between_purchases
FROM `instacart-ml-model.agent_safe.candidate_reorder_features` TABLESAMPLE SYSTEM (1 PERCENT)
Note: correlation is a statistical estimate -- TABLESAMPLE cuts cost on a multi-million-row table with no meaningful accuracy loss. Compute every correlation in ONE query, not one call per column: splitting it into parallel calls still scans the same total bytes, summed against the same cost cap.'''


@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


class BigQuerySqlArgs(BaseModel):
    query: str = Field(description="A SQL query to run against agent_safe -- the only dataset "
                                    "this can reach. Filter or aggregate rather than relying on "
                                    "LIMIT, which doesn't reduce bytes scanned.")


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
    raw order and product features, pre-model. The only dataset this can reach
    (IAM-scoped).

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
        description="1-3 short, natural follow-up questions -- include these by default, "
                    "since they help the user continue the conversation. Leave empty only when "
                    "nothing natural genuinely fits or clarifying_question is set.")
    clarifying_question: str = Field(
        default="",
        description="Set this INSTEAD of answer_markdown, and leave answer_markdown empty, only when "
                    "the request is genuinely ambiguous and a wrong guess would mislead the user. Ask "
                    "one short question naming the specific choice. Otherwise answer -- do not ask "
                    "about anything a reasonable default covers.")

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
                         suggested_follow_ups: list[str] | None = None, clarifying_question: str = "") -> str:
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

    The screenshot is a static reference for layout only -- it is not a
    live capture, may not match the dashboard's current filter/selection
    state, and is never a source of numbers. Never mention the screenshot,
    the reference layout, or a "default" state to the user -- it's for your
    own understanding of page structure only. State only the current,
    filter_context-grounded reality.

    On the Model Performance page: the "Recall@5 by Model" chart's title
    and metric change with the Evaluation Metric Parameter slicer (Recall@5,
    NDCG@5, Precision@5, F1@5) -- the screenshot shows one fixed selection.
    If filter_context includes an "Evaluation Metric Parameter[Evaluation
    Metric Parameter]" entry, trust that value over the screenshot and
    describe the chart accordingly (e.g. "NDCG@5 by Model" if NDCG@5 is
    selected).

    Also on the Model Performance page: the "Feature Importance: Mean
    Absolute SHAP" chart shows feature importance for whichever base model
    is currently selected via the Model Selection Slicer (XGBoost Ranker,
    XGBoost Classifier, LightGBM Ranker, LightGBM Classifier) -- its title
    stays fixed, but the values shown depend on the selection, and the
    screenshot reflects only one. If filter_context includes a
    "base_models_dimension[model_name]" entry, trust that value over the
    screenshot when stating which model's feature importance is shown.

    That same chart also switches its whole plot type -- a bar chart
    ("Bar Plot (Mean Absolute SHAP)") or a beeswarm plot ("Beeswarm") --
    via the Plot Selection Buttons. The screenshot shown here already
    reflects filter_context's "Feature_Importance_Chart_Types[Chart_Types]"
    entry when present, so describe whichever plot type is actually shown.

    On the Financial Impact page: the sensitivity sliders (Conversion Rate,
    Churn Rate, Conversion/AOV/Churn Lift per 10% Recall) and the waterfall
    chart's model selection come from filter_context, not the screenshot --
    ground any DAX for this page in those values.
    """
    content = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
    image_name = resolve_image_name(page_name, filter_context)
    return [
        {"type": "text", "text": f"Page: {page_name}\n\n{content}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                      "data": _page_image_base64(image_name)}},
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
