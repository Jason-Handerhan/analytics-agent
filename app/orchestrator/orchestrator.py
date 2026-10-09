"""LangGraph tool-calling loop: state, nodes, routing, graph.
init_orchestrator() populates MCP_TOOLS/ALL_TOOLS/SYSTEM_MESSAGE lazily --
call once before running graph. AgentState and its TypedDicts live in
app/orchestrator/state.py; entry/exit helpers (called by the gateway, never
the graph) live in app/gateway/entry_exit.py -- kept out of this file so
neither needs this module's heavier LangGraph/LangChain-model imports.
"""
import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from itertools import groupby
from operator import itemgetter

import google.auth
import google.auth.transport.requests
import mistune
import msal
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_anthropic import ChatAnthropic, convert_to_anthropic_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.config import (
    ABSOLUTE_CAP,
    PENDING_APPROVAL_THRESHOLD,
    AGENT_SA_EMAIL,
    BIGQUERY_PRICE_PER_TIB,
    CHART_URL_EXPIRATION_HOURS,
    CONTEXT_DIR,
    DAX_ROW_CAP,
    DAX_TIMEOUT_SECONDS,
    GCP_PROJECT_ID,
    GCS_CHART_BUCKET,
    GITHUB_REPO_BRANCH,
    GITHUB_REPO_NAME,
    GITHUB_REPO_OWNER,
    LANGCHAIN_PROJECT,
    LANGSMITH_TRACING,
    MAX_ANSWER_CHARS,
    MAX_ANSWER_TABLE_ROWS,
    MAX_CLAIM_PRECISION,
    MAX_ITERATIONS,
    MAX_LENGTH_RETRIES,
    MAX_VERIFY_RETRIES,
    MCP_SERVER_HEADERS,
    MCP_SERVER_NAME,
    MCP_SERVER_URL,
    MODEL,
    NUMERIC_SOURCE_TOOLS,
    POWER_BI_DATASET_ID,
    POWER_BI_WORKSPACE_ID,
    get_secret,
)
from app.orchestrator.context import get_static_context
from app.orchestrator.shared_helpers import is_cancelled, label_chartable_result
from app.orchestrator.state import AgentState, ToolCallRecord, TurnError
from app.orchestrator.tools import (
    CombineResultsToolCallArgs,
    GenerateChartToolCallArgs,
    SubmitAnswerArgs,
    ToolError,
    chart_tool_call_standin,
    combine_results_call_standin,
    dry_run,
    get_measure_dax,
    get_page_info,
    run_bigquery_sql,
    search_docs,
    submit_answer,
)
from app.telemetry.writer import build_telemetry_row, write_telemetry_row

MCP_TOOLS: dict[str, BaseTool] = {}
ALL_TOOLS: dict[str, BaseTool] = {}
BIND_TOOLS_LIST: list[BaseTool] = []
SYSTEM_MESSAGE: SystemMessage | None = None
FILE_DESCRIPTIONS: dict[str, str] = {}

# Tools whose result gets a ref_id, resolvable by generate_chart/combine_results.
CHARTABLE_TOOLS = {"run_bigquery_sql", "run_dax_query", "combine_results"}


async def init_orchestrator(mcp_server_url: str = MCP_SERVER_URL) -> None:
    """Populates MCP_TOOLS, ALL_TOOLS, BIND_TOOLS_LIST, SYSTEM_MESSAGE,
    FILE_DESCRIPTIONS. Idempotent."""
    global MCP_TOOLS, ALL_TOOLS, BIND_TOOLS_LIST, SYSTEM_MESSAGE, FILE_DESCRIPTIONS
    if MCP_TOOLS and ALL_TOOLS and SYSTEM_MESSAGE:
        return

    # Set API Key and Tracing Environment Variables
    os.environ["ANTHROPIC_API_KEY"] = get_secret("anthropic-api-key", GCP_PROJECT_ID)
    os.environ["LANGSMITH_TRACING"] = LANGSMITH_TRACING
    os.environ["LANGSMITH_API_KEY"] = get_secret("langsmith-api-key", GCP_PROJECT_ID)
    os.environ["LANGCHAIN_PROJECT"] = LANGCHAIN_PROJECT
    FILE_DESCRIPTIONS = json.loads(
        (CONTEXT_DIR / "file_descriptions" / "github_file_descriptions.json").read_text())
    client = MultiServerMCPClient({
        MCP_SERVER_NAME: {"transport": "streamable_http", "url": mcp_server_url,
                           "headers": MCP_SERVER_HEADERS},
    })
    tools = await client.get_tools()
    MCP_TOOLS = {t.name: t for t in tools}
    ALL_TOOLS = {
        **MCP_TOOLS,
        "run_bigquery_sql": run_bigquery_sql,
        "submit_answer": submit_answer,
        "get_measure_dax": get_measure_dax,
        "search_docs": search_docs,
        "get_page_info": get_page_info,
    }
    # Model binds here, stand-in tools (generate_chart, combine_results) + non-stand-in tools (the rest)

    generate_chart_tool = convert_to_anthropic_tool(chart_tool_call_standin)

    stand_in_tool_names = ("generate_chart", "combine_results")
    stand_in_tools = [generate_chart_tool, combine_results_call_standin]

    non_stand_in_tools = [t for name, t in ALL_TOOLS.items()
                          if name not in stand_in_tool_names]

    BIND_TOOLS_LIST = non_stand_in_tools + stand_in_tools
    SYSTEM_MESSAGE = SystemMessage(content=get_static_context())


