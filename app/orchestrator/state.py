"""The shared data shapes: AgentState (the graph's state) and the types around it,
including the shapes stored in Firestore (StoredTurn, PendingApproval).

Kept separate from orchestrator.py so the gateway, compaction and tests can import
these types without importing orchestrator.py and its heavy dependencies
(ChatAnthropic, StateGraph, MultiServerMCPClient, ...).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

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
    model: str
    model_provider: str
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


@dataclass
class StoredTurn:
    """One element of sessions.history_messages: a turn's messages (messages_to_dict), when it was stored,
    and the numbers it verified. A stored summary has the same shape but is not a turn and has none."""
    messages: list[dict]
    timestamp: datetime
    verified_numbers: list[float] = field(default_factory=list)


class Task(TypedDict):
    text: str
    status: Literal["todo", "in_progress", "done"]


NoteKind = Literal["preference", "definition", "goal", "constraint"]


class Note(TypedDict):
    id: int
    kind: NoteKind
    text: str


class TaskPlan(TypedDict, total=False):
    """The conversation's saved plan; empty once cleared. scope is absent until one is set."""
    scope: str
    tasks: list[Task]


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
    model: str
    model_provider: str
    turn_started_at: datetime
    filter_context: list[dict]
    active_page: str | None
    image_base64: str | None
    history_messages: list[BaseMessage]  # conversation history
    notes: list[Note]   # standing notes for this conversation, read from the sessions document
    task_plan: TaskPlan   # this conversation's task plan, read from the sessions document
    history_verified_numbers: list[float]   # verified_numbers of the stored turns, read with the history

    # Accumulated during the tool loop
    messages: Annotated[list[BaseMessage], add_messages]
    tool_calls: Annotated[list[ToolCallRecord], append_list]
    iteration_count: int

    # Separate retry budgets
    verification_retry_count: int
    length_retry_count: int

    verified: bool
    verified_numbers: list[float]   # claims plus table values when this turn verified, else empty

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

    # Compaction -- set by the gateway before the graph runs, recorded by finalize
    compacted: bool
    compaction_failed: bool
    compaction_prompt_tokens: int
    compaction_completion_tokens: int

    # Building toward AgentResponse
    answer_markdown: str
    clarifying_question: str  # "" unless the turn ended on a clarifying question
    chart_urls: Annotated[list[str], append_list]
    sources: list[str]
    all_prose_numeric_claims: list[float]  # llm provided numeric claims in prose
    suggested_follow_ups: list[str]
    title: str  # tags from submit_answer; "" / [] when the model left them out
    topics: list[str]
    entities: list[str]
