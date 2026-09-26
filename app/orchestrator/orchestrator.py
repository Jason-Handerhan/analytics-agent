"""LangGraph tool-calling loop: state, nodes, routing, graph
(.claude/rules/orchestrator.md). init_orchestrator() populates MCP_TOOLS/
ALL_TOOLS/SYSTEM_MESSAGE lazily -- call once before running graph.
"""
import asyncio
import json
from datetime import datetime, timezone
from itertools import groupby
from operator import itemgetter
from typing import Annotated, TypedDict

import mistune
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from app.config import (
    ABSOLUTE_CAP,
    BIGQUERY_PRICE_PER_TIB,
    MAX_ANSWER_CHARS,
    MAX_ANSWER_TABLE_ROWS,
    MAX_ITERATIONS,
    MAX_LENGTH_RETRIES,
    MAX_VERIFY_RETRIES,
    MCP_SERVER_HEADERS,
    MCP_SERVER_NAME,
    MCP_SERVER_URL,
    MODEL,
)
from app.orchestrator.context import get_static_context
from app.orchestrator.tools import SubmitAnswerArgs, dry_run, run_bigquery_sql, submit_answer
from app.telemetry.writer import build_telemetry_row, write_telemetry_row

MCP_TOOLS: dict[str, BaseTool] = {}
ALL_TOOLS: dict[str, BaseTool] = {}
SYSTEM_MESSAGE: SystemMessage | None = None


async def init_orchestrator(mcp_server_url: str = MCP_SERVER_URL) -> None:
    """Populates MCP_TOOLS, ALL_TOOLS, SYSTEM_MESSAGE. Idempotent."""
    global MCP_TOOLS, ALL_TOOLS, SYSTEM_MESSAGE
    if MCP_TOOLS and ALL_TOOLS and SYSTEM_MESSAGE:
        return
    client = MultiServerMCPClient({
        MCP_SERVER_NAME: {"transport": "streamable_http", "url": mcp_server_url,
                           "headers": MCP_SERVER_HEADERS},
    })
    tools = await client.get_tools()
    MCP_TOOLS = {t.name: t for t in tools}
    ALL_TOOLS = {**MCP_TOOLS, "run_bigquery_sql": run_bigquery_sql, "submit_answer": submit_answer}
    SYSTEM_MESSAGE = SystemMessage(content=get_static_context())


# --- State & reducers --------------------------------------------------

class ToolCallRecord(TypedDict):
    id: str
    name: str
    args: dict
    query_text: str | None  # args["query"] or args["dax"] -- SQL/DAX only, else None
    result: list[dict] | str
    success: bool
    error: str | None
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
    history_messages: list[BaseMessage]  # reconstructed history, kept OUT of
                                         # `messages` -- see orchestrator.md

    # Accumulated during the loop
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
                            # call dispatched this round -- doesn't count
                            # against iteration_count (it's the response
                            # mechanism, not a data-gathering tool)

    # Building toward AgentResponse
    answer_markdown: str
    chart_url: str | None
    sources: list[str]
    all_prose_numeric_claims: list[float]  # every number the model states in
                                           # prose as fact -- set by call_tool_node
                                           # from submit_answer's args
    suggested_follow_ups: list[str]        # same source; flows straight to
                                           # AgentResponse, unlike the claims


# --- Helper functions ----------------------------------------------------

# call_tool_node: cost gate + tool-call bookkeeping

def exceeds_absolute_cap(bytes_consumed: int, batch_bytes: int, cap: int = ABSOLUTE_CAP) -> bool:
    """True if dispatching this batch would push the turn's cumulative bytes at/over cap."""
    return bytes_consumed + batch_bytes >= cap


def format_cost(num_bytes: int) -> str:
    """Byte count as a display dollar string -- display only, not the decision."""
    return f"${(num_bytes / 1024**4) * BIGQUERY_PRICE_PER_TIB:.2f}"


def build_tool_call_record(
    tc: dict, message: ToolMessage, started_at: datetime, completed_at: datetime
) -> ToolCallRecord:
    """Builds a ToolCallRecord from a dispatched tool call and its ToolMessage."""
    query_text = tc["args"].get("query") or tc["args"].get("dax")
    success = message.status != "error"
    if not success:
        return ToolCallRecord(
            id=tc["id"], name=tc["name"], args=tc["args"], query_text=query_text, result="",
            success=False, error=message.content,
            started_at=started_at, completed_at=completed_at,
        )

    # MCP tools: result in artifact (dict). Plain tools: JSON string in content.
    result = message.artifact if message.artifact is not None else json.loads(message.content)
    return ToolCallRecord(
        id=tc["id"], name=tc["name"], args=tc["args"], query_text=query_text, result=result,
        success=True, error=None,
        started_at=started_at, completed_at=completed_at,
    )


def build_turn_error(
    stage: str, error_type: str, message: str, occurred_at: datetime, tool_call_id: str | None
) -> TurnError:
    """Builds a TurnError -- a guardrail decline or a real tool failure."""
    return TurnError(stage=stage, error_type=error_type, message=message,
                      occurred_at=occurred_at, tool_call_id=tool_call_id)


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