# --- Helper functions ----------------------------------------------------

# call_tool_node: cost gate + tool-call bookkeeping

def format_cost(num_bytes: int) -> str:
    """Byte count as a display dollar string -- display only, not the decision."""
    return f"${(num_bytes / 1024**4) * BIGQUERY_PRICE_PER_TIB:.2f}"

@lru_cache
def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        get_secret("power-bi-sp-client-id", GCP_PROJECT_ID),
        authority=f"https://login.microsoftonline.com/{get_secret('azure-tenant-id', GCP_PROJECT_ID)}",
        client_credential=get_secret("power-bi-sp-client-secret", GCP_PROJECT_ID),
    )


def get_power_bi_token() -> str:
    result = _msal_app().acquire_token_for_client(
        scopes=["https://analysis.windows.net/powerbi/api/.default"]
    )
    return result["access_token"]


# Inject Dax Args: For llm excluded MCP tool (run_dax_query) args
async def inject_dax_args(tc: dict) -> dict:
    """Adds Power BI auth/target/guardrail values to a run_dax_query call --
    excluded from the model's schema (exclude_args)"""
    try:
        access_token = await asyncio.to_thread(get_power_bi_token)
    except Exception as e:
        raise ToolError(f"Failed to get a Power BI access token: {e}") from e
    return {
        **tc,
        "args": {
            **tc["args"],
            "access_token": access_token,
            "workspace_id": POWER_BI_WORKSPACE_ID,
            "dataset_id": POWER_BI_DATASET_ID,
            "row_cap": DAX_ROW_CAP,
            "timeout_seconds": DAX_TIMEOUT_SECONDS,
        },
    }


@lru_cache
def get_github_token() -> str:
    """The GitHub read token, fetched once per process -- a static
    credential, unlike an OAuth access token that expires and needs refreshing."""
    return get_secret("github-read-token", GCP_PROJECT_ID)


def inject_code_search_args(tc: dict) -> dict:
    """Adds GitHub identity/connection values and this project's curated
    file descriptions to a get_repo_contents call -- excluded from the
    model's schema (exclude_args). A different deployer supplies their own
    file_descriptions for their own repo instead."""
    try:
        access_token = get_github_token()
    except Exception as e:
        raise ToolError(f"Failed to get the GitHub read token: {e}") from e
    return {
        **tc,
        "args": {
            **tc["args"],
            "owner": GITHUB_REPO_OWNER,
            "repo": GITHUB_REPO_NAME,
            "branch": GITHUB_REPO_BRANCH,
            "access_token": access_token,
            "file_descriptions": FILE_DESCRIPTIONS,
        },
    }


# Chart + combine_results dispatch -- source_ref resolution and GCS args for generate_chart

@lru_cache
def _get_adc_credentials():
    creds, _ = google.auth.default()
    return creds


async def get_gcp_access_token() -> str:
    """An OAuth access token for agent-sa, via ADC. Refreshes only when the
    cached credentials have actually expired, not on every call."""
    creds = _get_adc_credentials()
    if not creds.valid:
        await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())
    return creds.token


async def inject_chart_args(tc: dict) -> dict:
    """Adds GCS storage/auth values to a generate_chart call -- excluded
    from the model's schema (exclude_args), never stored in ToolCallRecord/telemetry."""
    try:
        access_token = await get_gcp_access_token()
    except Exception as e:
        raise ToolError(f"Failed to get a GCP access token: {e}") from e
    return {
        **tc,
        "args": {
            **tc["args"],
            "bucket_name": GCS_CHART_BUCKET,
            "storage_backend": "gcs",
            "expiration_hours": CHART_URL_EXPIRATION_HOURS,
            "access_token": access_token,
            "service_account_email": AGENT_SA_EMAIL,
        },
    }


