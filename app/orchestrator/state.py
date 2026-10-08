"""AgentState and its supporting TypedDicts/reducers -- the graph's state
shape, shared by orchestrator.py (the graph) and app/gateway/entry_exit.py
(gateway-facing helpers). Deliberately free of heavier LangGraph/LangChain-
model imports (ChatAnthropic, StateGraph, MultiServerMCPClient, ...), so
either of those modules can be imported without pulling in the other's
dependencies.
"""
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ToolCallRecord(TypedDict):
    id: str
    name: str
    args: dict
    query_text: str | None  # args["query"] or args["dax"] -- SQL/DAX only, else None
    result: Any  # shape depends on the tool
    success: bool
    error: str | None
    ref_id: str | None  # set only for a chartable tool's successful result
    bytes_billed: int | None  # from the tool's artifact; None for non-BigQuery or failed calls
    started_at: datetime
    completed_at: datetime


class PendingApproval(TypedDict):
    conversation_id: str
    question: str
    filter_context: list[dict]
    active_page: str | None
    messages: list[dict]  # messages_to_dict(state["messages"]) -- this turn so far
    pending_queries: list[dict]
    deferred_dax: list[dict]
    tool_calls: list[ToolCallRecord]
    iteration_count: int
    bytes_consumed: int
    estimated_cost: str
    paused_at: datetime


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
    bytes_consumed_baseline: int  # bytes_consumed's value at this phase's start --
                                  # 0 for a fresh turn, seeded from PendingApproval on resume
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int

    errors: Annotated[list[TurnError], append_list]
    cancelled: bool

    # Guardrail outcomes
    needs_approval: bool
    approved_batch: bool  # True on a resumed turn's first batch; skips the pause gate once
    approval_decision: str | None  # "approved" | "rejected"; None until the user decides
    pending_queries: list[dict]
    largest_pending_query: str | None  # the query the approval card shows: largest estimated_bytes
    paused_at: datetime | None  # set by call_tool_node's pause branch; None until a pause happens
    deferred_dax: list[dict]
    estimated_cost: str | None
    cost_cap_exceeded: bool
    iteration_cap_hit: bool
    answer_submitted: bool  # True only when submit_answer was the sole tool

    # Building toward AgentResponse
    answer_markdown: str
    clarifying_question: str  # "" unless the turn ended on a clarifying question
    chart_urls: Annotated[list[str], append_list]
    sources: list[str]
    all_prose_numeric_claims: list[float]  # llm provided numeric claims in prose
    suggested_follow_ups: list[str]