# Separate from FRIENDLY_TOOL_NAMES (.claude/rules/gateway.md): those are
# present-tense progress messages, these are past-tense badges. No tool jargon
# -- the reader is field-operations staff. "Model fields" is the SEMANTIC
# model's, not BigQuery's.
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
    only ever arrives via submit_answer, never plain text."""
    model = ChatAnthropic(model=MODEL).bind_tools(list(ALL_TOOLS.values()), strict=True)
    response = await model.ainvoke([SYSTEM_MESSAGE, *state["history_messages"], *state["messages"]])
    return {"messages": [response], "llm_calls": state["llm_calls"] + 1}


async def call_tool_node(state: AgentState) -> dict:
    """Dispatches tool calls under the iteration/cost-cap guardrails;
    records a ToolCallRecord and, for any declined/failed call, a TurnError."""
    tool_calls = state["messages"][-1].tool_calls

    # A sole submit_answer call is always dispatched, even past the iteration
    # cap -- it's the response mechanism, not a data-gathering tool, so it
    # doesn't count against iteration_count and doesn't get a ToolCallRecord
    # (its content -- answer_markdown, claims, follow-ups -- is already
    # captured directly in state).
    answer_submitted = len(tool_calls) == 1 and tool_calls[0]["name"] == "submit_answer"
    if answer_submitted:
        tc = tool_calls[0]
        msg = await submit_answer.ainvoke(tc)
        args = tc["args"]
        return {
            "messages": [msg],
            "answer_submitted": True,
            "answer_markdown": args["answer_markdown"],
            "all_prose_numeric_claims": args["all_prose_numeric_claims"],
            "suggested_follow_ups": args["suggested_follow_ups"],
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
    other_messages = await asyncio.gather(*[ALL_TOOLS[tc["name"]].ainvoke(tc) for tc in other_calls])
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

    # gather() preserves input order, so zip(calls, messages) pairs correctly.
    bq_records = [
        build_tool_call_record(tc, msg, bq_started_at, bq_completed_at)
        for tc, msg in zip(bq_calls, bq_messages)
    ]
    # submit_answer excluded -- if batched with a real tool, its ToolMessage
    # is still dispatched above (satisfies the API's tool_use/tool_result
    # protocol) but the submission itself is ignored.
    other_records = [
        build_tool_call_record(tc, msg, other_started_at, other_completed_at)
        for tc, msg in zip(other_calls, other_messages)
        if tc["name"] != "submit_answer"
    ]

    bq_error_type = "cost_cap_exceeded" if cost_cap_exceeded else "tool_error"
    errors = [
        build_turn_error("call_tool", bq_error_type, r["error"], r["completed_at"], r["id"])
        for r in bq_records if not r["success"]
    ] + [
        build_turn_error("call_tool", "tool_error", r["error"], r["completed_at"], r["id"])
        for r in other_records if not r["success"]
    ]

    # Only bill bytes for calls that actually ran to completion -- a query
    # that fails (MAX_BYTES_BILLED, timeout, bad SQL) was never billed by
    # BigQuery, so charging its dry-run estimate against bytes_consumed would
    # deplete the turn's budget for work that cost nothing.
    billed_bytes = 0 if cost_cap_exceeded else sum(
        est for est, r in zip(estimates, bq_records) if r["success"]
    )
    return {
        "messages": bq_messages + other_messages,
        "iteration_count": state["iteration_count"] + 1,
        "bytes_consumed": state["bytes_consumed"] + billed_bytes,
        "cost_cap_exceeded": cost_cap_exceeded,
        "estimated_cost": format_cost(batch_bytes) if cost_cap_exceeded else state["estimated_cost"],
        "tool_calls": bq_records + other_records,
        "errors": errors,
        "answer_submitted": False,
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
    for claim in all_claimed:
        if not claim_matches_pool(claim, pool):
            return False, f"{claim} does not match any tool result this turn."
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
        chart_url=state["chart_url"],
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
# route_entry/execute_approved land in item 11 -- graph starts at agent.

g = StateGraph(AgentState)
g.add_node("agent",        agent_node)
g.add_node("call_tool",    call_tool_node)
g.add_node("check_length", check_length_node)
g.add_node("verify",       verify_node)
g.add_node("finalize",     finalize_node)

g.add_edge(START, "agent")

# A plain list of names is sugar for the identity path_map ({name: name for
# name in [...]}) -- still needed for get_graph() to draw the real edges.
g.add_conditional_edges("agent", route_after_agent,
    ["call_tool", "check_length"])
g.add_conditional_edges("call_tool", route_after_call_tool,
    ["finalize", "agent", "check_length"])
g.add_conditional_edges("check_length", route_after_check_length,
    ["verify", "agent", "finalize"])
g.add_conditional_edges("verify", route_after_verify,
    ["finalize", "agent"])

g.add_edge("finalize", END)
graph = g.compile()