def _lookup_source_ref(source_ref: str, prior_tool_calls: list["ToolCallRecord"]) -> "ToolCallRecord":
    """Finds the successful, chartable tool call source_ref points at, or
    raises an actionable error if it can't be found."""
    source_tc = next(
        (r for r in prior_tool_calls
         if r.get("ref_id") == source_ref and r["success"] and r["name"] in CHARTABLE_TOOLS),
        None)
    if source_tc is None:
        raise ToolError(
            f"'{source_ref}' is not a successfully completed run_bigquery_sql, "
            "run_dax_query, or combine_results call from earlier this turn -- it may not "
            "exist, may have failed, or may be from later in this same batch and hasn't "
            "run yet. Fetch the data first, then reference it in a follow-up step.")
    return source_tc


def resolve_chart_data(chart_tc: dict, prior_tool_calls: list["ToolCallRecord"]) -> dict:
    """Rewrites a model-facing chart call into the real MCP tool's args shape."""
    try:
        call_args = GenerateChartToolCallArgs.model_validate(chart_tc["args"])
    except ValidationError as e:
        raise ToolError(str(e.errors()[0])) from e
    source_tc = _lookup_source_ref(call_args.source_ref, prior_tool_calls)
    return {
        **chart_tc,
        "args": {"args": {"data": source_tc["result"], "spec": call_args.spec.model_dump()}},
    }


def resolve_combine_data(combine_tc: dict, prior_tool_calls: list["ToolCallRecord"]) -> dict:
    """Rewrites a model-facing combine call into the real MCP tool's args shape."""
    try:
        call_args = CombineResultsToolCallArgs.model_validate(combine_tc["args"])
    except ValidationError as e:
        raise ToolError(str(e.errors()[0])) from e
    source_tcs = [_lookup_source_ref(ref, prior_tool_calls) for ref in call_args.source_refs]
    return {
        **combine_tc,
        "args": {"args": {
            "data": [tc["result"] for tc in source_tcs],
            "method": call_args.method,
            "join_key": call_args.join_key,
            "join_how": call_args.join_how,
        }},
    }

# Tool-call record / batch bookkeeping

def _unwrap_content(content: str | list) -> str:
    """LangChain tools' content is a plain JSON string; MCP tools' content
    (via langchain_mcp_adapters) is a list of content blocks -- unwrap to
    the plain text either way."""
    return content[0]["text"] if isinstance(content, list) else content


def build_tool_call_record(
    tc: dict, message: ToolMessage, started_at: datetime, completed_at: datetime,
    ref_id: str | None = None,
) -> ToolCallRecord:
    """Builds a ToolCallRecord from a dispatched tool call and its ToolMessage."""
    query_text = tc["args"].get("query") or tc["args"].get("dax")
    success = message.status != "error"
    bytes_billed = (message.artifact or {}).get("bytes_billed")
    if not success:
        return ToolCallRecord(
            id=tc["id"], name=tc["name"], args=tc["args"], query_text=query_text, result="",
            success=False, error=_unwrap_content(message.content), ref_id=ref_id,
            bytes_billed=bytes_billed,
            started_at=started_at, completed_at=completed_at,
        )

    result = json.loads(_unwrap_content(message.content))
    return ToolCallRecord(
        id=tc["id"], name=tc["name"], args=tc["args"], query_text=query_text, result=result,
        success=True, error=None, ref_id=ref_id, bytes_billed=bytes_billed,
        started_at=started_at, completed_at=completed_at,
    )


def build_turn_error(
    stage: str, error_type: str, message: str, occurred_at: datetime, tool_call_id: str | None
) -> TurnError:
    """Builds a TurnError -- a guardrail decline or a real tool failure."""
    return TurnError(stage=stage, error_type=error_type, message=message,
                      occurred_at=occurred_at, tool_call_id=tool_call_id)


@dataclass
class ToolBatch:
    calls: list[dict]
    messages: list[ToolMessage]
    started_at: datetime
    completed_at: datetime
    error_type: str


@dataclass
class BatchResult:
    records: list[ToolCallRecord]
    display_messages: list[ToolMessage]
    errors: list[TurnError]
    ref_counter: int


def build_tool_call_records_and_messages(batch: ToolBatch, ref_counter: int) -> BatchResult:
    """Builds each call's ToolCallRecord, its message for state["messages"],
    and a TurnError for any failure."""
    records = []
    display_messages = []
    errors = []

    # asyncio.gather() preserves input order in its results -- zip preserves order correctly
    for tc, msg in zip(batch.calls, batch.messages):
        if tc["name"] in CHARTABLE_TOOLS and msg.status != "error":
            ref_id = f"ref_{ref_counter}"
            ref_counter += 1
            record = build_tool_call_record(tc, msg, batch.started_at, batch.completed_at, ref_id=ref_id)
            records.append(record)
            display_messages.append(label_chartable_result(msg, ref_id))
        elif tc["name"] == "submit_answer":  # Anthropic requires a tool message for every tool call
            display_messages.append(msg)
        else:
            record = build_tool_call_record(tc, msg, batch.started_at, batch.completed_at)
            records.append(record)
            display_messages.append(msg)
            if msg.status == "error":
                errors.append(build_turn_error(
                    "call_tool", batch.error_type, record["error"], record["completed_at"], record["id"]))
    return BatchResult(records, display_messages, errors, ref_counter)


