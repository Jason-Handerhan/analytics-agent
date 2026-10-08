"""Entry/exit helpers -- called by the gateway, never by the graph itself.
Translates an HTTP request into graph input and the graph's final state back
into the gateway's wire format, plus the conversation-history read/write
functions either side of a turn.
"""
import json
from datetime import datetime, timezone

from langchain_core.messages import (
    BaseMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict,
)

from app.config import HISTORY_TURN_COUNT, NUMERIC_SOURCE_TOOLS
from app.gateway.models import AgentResponse
from app.orchestrator.shared_helpers import label_chartable_result
from app.orchestrator.state import AgentState, PendingApproval, ToolCallRecord


# --- Turn setup (gateway request -> graph input) ---------------------------

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
    history_messages: list[BaseMessage],
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
        history_messages=history_messages,
        messages=[build_human_message(question, filter_context, active_page)],
        tool_calls=[],
        iteration_count=0,
        verification_retry_count=0,
        length_retry_count=0,
        verified=False,
        bytes_consumed=0,
        bytes_consumed_baseline=0,
        prompt_tokens=0,
        completion_tokens=0,
        llm_calls=0,
        errors=[],
        cancelled=False,
        needs_approval=False,
        pending_queries=[],
        largest_pending_query=None,
        paused_at=None,
        approved_batch=False,
        approval_decision=None,
        deferred_dax=[],
        estimated_cost=None,
        cost_cap_exceeded=False,
        iteration_cap_hit=False,
        answer_submitted=False,
        answer_markdown="",
        clarifying_question="",
        chart_urls=[],
        sources=[],
        all_prose_numeric_claims=[],
        suggested_follow_ups=[],
    )


# --- Turn teardown (graph output -> gateway wire format) -------------------

def build_agent_response(state: AgentState) -> AgentResponse:
    """Converts the graph's final state into the gateway's wire format."""
    return AgentResponse(
        answer_markdown=state["answer_markdown"],
        sources=state["sources"],
        needs_approval=state["needs_approval"],
        chart_urls=state["chart_urls"],
        suggested_follow_ups=state["suggested_follow_ups"],
        iteration_cap_hit=state["iteration_cap_hit"],
        pending_query=state["largest_pending_query"],
        estimated_cost=state["estimated_cost"],
        cost_cap_exceeded=state["cost_cap_exceeded"],
    )


# --- Shared tool-result redaction -------------------------------------------

STALE_RESULT_MESSAGE = ("Result removed -- this tool call is from a prior turn, not this one. "
                         "Re-run it if you need this data now.")
PAUSE_REDACTED_MESSAGE = ("Result redacted during this turn's approval pause. "
                          "Re-run it if you still need this data.")


def _redact_tool_results(messages: list[BaseMessage], notice: str) -> list[BaseMessage]:
    """Replaces every successful non-submit_answer ToolMessage's content with
    `notice`, keeping name/tool_call_id/status intact."""
    redacted = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.name != "submit_answer" and msg.status != "error":
            msg = ToolMessage(content=notice, name=msg.name,
                              tool_call_id=msg.tool_call_id, status=msg.status)
        redacted.append(msg)
    return redacted


# --- Conversation history (sessions.history_messages) ---------------------

def build_updated_history(history_messages: list[dict], state: AgentState) -> list[dict]:
    """Returns the updated history_messages list -- FIFO-trimmed to
    HISTORY_TURN_COUNT, with this turn's messages appended. Every
    successful tool result except submit_answer's is redacted first"""
    redacted = _redact_tool_results(state["messages"], STALE_RESULT_MESSAGE)
    new_entry = {"messages": messages_to_dict(redacted), "timestamp": datetime.now(timezone.utc)}
    return (history_messages + [new_entry])[-HISTORY_TURN_COUNT:]


def build_history_messages(history_messages: list[dict]) -> list[BaseMessage]:
    """Reconstructs prior turns' message sequence -- real tool structure, not prose."""
    result: list[BaseMessage] = []
    for turn in history_messages:
        result.extend(messages_from_dict(turn["messages"]))
    return result


# --- Approval pause (PendingApproval / live_turns) --------------------------

def build_pending_approval(state: AgentState) -> PendingApproval:
    """The handoff object POST /ask/respond resumes from -- carries only what's
    needed to resume reasoning or bounds total turn consumption; everything
    else resets by construction. Tool results are redacted the same way as
    cross-turn history (rebuild_paused_messages below restores numeric ones
    from their ToolCallRecord on resume)."""
    return PendingApproval(
        conversation_id=state["conversation_id"],
        question=state["question"],
        filter_context=state["filter_context"],
        active_page=state["active_page"],
        messages=messages_to_dict(_redact_tool_results(state["messages"], PAUSE_REDACTED_MESSAGE)),
        pending_queries=state["pending_queries"],
        deferred_dax=state["deferred_dax"],
        tool_calls=state["tool_calls"],
        iteration_count=state["iteration_count"],
        bytes_consumed=state["bytes_consumed"],
        estimated_cost=state["estimated_cost"],
        paused_at=state["paused_at"],
    )


def rebuild_paused_messages(messages: list[dict], tool_calls: list[ToolCallRecord]) -> list[BaseMessage]:
    """Restores a numeric tool's redacted result from its ToolCallRecord -- the
    exact labeled content the model originally saw, same ref_id included.
    Everything else (the generic redaction, submit_answer, errors) passes
    through unchanged."""
    tc_by_id = {tc["id"]: tc for tc in tool_calls}
    rebuilt = []
    for msg in messages_from_dict(messages):
        tc = tc_by_id.get(msg.tool_call_id) if isinstance(msg, ToolMessage) else None
        if tc and tc["name"] in NUMERIC_SOURCE_TOOLS and msg.status != "error":
            tool_message = ToolMessage(content=json.dumps(tc["result"], ensure_ascii=False),
                                       name=tc["name"], tool_call_id=tc["id"], status="success")
            rebuilt.append(label_chartable_result(tool_message, tc["ref_id"]))
        else:
            rebuilt.append(msg)
    return rebuilt
