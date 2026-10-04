"""Entry/exit helpers -- called by the gateway, never by the graph itself.
Translates an HTTP request into graph input and the graph's final state back
into the gateway's wire format, plus the conversation-history read/write
functions either side of a turn.
"""
from datetime import datetime, timezone

from langchain_core.messages import (
    BaseMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict,
)

from app.config import HISTORY_TURN_COUNT
from app.gateway.models import AgentResponse
from app.orchestrator.state import AgentState


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


# --- Conversation history (sessions.history_messages) ---------------------

STALE_RESULT_MESSAGE = ("Result removed -- this tool call is from a prior turn, not this one. "
                         "Re-run it if you need this data now.")


def build_updated_history(history_messages: list[dict], state: AgentState) -> list[dict]:
    """Returns the updated history_messages list -- FIFO-trimmed to
    HISTORY_TURN_COUNT, with this turn's messages appended. Every
    successful tool result except submit_answer's is redacted first --
    none of these tools are expensive to re-run, so the model can't
    mistake a stale result for current data."""
    redacted = []
    for msg in state["messages"]:
        if isinstance(msg, ToolMessage) and msg.name != "submit_answer" and msg.status != "error":
            msg = ToolMessage(content=STALE_RESULT_MESSAGE, name=msg.name,
                               tool_call_id=msg.tool_call_id, status=msg.status)
        redacted.append(msg)
    new_entry = {"messages": messages_to_dict(redacted), "timestamp": datetime.now(timezone.utc)}
    return (history_messages + [new_entry])[-HISTORY_TURN_COUNT:]


def build_history_messages(history_messages: list[dict]) -> list[BaseMessage]:
    """Reconstructs prior turns' message sequence -- real tool structure, not prose."""
    result: list[BaseMessage] = []
    for turn in history_messages:
        result.extend(messages_from_dict(turn["messages"]))
    return result