async def dispatch_other_call(tc: dict, prior_tool_calls: list[ToolCallRecord]) -> ToolMessage:
    """Dispatches one non-BigQuery tool call, injecting hidden args first."""
    if tc["name"] == "submit_answer":
        # Only reachable when not the sole call this round.
        return ToolMessage(
            content="Not recorded -- submit_answer must be your only tool call "
                    "this round. Call it alone, with nothing else.",
            name=tc["name"], tool_call_id=tc["id"], status="error")
    if tc["name"] == "run_dax_query":
        try:
            full_args = await inject_dax_args(tc)
        except ToolError as e:
            return ToolMessage(content=str(e), name=tc["name"], tool_call_id=tc["id"], status="error")
        return await ALL_TOOLS[tc["name"]].ainvoke(full_args)
    if tc["name"] == "get_repo_contents":
        try:
            full_args = inject_code_search_args(tc)
        except ToolError as e:
            return ToolMessage(content=str(e), name=tc["name"], tool_call_id=tc["id"], status="error")
        return await ALL_TOOLS[tc["name"]].ainvoke(full_args)
    if tc["name"] == "generate_chart":
        try:
            args_w_data = resolve_chart_data(tc, prior_tool_calls)
            full_args = await inject_chart_args(args_w_data)
        except ToolError as e:
            return ToolMessage(content=str(e), name=tc["name"], tool_call_id=tc["id"], status="error")
        return await ALL_TOOLS[tc["name"]].ainvoke(full_args)
    if tc["name"] == "combine_results":
        try:
            full_args = resolve_combine_data(tc, prior_tool_calls)
        except ToolError as e:
            return ToolMessage(content=str(e), name=tc["name"], tool_call_id=tc["id"], status="error")
        return await ALL_TOOLS[tc["name"]].ainvoke(full_args)
    return await ALL_TOOLS[tc["name"]].ainvoke(tc)


def iteration_cap_update(tool_calls: list[dict], iteration_count: int) -> dict:
    """State update for a turn that hit max_iterations -- declines every call in the batch."""
    started_at = datetime.now(timezone.utc)
    messages = [
        ToolMessage(
            content=(
                "Not executed -- this turn has reached its iteration limit. "
                "You cannot call any more tools. Provide the best answer you "
                "can with what you already have, and state plainly that this "
                "is a partial answer."),
            name=tc["name"], tool_call_id=tc["id"], status="error")
        for tc in tool_calls
    ]
    completed_at = datetime.now(timezone.utc)
    records = [
        build_tool_call_record(tc, msg, started_at, completed_at)
        for tc, msg in zip(tool_calls, messages)
    ]
    return {
        "messages": messages,
        "iteration_count": iteration_count + 1,
        "iteration_cap_hit": True,
        "tool_calls": records,
        "errors": [
            build_turn_error("call_tool", "iteration_cap_hit", r["error"], r["completed_at"], r["id"])
            for r in records
        ],
    }


# Shared GFM table parser -- used by check_table_rows & extract_table_values
_markdown_ast = mistune.create_markdown(renderer="ast", plugins=["table"])


# check_length_node: answer-length guardrail

def check_answer_length(answer_markdown: str) -> bool:
    return len(answer_markdown) <= MAX_ANSWER_CHARS


def check_table_rows(answer_markdown: str) -> bool:
    """True if every markdown table in the answer has at most
    MAX_ANSWER_TABLE_ROWS data rows."""
    for block in _markdown_ast(answer_markdown):
        if block.get("type") != "table":
            continue
        body = next((c for c in block["children"] if c["type"] == "table_body"), None)
        if body is None:
            continue
        if len(body["children"]) > MAX_ANSWER_TABLE_ROWS:
            return False
    return True


# verify_node: pooled numeric-claim verification
# NUMERIC_SOURCE_TOOLS is defined near the top of the file, with the other
# module-level constants.


def extract_numeric_values(data: list[dict] | str) -> list[float]:
    """Recursively pulls every numeric leaf out of a tool result."""
    values: list[float] = []
    if isinstance(data, bool):
        return []
    if isinstance(data, (int, float)):
        values.append(float(data))
    elif isinstance(data, dict):
        for v in data.values():
            values.extend(extract_numeric_values(v))
    elif isinstance(data, list):
        for item in data:
            values.extend(extract_numeric_values(item))
    return values


