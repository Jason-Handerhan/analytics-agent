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

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.config import (
    ABSOLUTE_CAP,
    BIGQUERY_PRICE_PER_TIB,
    MAX_ANSWER_CHARS,
    MAX_ITERATIONS,
    MAX_LENGTH_RETRIES,
    MAX_VERIFY_RETRIES,
    MCP_SERVER_HEADERS,
    MCP_SERVER_NAME,
    MCP_SERVER_URL,
    MODEL,
)
from app.orchestrator.context import get_static_context
from app.orchestrator.tools import dry_run, run_bigquery_sql
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
    ALL_TOOLS = {**MCP_TOOLS, "run_bigquery_sql": run_bigquery_sql}
    SYSTEM_MESSAGE = SystemMessage(content=get_static_context())


def check_answer_length(answer_markdown: str) -> bool:
    return len(answer_markdown) <= MAX_ANSWER_CHARS


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

    # Building toward AgentResponse
    answer_markdown: str
    chart_url: str | None
    sources: list[str]


# --- Helper functions ----------------------------------------------------

# Past-tense badges -- distinct from FRIENDLY_TOOL_NAMES (.claude/rules/gateway.md)
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


def exceeds_absolute_cap(bytes_consumed: int, batch_bytes: int, cap: int = ABSOLUTE_CAP) -> bool:
    """True if dispatching this batch would push the turn's cumulative bytes at/over cap."""
    return bytes_consumed + batch_bytes >= cap


def format_cost(num_bytes: int) -> str:
    """Byte count as a display dollar string -- display only, not the decision."""
    return f"${(num_bytes / 1024**4) * BIGQUERY_PRICE_PER_TIB:.2f}"


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


# --- Node functions ------------------------------------------------------

async def agent_node(state: AgentState) -> dict:
    """Calls the LLM with tools bound. Emits tool calls, or writes answer_markdown."""
    model = ChatAnthropic(model=MODEL).bind_tools(list(ALL_TOOLS.values()), strict=True)
    response = await model.ainvoke([SYSTEM_MESSAGE, *state["history_messages"], *state["messages"]])
    update = {"messages": [response], "llm_calls": state["llm_calls"] + 1}
    if not response.tool_calls:
        update["answer_markdown"] = response.content
    return update


async def call_tool_node(state: AgentState) -> dict:
    """Dispatches tool calls under the iteration/cost-cap guardrails;
    records a ToolCallRecord and, for any declined/failed call, a TurnError."""
    tool_calls = state["messages"][-1].tool_calls

    if state["iteration_count"] >= MAX_ITERATIONS:
        return iteration_cap_update(tool_calls, state["iteration_count"])

    bq_calls = [tc for tc in tool_calls if tc["name"] == "run_bigquery_sql"]
    other_calls = [tc for tc in tool_calls if tc["name"] != "run_bigquery_sql"]

    batch_bytes = 0
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
    other_records = [
        build_tool_call_record(tc, msg, other_started_at, other_completed_at)
        for tc, msg in zip(other_calls, other_messages)
    ]

    bq_error_type = "cost_cap_exceeded" if cost_cap_exceeded else "tool_error"
    errors = [
        build_turn_error("call_tool", bq_error_type, r["error"], r["completed_at"], r["id"])
        for r in bq_records if not r["success"]
    ] + [
        build_turn_error("call_tool", "tool_error", r["error"], r["completed_at"], r["id"])
        for r in other_records if not r["success"]
    ]

    billed_bytes = 0 if cost_cap_exceeded else batch_bytes
    return {
        "messages": bq_messages + other_messages,
        "iteration_count": state["iteration_count"] + 1,
        "bytes_consumed": state["bytes_consumed"] + billed_bytes,
        "cost_cap_exceeded": cost_cap_exceeded,
        "estimated_cost": format_cost(batch_bytes) if cost_cap_exceeded else state["estimated_cost"],
        "tool_calls": bq_records + other_records,
        "errors": errors,
    }


async def check_length_node(state: AgentState) -> dict:
    """Checks answer_markdown length; on failure, requests a shorter answer."""
    if check_answer_length(state["answer_markdown"]):
        return {}
    return {
        "length_retry_count": state["length_retry_count"] + 1,
        "messages": [HumanMessage(content=(
            "Your answer is too long to display. Summarize the key findings "
            "concisely, or generate a chart instead of listing rows."))],
    }


async def verify_node(state: AgentState) -> dict:
    """Placeholder -- always verifies. Real checks land later."""
    return {"verified": True}


async def finalize_node(state: AgentState) -> dict:
    """Tallies tokens/sources, builds and writes the telemetry row."""
    prompt_tokens, completion_tokens = sum_token_usage(state["messages"])
    sources = build_sources(state["tool_calls"])
    row = build_telemetry_row(
        conversation_id=state["conversation_id"],
        user_id=state["user_id"],
        question=state["question"],
        answer_markdown=state["answer_markdown"],
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
    )
    await write_telemetry_row(row)
    return {
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
    return "agent"   # also cost_cap_exceeded and iteration_cap_hit


def route_after_check_length(state: AgentState) -> str:
    if check_answer_length(state["answer_markdown"]):
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
    ["finalize", "agent"])
g.add_conditional_edges("check_length", route_after_check_length,
    ["verify", "agent", "finalize"])
g.add_conditional_edges("verify", route_after_verify,
    ["finalize", "agent"])

g.add_edge("finalize", END)
graph = g.compile()
