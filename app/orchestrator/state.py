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