def _cell_text(node: dict) -> str:
    """Extracts text from a cell by recursively checking levels for the text node."""
    if node.get("type") == "text":
        return node.get("raw", "")
    return "".join(_cell_text(child) for child in node.get("children", []))


def extract_table_values(answer_markdown: str) -> list[float]:
    """Pulls every numeric cell out of every markdown table in answer_markdown."""
    values: list[float] = []
    for block in _markdown_ast(answer_markdown):
        if block.get("type") != "table":
            continue
        body = next((c for c in block["children"] if c["type"] == "table_body"), None)
        if body is None:
            continue
        for row in body["children"]:
            for cell in row["children"]:
                text = _cell_text(cell).strip()
                normalized = text.replace(",", "").replace("%", "").replace("$", "")
                try:
                    values.append(float(normalized))
                except ValueError:
                    pass
    return values


def build_numeric_pool(tool_calls: list[ToolCallRecord]) -> set[float]:
    """Every number from this turn's successful BigQuery/DAX query results --
    the only tools that return genuine queried data, not incidental numbers
    embedded in code, doc chunks, or metadata."""
    pool: set[float] = set()
    for tc in tool_calls:
        if tc["success"] and tc["name"] in NUMERIC_SOURCE_TOOLS:
            pool.update(extract_numeric_values(tc["result"]))
    return pool


def _decimal_places(value: float) -> int:
    """Decimal digits in value's shortest string form -- a bare ".0"
    means a whole number (0 decimals), not 1."""
    text = str(value)
    if "." not in text:
        return 0
    frac = text.split(".")[1]
    return 0 if frac == "0" else len(frac)


def claim_matches_pool(claim: float, pool: set[float]) -> bool:
    """True if claim matches some pool value, or its *100 percentage-scaled
    form, once both are rounded to the claim's own decimal precision
    (capped at MAX_CLAIM_PRECISION), or once both are truncated to it
    instead -- covers a model that truncates rather than rounds when
    reproducing a long float."""
    precision = min(_decimal_places(claim), MAX_CLAIM_PRECISION)
    factor = 10 ** precision
    claim_rounded = round(claim, precision)
    claim_truncated = math.floor(claim * factor) / factor # Truncation to precision

    def _matches(v: float) -> bool:
        return (round(v, precision) == claim_rounded
                or math.floor(v * factor) / factor == claim_truncated)

    return any(_matches(v) or _matches(v * 100) for v in pool)


VERIFICATION_FAILURE_MESSAGE = ("I wasn't able to verify a confident answer to this "
                                 "question. Please try rephrasing or asking again.")
PENDING_APPROVAL_MESSAGE = ("This question requires running a large query. "
                            "Approve or reject it to continue.")
CANCELLED_MESSAGE = "This turn was cancelled."


# finalize_node: token/source tallying for the telemetry row

# Past-tense badges, no tool jargon -- reader is field-operations staff.
SOURCE_LABELS = {
    "run_bigquery_sql": "Query warehouse",
    "run_dax_query":    "Query dashboard",
    "get_measure_dax":  "Measure Lookup",
    "get_page_info":    "PBI page info",
    "get_repo_contents": "Read code",
    "search_docs":      "Doc Search",
    "generate_chart":   "Create chart",
    "combine_results":  "Combine results",
}


def sum_token_usage(messages: list[BaseMessage]) -> tuple[int, int]:
    """Sums prompt/completion tokens across every AIMessage this turn."""
    ai_messages = [m for m in messages if isinstance(m, AIMessage)]
    prompt_tokens = sum(m.usage_metadata["input_tokens"] for m in ai_messages)
    completion_tokens = sum(m.usage_metadata["output_tokens"] for m in ai_messages)
    return prompt_tokens, completion_tokens


def build_sources(tool_calls: list[ToolCallRecord]) -> list[str]:
    """Ordered source labels for successful calls, consecutive runs collapsed with a count."""
    ordered = sorted(tool_calls, key=itemgetter("started_at", "name"))
    names = [SOURCE_LABELS.get(tc["name"], tc["name"])
             for tc in ordered if tc["success"]]
    return [f"{name} ({n})" if (n := len(list(grp))) > 1 else name
            for name, grp in groupby(names)]


# --- Node functions ------------------------------------------------------

