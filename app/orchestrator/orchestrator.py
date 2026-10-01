"""LangGraph tool-calling loop: state, nodes, routing, graph.
init_orchestrator() populates MCP_TOOLS/ALL_TOOLS/SYSTEM_MESSAGE lazily --
call once before running graph.
"""
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from operator import itemgetter
from typing import Annotated, Any, TypedDict

import google.auth
import google.auth.transport.requests
import mistune
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_anthropic import ChatAnthropic, convert_to_anthropic_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from app.config import (
    ABSOLUTE_CAP,
    AGENT_SA_EMAIL,
    BIGQUERY_PRICE_PER_TIB,
    CHART_URL_EXPIRATION_HOURS,
    DAX_ROW_CAP,
    DAX_TIMEOUT_SECONDS,
    GCP_PROJECT_ID,
    GCS_CHART_BUCKET,
    MAX_ANSWER_CHARS,
    MAX_ANSWER_TABLE_ROWS,
    MAX_ITERATIONS,
    MAX_LENGTH_RETRIES,
    MAX_VERIFY_RETRIES,
    MCP_SERVER_HEADERS,
    MCP_SERVER_NAME,
    MCP_SERVER_URL,
    MODEL,
    POWER_BI_DATASET_ID,
    POWER_BI_WORKSPACE_ID,
    get_secret,
)
from app.orchestrator.context import get_static_context
from app.orchestrator.models import AgentResponse
from app.orchestrator.power_bi_auth import get_power_bi_token
from app.orchestrator.tools import (
    GenerateChartToolCallArgs,
    SubmitAnswerArgs,
    ToolError,
    chart_tool_call_standin,
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


async def init_orchestrator(mcp_server_url: str = MCP_SERVER_URL) -> None:
    """Populates MCP_TOOLS, ALL_TOOLS, BIND_TOOLS_LIST, SYSTEM_MESSAGE. Idempotent."""
    global MCP_TOOLS, ALL_TOOLS, BIND_TOOLS_LIST, SYSTEM_MESSAGE
    if MCP_TOOLS and ALL_TOOLS and SYSTEM_MESSAGE:
        return
    
    # Get API Key
    os.environ["ANTHROPIC_API_KEY"] = get_secret("anthropic-api-key", GCP_PROJECT_ID)
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
    # Model binds here, not ALL_TOOLS -- generate_chart's entry is swapped for
    # a stand-in schema (source_ref + spec). Dispatch still uses ALL_TOOLS.
    # Pre-converted to a raw Anthropic tool dict (no strict kwarg) so
    # bind_tools(strict=True) below passes it through untouched.
    generate_chart_tool = convert_to_anthropic_tool(chart_tool_call_standin)
    BIND_TOOLS_LIST = [generate_chart_tool if name == "generate_chart" else t
                       for name, t in ALL_TOOLS.items()]
    SYSTEM_MESSAGE = SystemMessage(content=get_static_context())


# --- State & reducers --------------------------------------------------

class ToolCallRecord(TypedDict):
    id: str
    name: str
    args: dict
    query_text: str | None  # args["query"] or args["dax"] -- SQL/DAX only, else None
    result: Any  # shape depends on the tool
    success: bool
    error: str | None
    ref_id: str | None  # set only for a chartable tool's successful result
    started_at: datetime
    completed_at: datetime


class TurnError(TypedDict):
    stage: str
    error_type: str
    message: str
    occurred_at: datetime
    tool_call_id: str | None  # matches ToolCallRecord.id


def append_list(existing: list, new: list) -> list:
    """Accumulates a list across graph supersteps."""
    return existing + new


class AgentState(TypedDict):
    # Set once, at invocation
    question: str
    conversation_id: str
    user_id: str
    turn_started_at: datetime
    filter_context: list[dict]
    active_page: str | None
    image_base64: str | None
    history_messages: list[BaseMessage]  # conversation history

    # Accumulated during the tool loop
    messages: Annotated[list[BaseMessage], add_messages]
    tool_calls: Annotated[list[ToolCallRecord], append_list]
    iteration_count: int

    # Separate retry budgets
    verification_retry_count: int
    length_retry_count: int

    verified: bool

    # Resource accumulators
    bytes_consumed: int
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int

    errors: Annotated[list[TurnError], append_list]
    cancelled: bool

    # Guardrail outcomes
    needs_approval: bool
    pending_queries: list[dict]
    deferred_dax: list[dict]
    estimated_cost: str | None
    cost_cap_exceeded: bool
    iteration_cap_hit: bool
    answer_submitted: bool  # True only when submit_answer was the sole tool

    # Building toward AgentResponse
    answer_markdown: str
    chart_urls: Annotated[list[str], append_list]
    sources: list[str]
    all_prose_numeric_claims: list[float]  # llm provided numeric claims in prose
    suggested_follow_ups: list[str]


# --- Entry/exit: called by the gateway, not the graph -------------

def build_human_message(question: str, filter_context: list[dict], active_page: str | None) -> HumanMessage:
    """The turn's initial HumanMessage -- question plus dashboard grounding."""
    content = question
    if active_page:
        content += f"\n\nCurrent dashboard page: {active_page}"
    if filter_context:
        content += f"\n\nCurrent filter state: {filter_context}"
    return HumanMessage(content=content)


def build_initial_state(
    question: str, conversation_id: str, user_id: str,
    filter_context: list[dict], active_page: str | None, image_base64: str | None,
) -> AgentState:
    """Initializes every AgentState field for a fresh turn."""
    return AgentState(
        question=question,
        conversation_id=conversation_id,
        user_id=user_id,
        turn_started_at=datetime.now(timezone.utc),
        filter_context=filter_context,
        active_page=active_page,
        image_base64=image_base64,
        history_messages=[],
        messages=[build_human_message(question, filter_context, active_page)],
        tool_calls=[],
        iteration_count=0,
        verification_retry_count=0,
        length_retry_count=0,
        verified=False,
        bytes_consumed=0,
        prompt_tokens=0,
        completion_tokens=0,
        llm_calls=0,
        errors=[],
        cancelled=False,
        needs_approval=False,
        pending_queries=[],
        deferred_dax=[],
        estimated_cost=None,
        cost_cap_exceeded=False,
        iteration_cap_hit=False,
        answer_submitted=False,
        answer_markdown="",
        chart_urls=[],
        sources=[],
        all_prose_numeric_claims=[],
        suggested_follow_ups=[],
    )


def build_agent_response(state: AgentState) -> AgentResponse:
    """Converts the graph's final state into the gateway's wire format."""
    return AgentResponse(
        answer_markdown=state["answer_markdown"],
        sources=state["sources"],
        needs_approval=state["needs_approval"],
        chart_urls=state["chart_urls"],
        suggested_follow_ups=state["suggested_follow_ups"],
        iteration_cap_hit=state["iteration_cap_hit"],
        pending_query=state["pending_queries"][-1]["query"] if state["pending_queries"] else None,
        estimated_cost=state["estimated_cost"],
        cost_cap_exceeded=state["cost_cap_exceeded"],
    )


# --- Helper functions ----------------------------------------------------

# call_tool_node: cost gate + tool-call bookkeeping

def exceeds_absolute_cap(bytes_consumed: int, batch_bytes: int, cap: int = ABSOLUTE_CAP) -> bool:
    """True if dispatching this batch would push the turn's cumulative bytes at/over cap."""
    return bytes_consumed + batch_bytes >= cap


def format_cost(num_bytes: int) -> str:
    """Byte count as a display dollar string -- display only, not the decision."""
    return f"${(num_bytes / 1024**4) * BIGQUERY_PRICE_PER_TIB:.2f}"

# Inject Dax Args: For llm excluded MCP tool (run_dax_query) args
def inject_dax_args(tc: dict) -> dict:
    """Adds Power BI auth/target/guardrail values to a run_dax_query call --
    excluded from the model's schema (exclude_args)"""
    return {
        **tc,
        "args": {
            **tc["args"],
            "access_token": get_power_bi_token(),
            "workspace_id": POWER_BI_WORKSPACE_ID,
            "dataset_id": POWER_BI_DATASET_ID,
            "row_cap": DAX_ROW_CAP,
            "timeout_seconds": DAX_TIMEOUT_SECONDS,
        },
    }


# Chart dispatch -- source_ref resolution and GCS args for generate_chart

def get_gcp_access_token() -> str:
    """An OAuth access token for agent-sa, via ADC."""
    creds, _ = google.auth.default()
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def inject_chart_args(tc: dict) -> dict:
    """Adds GCS storage/auth values to a generate_chart call -- excluded
    from the model's schema (exclude_args), never stored in ToolCallRecord/telemetry."""
    return {
        **tc,
        "args": {
            **tc["args"],
            "bucket_name": GCS_CHART_BUCKET,
            "storage_backend": "gcs",
            "expiration_hours": CHART_URL_EXPIRATION_HOURS,
            "access_token": get_gcp_access_token(),
            "service_account_email": AGENT_SA_EMAIL,
        },
    }


CHARTABLE_TOOLS = {"run_bigquery_sql", "run_dax_query"}


def _lookup_chart_source(source_ref: str, prior_tool_calls: list["ToolCallRecord"]) -> "ToolCallRecord":
    """Finds the successful, chartable tool call source_ref points at, or
    raises an actionable error if it can't be found."""
    source_tc = next(
        (r for r in prior_tool_calls
         if r.get("ref_id") == source_ref and r["success"] and r["name"] in CHARTABLE_TOOLS),
        None)
    if source_tc is None:
        raise ToolError(
            f"'{source_ref}' is not a successfully completed run_bigquery_sql or "
            "run_dax_query call from earlier this turn -- it may not exist, may have failed, "
            "or may be from later in this same batch and hasn't run yet. Fetch the data "
            "first, then call generate_chart in a follow-up step.")
    return source_tc


def resolve_chart_data(chart_tc: dict, prior_tool_calls: list["ToolCallRecord"]) -> dict:
    """Rewrites a model-facing chart call into the real MCP tool's args shape."""
    call_args = GenerateChartToolCallArgs.model_validate(chart_tc["args"])
    source_tc = _lookup_chart_source(call_args.source_ref, prior_tool_calls)
    return {
        **chart_tc,
        "args": {"args": {"data": source_tc["result"], "spec": call_args.spec.model_dump()}},
    }


def _label_chartable_result(msg: ToolMessage, ref_id: str) -> ToolMessage:
    """Rebuilds a tool result's message with a visible reference label for
    the model to copy into generate_chart's source_ref."""
    return ToolMessage(
        content=f"Reference id for charting this result: {ref_id}\n{msg.content}",
        name=msg.name, tool_call_id=msg.tool_call_id, status=msg.status,
    )


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
    if not success:
        return ToolCallRecord(
            id=tc["id"], name=tc["name"], args=tc["args"], query_text=query_text, result="",
            success=False, error=_unwrap_content(message.content), ref_id=ref_id,
            started_at=started_at, completed_at=completed_at,
        )

    result = json.loads(_unwrap_content(message.content))
    return ToolCallRecord(
        id=tc["id"], name=tc["name"], args=tc["args"], query_text=query_text, result=result,
        success=True, error=None, ref_id=ref_id,
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
            display_messages.append(_label_chartable_result(msg, ref_id))
        elif tc["name"] == "submit_answer":  # Anthropic requires a tool message for every tool call
            display_messages.append(msg)
            record = None
        else:
            record = build_tool_call_record(tc, msg, batch.started_at, batch.completed_at)
            records.append(record)
            display_messages.append(msg)

        if record is not None and not record["success"]:
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
        return await ALL_TOOLS[tc["name"]].ainvoke(inject_dax_args(tc))
    if tc["name"] == "generate_chart":
        try:
            resolved = resolve_chart_data(tc, prior_tool_calls)
        except ToolError as e:
            return ToolMessage(content=str(e), name=tc["name"], tool_call_id=tc["id"], status="error")
        return await ALL_TOOLS[tc["name"]].ainvoke(inject_chart_args(resolved))
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


# Shared GFM table parser -- used by check_table_rows below and by
# extract_table_values further down (verify_node section).
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

NUMERIC_SOURCE_TOOLS = {"run_bigquery_sql", "run_dax_query"}


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
    """True if claim is a legitimately-rounded or percentage-scaled
    representation of some tool result, not just numerically close to one."""
    precision = _decimal_places(claim)
    return any(
        round(v, precision) == claim or round(v * 100, precision) == claim
        for v in pool
    )


VERIFICATION_FAILURE_MESSAGE = ("I wasn't able to verify a confident answer to this "
                                 "question. Please try rephrasing or asking again.")


# finalize_node: token/source tallying for the telemetry row

# Past-tense badges, no tool jargon -- reader is field-operations staff.
SOURCE_LABELS = {
    "run_bigquery_sql": "Query warehouse",
    "run_dax_query":    "Query dashboard",
    "get_measure_dax":  "Measure Lookup",
    "get_page_info":    "PBI page info",
    "list_repo_files":  "list code",
    "read_repo_file":   "Read code",
    "search_docs":      "Doc Search",
    "generate_chart":   "Create chart",
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

    # A sole submit_answer call is always dispatched, even past the iteration cap
    answer_submitted = len(tool_calls) == 1 and tool_calls[0]["name"] == "submit_answer"
    if answer_submitted:
        tc = tool_calls[0]
        msg = await submit_answer.ainvoke(tc)
        if msg.status == "error":
            # Malformed call -- let the model see the error and retry.
            return {"messages": [msg], "answer_submitted": False}
        #Validate Args
        args = SubmitAnswerArgs.model_validate(tc["args"])
        return {
            "messages": [msg],
            "answer_submitted": True,
            "answer_markdown": args.answer_markdown,
            "all_prose_numeric_claims": args.all_prose_numeric_claims,
            "suggested_follow_ups": args.suggested_follow_ups,
        }

    if state["iteration_count"] >= MAX_ITERATIONS:
        return iteration_cap_update(tool_calls, state["iteration_count"])

    bq_calls = [tc for tc in tool_calls if tc["name"] == "run_bigquery_sql"]
    other_calls = [tc for tc in tool_calls if tc["name"] != "run_bigquery_sql"]

    batch_bytes = 0
    estimates: list[int] = []
    cost_cap_exceeded = False
    if bq_calls:
        estimates = await asyncio.gather(*[dry_run(tc["args"]["query"]) for tc in bq_calls])
        batch_bytes = sum(estimates)
        cost_cap_exceeded = exceeds_absolute_cap(state["bytes_consumed"], batch_bytes)

    other_started_at = datetime.now(timezone.utc)
    other_messages = await asyncio.gather(*[
        dispatch_other_call(tc, state["tool_calls"]) for tc in other_calls
    ])
    other_completed_at = datetime.now(timezone.utc)

    bq_started_at = datetime.now(timezone.utc)
    if cost_cap_exceeded:
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
        bq_messages = await asyncio.gather(*[ALL_TOOLS[tc["name"]].ainvoke(tc) for tc in bq_calls])
    bq_completed_at = datetime.now(timezone.utc)

    ref_counter = sum(1 for r in state["tool_calls"] if r.get("ref_id")) + 1
    bq_error_type = "cost_cap_exceeded" if cost_cap_exceeded else "tool_error"

    bq_batch = ToolBatch(bq_calls, bq_messages, bq_started_at, bq_completed_at, bq_error_type)
    bq_result = build_tool_call_records_and_messages(bq_batch, ref_counter)

    other_batch = ToolBatch(other_calls, other_messages, other_started_at, other_completed_at, "tool_error")
    other_result = build_tool_call_records_and_messages(other_batch, bq_result.ref_counter)

    # Only bill bytes for calls that actually ran -- a failed query was never billed.
    billed_bytes = 0 if cost_cap_exceeded else sum(
        est for est, r in zip(estimates, bq_result.records) if r["success"]
    )
    chart_urls = [
        r["result"]["chart_url"] for r in other_result.records
        if r["name"] == "generate_chart" and r["success"]
    ]
    return {
        "messages": bq_result.display_messages + other_result.display_messages,
        "iteration_count": state["iteration_count"] + 1,
        "bytes_consumed": state["bytes_consumed"] + billed_bytes,
        "cost_cap_exceeded": cost_cap_exceeded,
        "estimated_cost": format_cost(batch_bytes) if cost_cap_exceeded else state["estimated_cost"],
        "tool_calls": bq_result.records + other_result.records,
        "errors": bq_result.errors + other_result.errors,
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
    """Tallies tokens/sources, builds and writes the telemetry row. A turn
    that ran verification and never passed ships the static failure message
    instead -- cancelled/needs_approval turns bypass verification entirely,
    so they're untouched here."""
    answer_markdown = state["answer_markdown"]
    if not state["verified"] and not state["cancelled"] and not state["needs_approval"]:
        answer_markdown = VERIFICATION_FAILURE_MESSAGE

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
        iteration_count=state["iteration_count"],
        verified=state["verified"],
        verification_retry_count=state["verification_retry_count"],
        length_retry_count=state["length_retry_count"],
        needs_approval=state["needs_approval"],
        cost_cap_exceeded=state["cost_cap_exceeded"],
        cancelled=state["cancelled"],
        iteration_cap_hit=state["iteration_cap_hit"],
        estimated_cost=state["estimated_cost"],
        pending_queries=state["pending_queries"],
        deferred_dax=state["deferred_dax"],
        chart_urls=state["chart_urls"],
        suggested_follow_ups=state["suggested_follow_ups"],
        all_prose_numeric_claims=state["all_prose_numeric_claims"],
    )
    await write_telemetry_row(row)
    return {
        "answer_markdown": answer_markdown,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "sources": sources,
    }


# --- Routing functions -----------------------------------------------------

def route_after_agent(state: AgentState) -> str:
    return "call_tool" if state["messages"][-1].tool_calls else "check_length"


def route_after_call_tool(state: AgentState) -> str:
    if state["cancelled"] or state["needs_approval"]:
        return "finalize"
    if state["answer_submitted"]:
        return "check_length"
    return "agent"   # also cost_cap_exceeded and iteration_cap_hit


def route_after_check_length(state: AgentState) -> str:
    answer = state["answer_markdown"]
    if check_answer_length(answer) and check_table_rows(answer):
        return "verify"
    if state["length_retry_count"] < MAX_LENGTH_RETRIES:
        return "agent"
    return "finalize"


def route_after_verify(state: AgentState) -> str:
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
g.add_edge(START, "agent")

#Tool Loop
g.add_conditional_edges("agent", route_after_agent,
    ["call_tool", "check_length"])
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