async def agent_node(state: AgentState) -> dict:
    """Calls the LLM with tools bound. Emits tool calls -- the final answer
    only ever arrives via submit_answer, never plain text.

    Extended thinking enabled for frontend display value -- incompatible with
    tool_choice="any", so a plain-text finish without submit_answer is
    possible again; verify_node's retry already recovers from that."""
    model = ChatAnthropic(
        model=MODEL,
        thinking={"type": "adaptive", "display": "summarized"},
    ).bind_tools(BIND_TOOLS_LIST, strict=True)
    response = await model.ainvoke([SYSTEM_MESSAGE, *state["history_messages"], *state["messages"]])
    return {"messages": [response], "llm_calls": state["llm_calls"] + 1}


async def call_tool_node(state: AgentState) -> dict:
    """Dispatches tool calls under the iteration/cost-cap guardrails;
    records a ToolCallRecord and, for any declined/failed call, a TurnError."""
    tool_calls = state["messages"][-1].tool_calls

    # Submit Answer Branch: Only triggered when submit_answer is the sole tool call, ignores iteration/cost caps.
    answer_submitted = len(tool_calls) == 1 and tool_calls[0]["name"] == "submit_answer"
    if answer_submitted:
        tc = tool_calls[0]
        msg = await submit_answer.ainvoke(tc)
        if msg.status == "error":
            # Malformed call -- let the model see the error and retry.
            return {"messages": [msg], "answer_submitted": False}
        #Validate Args
        args = SubmitAnswerArgs.model_validate(tc["args"])
        is_question = bool(args.clarifying_question.strip())
        return {
            "messages": [msg],
            "answer_submitted": True,
            "answer_markdown": args.clarifying_question if is_question else args.answer_markdown,
            "clarifying_question": args.clarifying_question,
            "all_prose_numeric_claims": args.all_prose_numeric_claims,
            "suggested_follow_ups": [] if is_question else args.suggested_follow_ups,
        }
    # Exceeds iteration cap Branch: Declines and builds error messages for every tool call in the batch.
    if state["iteration_count"] >= MAX_ITERATIONS:
        return iteration_cap_update(tool_calls, state["iteration_count"])

    bq_calls = [tc for tc in tool_calls if tc["name"] == "run_bigquery_sql"]
    other_calls = [tc for tc in tool_calls if tc["name"] != "run_bigquery_sql"]

    batch_bytes = 0
    estimates: list[int] = []
    cost_cap_exceeded = False
    needs_approval = False
    dry_run_error: str | None = None

    #Dry Run and Needs Approval Check
    if bq_calls:
        try:
            estimates = await asyncio.gather(*[dry_run(tc["args"]["query"]) for tc in bq_calls])
            batch_bytes = sum(estimates)
            cost_cap_exceeded = state["bytes_consumed"] + batch_bytes >= ABSOLUTE_CAP
            # TABLESAMPLE's dry-run estimate has repeatedly underestimated actual
            # billed bytes -- never trust it under the threshold, force a pause.
            uses_tablesample = any("TABLESAMPLE" in tc["args"]["query"].upper() for tc in bq_calls)
            needs_approval = ((batch_bytes > PENDING_APPROVAL_THRESHOLD or uses_tablesample)
                              and not cost_cap_exceeded and not state["approved_batch"])
        except ToolError as e:
            dry_run_error = str(e)

    # Pause before any tool runs, so the whole batch waits for approval. Iteration count is not incremented.
    if needs_approval:
        pending = [
            {"id": tc["id"], "query": tc["args"]["query"], "estimated_bytes": est}
            for tc, est in zip(bq_calls, estimates)
        ]
        largest_index = estimates.index(max(estimates))
        largest_pending_query = bq_calls[largest_index]["args"]["query"]
        return {
            "needs_approval": True,
            "pending_queries": pending,
            "largest_pending_query": largest_pending_query,
            "estimated_cost": format_cost(batch_bytes),
            "paused_at": datetime.now(timezone.utc),
        }

    other_started_at = datetime.now(timezone.utc)
    other_messages = await asyncio.gather(*[
        dispatch_other_call(tc, state["tool_calls"]) for tc in other_calls
    ])
    other_completed_at = datetime.now(timezone.utc)

    bq_started_at = datetime.now(timezone.utc)
    if dry_run_error is not None:
        bq_messages = [
            ToolMessage(
                content=(
                    f"Not executed -- a query in this batch failed to price (dry-run): "
                    f"{dry_run_error} All run_bigquery_sql calls in the same round are "
                    "declined together when this happens, so this error may describe a "
                    "different query than this one. Check each query in the batch, fix "
                    "the one it describes, and retry."),
                name=tc["name"], tool_call_id=tc["id"], status="error")
            for tc in bq_calls
        ]
    elif cost_cap_exceeded:
        bq_messages = [
            ToolMessage(
                content=(
                    f"Not executed -- this query would push the turn's total scanned "
                    f"data over the {ABSOLUTE_CAP / 1024**3:.0f} GiB limit. Try a "
                    "cheaper query, or provide the best answer you can with what you "
                    "already have and tell the user you hit the turn's cost limit."),
                name=tc["name"], tool_call_id=tc["id"], status="error")
            for tc in bq_calls
        ]
    else:
        full_bq_calls = [
            {**tc, "args": {**tc["args"], "conversation_id": state["conversation_id"]}}
            for tc in bq_calls
        ]
        bq_messages = await asyncio.gather(*[ALL_TOOLS[tc["name"]].ainvoke(tc) for tc in full_bq_calls])
    bq_completed_at = datetime.now(timezone.utc)

    ref_counter = sum(1 for r in state["tool_calls"] if r.get("ref_id")) + 1
    if dry_run_error is not None:
        bq_error_type = "dry_run_failed"
    elif cost_cap_exceeded:
        bq_error_type = "cost_cap_exceeded"
    else:
        bq_error_type = "tool_error"

    bq_batch = ToolBatch(bq_calls, bq_messages, bq_started_at, bq_completed_at, bq_error_type)
    bq_result = build_tool_call_records_and_messages(bq_batch, ref_counter)

    other_batch = ToolBatch(other_calls, other_messages, other_started_at, other_completed_at, "tool_error")
    other_result = build_tool_call_records_and_messages(other_batch, bq_result.ref_counter)

    # Only successful queries count -- a failed query was never billed.
    total_billed_bytes = sum(r["bytes_billed"] for r in bq_result.records if r["success"])
    chart_urls = [
        r["result"]["chart_url"] for r in other_result.records
        if r["name"] == "generate_chart" and r["success"]
    ]
    cancelled = await is_cancelled(state["conversation_id"])

    return {
        "messages": bq_result.display_messages + other_result.display_messages,
        "approved_batch": False,
        "iteration_count": state["iteration_count"] + 1,
        "bytes_consumed": state["bytes_consumed"] + total_billed_bytes,
        "cost_cap_exceeded": cost_cap_exceeded,
        "estimated_cost": format_cost(batch_bytes) if cost_cap_exceeded else state["estimated_cost"],
        "tool_calls": bq_result.records + other_result.records,
        "errors": bq_result.errors + other_result.errors,
        "cancelled": cancelled,
        "answer_submitted": False,
        "chart_urls": chart_urls,
    }


async def check_length_node(state: AgentState) -> dict:
    """Checks answer_markdown length and table size; on failure, requests a
    shorter answer or a smaller table."""
    answer = state["answer_markdown"]
    if check_answer_length(answer) and check_table_rows(answer):
        return {}
    if not check_answer_length(answer):
        note = ("Your answer is too long to display. Summarize the key findings "
                "concisely, or generate a chart instead of listing rows.")
    else:
        note = (f"Your answer includes a table with more than {MAX_ANSWER_TABLE_ROWS} rows. "
                "Show only the top results, summarize the rest, or generate a chart "
                "instead of listing every row.")
    return {
        "length_retry_count": state["length_retry_count"] + 1,
        "messages": [HumanMessage(content=note)],
    }


def verify_response(state: AgentState) -> tuple[bool, str | None]:
    """Shape-checks the submission, then checks every claimed number --
    prose claims plus every markdown table value -- against this turn's
    BigQuery/DAX results."""
    try:
        SubmitAnswerArgs.model_validate({
            "answer_markdown": state["answer_markdown"],
            "all_prose_numeric_claims": state["all_prose_numeric_claims"],
            "suggested_follow_ups": state["suggested_follow_ups"],
        })
    except ValidationError as e:
        return False, (f"No valid answer was submitted: {e}. Call submit_answer "
                        "with your final answer_markdown and numeric claims.")

    pool = build_numeric_pool(state["tool_calls"])
    all_claimed = state["all_prose_numeric_claims"] + extract_table_values(state["answer_markdown"])
    unmatched = [c for c in all_claimed if not claim_matches_pool(c, pool)]
    if unmatched:
        return False, f"{unmatched} do not match any tool result this turn."
    return True, None


async def verify_node(state: AgentState) -> dict:
    """Runs verify_response; retries are handled by route_after_verify's
    existing retry-budget check."""
    verified, error_message = verify_response(state)
    if verified:
        return {"verified": True}
    return {
        "verified": False,
        "verification_retry_count": state["verification_retry_count"] + 1,
        "messages": [HumanMessage(content=error_message)],
    }


async def finalize_node(state: AgentState) -> dict:
    """Tallies tokens/sources, builds and writes the telemetry row. Each
    terminal state ships its own canned message if the turn has no real
    answer_markdown yet; a cancelled turn keeps a real answer only if
    verify_node already confirmed it this turn."""
    cancelled = await is_cancelled(state["conversation_id"])
    # A cancelled turn no longer needs approval -- cancelled overrides it everywhere.
    needs_approval = False if cancelled else state["needs_approval"]
    if cancelled:
        # Only show a real answer if verify_node already confirmed it this turn --
        # otherwise a cancel arriving right after submit_answer, before verify_node
        # runs, would display an unverified answer_markdown. Checked before
        # needs_approval -- a cancel request should win over a fresh pause.
        answer_markdown = state["answer_markdown"] if state["verified"] else CANCELLED_MESSAGE
    elif needs_approval:
        answer_markdown = PENDING_APPROVAL_MESSAGE
    elif not state["verified"]:
        answer_markdown = VERIFICATION_FAILURE_MESSAGE
    else:
        answer_markdown = state["answer_markdown"]

    prompt_tokens, completion_tokens = sum_token_usage(state["messages"])
    sources = build_sources(state["tool_calls"])
    row = build_telemetry_row(
        conversation_id=state["conversation_id"],
        user_id=state["user_id"],
        question=state["question"],
        answer_markdown=answer_markdown,
        turn_started_at=state["turn_started_at"],
        turn_completed_at=datetime.now(timezone.utc),
        filter_context=state["filter_context"],
        active_page=state["active_page"],
        tool_calls=state["tool_calls"],
        errors=state["errors"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        llm_calls=state["llm_calls"],
        bytes_consumed=state["bytes_consumed"],
        bytes_consumed_baseline=state["bytes_consumed_baseline"],
        iteration_count=state["iteration_count"],
        verified=state["verified"],
        verification_retry_count=state["verification_retry_count"],
        length_retry_count=state["length_retry_count"],
        needs_approval=needs_approval,
        cost_cap_exceeded=state["cost_cap_exceeded"],
        cancelled=cancelled,
        iteration_cap_hit=state["iteration_cap_hit"],
        estimated_cost=state["estimated_cost"],
        pending_queries=state["pending_queries"],
        largest_pending_query=state["largest_pending_query"],
        approval_decision=state["approval_decision"],
        deferred_dax=state["deferred_dax"],
        chart_urls=state["chart_urls"],
        suggested_follow_ups=state["suggested_follow_ups"],
        all_prose_numeric_claims=state["all_prose_numeric_claims"],
        clarifying_question=state["clarifying_question"],
    )
    await write_telemetry_row(row)
    return {
        "answer_markdown": answer_markdown,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "sources": sources,
        "cancelled": cancelled,
        "needs_approval": needs_approval,
    }


# --- Routing functions -----------------------------------------------------

async def route_entry(state: AgentState) -> str:
    """A resumed, approved turn replays its dangling AIMessage.tool_calls
    straight into call_tool -- a fresh turn starts at agent as usual."""
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    return "call_tool" if state["approved_batch"] else "agent"


async def route_after_agent(state: AgentState) -> str:
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    return "call_tool" if state["messages"][-1].tool_calls else "check_length"


async def route_after_call_tool(state: AgentState) -> str:
    if state["needs_approval"] or await is_cancelled(state["conversation_id"]):
        return "finalize"
    if state["answer_submitted"]:
        return "check_length"
    return "agent"   # also cost_cap_exceeded and iteration_cap_hit


async def route_after_check_length(state: AgentState) -> str:
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    answer = state["answer_markdown"]
    if check_answer_length(answer) and check_table_rows(answer):
        return "verify"
    if state["length_retry_count"] < MAX_LENGTH_RETRIES:
        return "agent"
    return "finalize"


async def route_after_verify(state: AgentState) -> str:
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    if state["verified"]:
        return "finalize"
    if state["verification_retry_count"] < MAX_VERIFY_RETRIES:
        return "agent"
    return "finalize"


# --- Graph -------------------------------------------------------------

g = StateGraph(AgentState)
g.add_node("agent",        agent_node)
g.add_node("call_tool",    call_tool_node)
g.add_node("check_length", check_length_node)
g.add_node("verify",       verify_node)
g.add_node("finalize",     finalize_node)

#Start
g.add_conditional_edges(START, route_entry,
    ["call_tool", "agent", "finalize"])

#Tool Loop
g.add_conditional_edges("agent", route_after_agent,
    ["call_tool", "check_length", "finalize"])
g.add_conditional_edges("call_tool", route_after_call_tool,
    ["finalize", "agent", "check_length"])

#Verification
g.add_conditional_edges("check_length", route_after_check_length,
    ["verify", "agent", "finalize"])
g.add_conditional_edges("verify", route_after_verify,
    ["finalize", "agent"])

#Finalize
g.add_edge("finalize", END)

#Compile Graph
graph = g.compile()
