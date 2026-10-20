"""Entry/exit helpers -- called by the gateway, never by the graph itself.
Translates an HTTP request into graph input and the graph's final state back
into the gateway's wire format, plus the conversation-history read/write
functions either side of a turn.
"""
import json
from datetime import datetime, timezone

import mistune
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict,
)

from app.config import (
    ANSWER_BODY_FONT_SIZE,
    ANSWER_CODE_BLOCK_BG,
    ANSWER_CODE_BLOCK_TEXT,
    ANSWER_CODE_FONT_FAMILY,
    ANSWER_CODE_FONT_SIZE,
    ANSWER_FONT_FAMILY,
    ANSWER_HEADING_FONT_SIZE,
    ANSWER_INLINE_CODE_BG,
    ANSWER_INLINE_CODE_BORDER,
    ANSWER_INLINE_CODE_TEXT,
    ANSWER_SUBHEADING_FONT_SIZE,
    ANSWER_TABLE_CELL_BORDER,
    ANSWER_TABLE_CELL_TEXT,
    ANSWER_TABLE_FONT_SIZE,
    ANSWER_TABLE_HEADER_BG,
    ANSWER_TABLE_HEADER_BORDER,
    ANSWER_TABLE_HEADER_TEXT,
    HISTORY_TURN_COUNT,
    NUMERIC_SOURCE_TOOLS,
)
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

# <style> blocks aren't reliably honored by the Power Apps HTML text
# control, so every element that needs styling gets it inline instead.
class _AnswerRenderer(mistune.HTMLRenderer):
    def paragraph(self, text: str) -> str:
        return (f'<p style="font-family: {ANSWER_FONT_FAMILY}; '
                f'font-size: {ANSWER_BODY_FONT_SIZE};">{text}</p>\n')

    def heading(self, text: str, level: int, **attrs) -> str:
        tag = f"h{level}"
        size = ANSWER_HEADING_FONT_SIZE if level <= 2 else ANSWER_SUBHEADING_FONT_SIZE
        style = f"font-family: {ANSWER_FONT_FAMILY}; font-size: {size};"
        heading_id = attrs.get("id")
        id_attr = f' id="{mistune.util.escape(heading_id)}"' if heading_id else ""
        return f'<{tag}{id_attr} style="{style}">{text}</{tag}>\n'

    def list_item(self, text: str) -> str:
        return (f'<li style="font-family: {ANSWER_FONT_FAMILY}; '
                f'font-size: {ANSWER_BODY_FONT_SIZE};">{text}</li>\n')

    def image(self, text: str, url: str, title: str | None = None) -> str:
        src = self.safe_url(url)
        alt = mistune.util.striptags(text)
        return f'<img src="{src}" alt="{alt}" style="max-width: 100%;" />'

    def block_code(self, code: str, info: str | None = None) -> str:
        escaped = mistune.util.escape(code)
        return (
            '<pre style="display: inline-block; max-width: 100%; overflow-x: auto; '
            f'font-family: {ANSWER_CODE_FONT_FAMILY}; font-size: {ANSWER_CODE_FONT_SIZE}; '
            f'white-space: pre-wrap; background-color: {ANSWER_CODE_BLOCK_BG}; '
            f'color: {ANSWER_CODE_BLOCK_TEXT}; padding: 12px 16px; border-radius: 6px; '
            'line-height: 1.5;">'
            f'<code>{escaped}</code></pre>\n'
        )

    def codespan(self, text: str) -> str:
        escaped = mistune.util.escape(text)
        return (
            f'<code style="font-family: {ANSWER_CODE_FONT_FAMILY}; font-size: {ANSWER_CODE_FONT_SIZE}; '
            f'background-color: {ANSWER_INLINE_CODE_BG}; border: 1px solid {ANSWER_INLINE_CODE_BORDER}; '
            f'color: {ANSWER_INLINE_CODE_TEXT}; padding: 2px 5px; '
            f'border-radius: 3px;">{escaped}</code>'
        )


def _render_table(renderer, text: str) -> str:
    return f'<table style="border-collapse: collapse; max-width: 100%;">\n{text}</table>\n'


def _render_table_head(renderer, text: str) -> str:
    return f'<thead>\n<tr>\n{text}</tr>\n</thead>\n'


def _render_table_body(renderer, text: str) -> str:
    return f'<tbody>\n{text}</tbody>\n'


def _render_table_row(renderer, text: str) -> str:
    return f'<tr>\n{text}</tr>\n'


def _render_table_cell(renderer, text: str, align: str | None = None, head: bool = False) -> str:
    tag = "th" if head else "td"
    font = f"font-family: {ANSWER_FONT_FAMILY}; font-size: {ANSWER_TABLE_FONT_SIZE}; "
    if head:
        style = (font + f"border: 1px solid {ANSWER_TABLE_HEADER_BORDER}; padding: 6px 10px; "
                  f"color: {ANSWER_TABLE_HEADER_TEXT}; line-height: 1.3; "
                  f"background-color: {ANSWER_TABLE_HEADER_BG}; font-weight: bold;")
    else:
        style = (font + f"border: 1px solid {ANSWER_TABLE_CELL_BORDER}; padding: 4px 10px; "
                  f"color: {ANSWER_TABLE_CELL_TEXT}; line-height: 1.3;")
    if align:
        style += f" text-align: {align};"
    return f'  <{tag} style="{style}">{text}</{tag}>\n'


# The table plugin registers its own default render functions onto the
# renderer by name -- registering these again, after create_markdown(),
# overrides them with the styled versions above. Same plugin used by
# orchestrator.py's own verification AST parser, for the same markdown shape.
_answer_markdown = mistune.create_markdown(renderer=_AnswerRenderer(), plugins=["table"])
_answer_markdown.renderer.register("table", _render_table)
_answer_markdown.renderer.register("table_head", _render_table_head)
_answer_markdown.renderer.register("table_body", _render_table_body)
_answer_markdown.renderer.register("table_row", _render_table_row)
_answer_markdown.renderer.register("table_cell", _render_table_cell)


def render_answer_html(answer_markdown: str) -> str:
    """Renders answer_markdown to HTML for the Power Apps HTML text control --
    inline-styled throughout, since the control doesn't reliably honor a
    <style> block."""
    return _answer_markdown(answer_markdown)


def build_agent_response(state: AgentState) -> AgentResponse:
    """Converts the graph's final state into the gateway's wire format."""
    return AgentResponse(
        answer_markdown=state["answer_markdown"],
        answer_html=render_answer_html(state["answer_markdown"]),
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

GEMINI_THOUGHT_SIGNATURES_KEY = "__gemini_function_call_thought_signatures__"
_GEMINI_SIGNATURES_STORAGE_KEY = "gemini_function_call_thought_signatures"


def _clean_content_block(block):
    """Drops a thinking block entirely, or removes its signature metadata
    and keeps the rest."""
    if not isinstance(block, dict):
        return block
    if block.get("type") == "thinking":
        return None
    if "extras" in block:
        block = {k: v for k, v in block.items() if k != "extras"}
    return block


def _strip_thinking_blocks(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Drops thinking content blocks, and any leftover signature metadata
    on the blocks that remain, from every AIMessage."""
    stripped = []
    for msg in messages:
        if isinstance(msg, AIMessage) and isinstance(msg.content, list):
            content = [_clean_content_block(b) for b in msg.content if _clean_content_block(b) is not None]
            msg = msg.model_copy(update={"content": content})
        stripped.append(msg)
    return stripped


def _drop_gemini_signatures(messages: list[dict]) -> list[dict]:
    """Removes Gemini's thought-signature key from additional_kwargs."""
    for msg in messages:
        msg.get("data", {}).get("additional_kwargs", {}).pop(GEMINI_THOUGHT_SIGNATURES_KEY, None)
    return messages


def _rename_gemini_signatures_for_storage(messages: list[dict]) -> list[dict]:
    """Renames Gemini's thought-signature key to a Firestore-safe name."""
    for msg in messages:
        kwargs = msg.get("data", {}).get("additional_kwargs", {})
        if GEMINI_THOUGHT_SIGNATURES_KEY in kwargs:
            kwargs[_GEMINI_SIGNATURES_STORAGE_KEY] = kwargs.pop(GEMINI_THOUGHT_SIGNATURES_KEY)
    return messages


def _restore_gemini_signatures(messages: list[dict]) -> list[dict]:
    """Renames the stored key back to Gemini's original thought-signature key."""
    for msg in messages:
        kwargs = msg.get("data", {}).get("additional_kwargs", {})
        if _GEMINI_SIGNATURES_STORAGE_KEY in kwargs:
            kwargs[GEMINI_THOUGHT_SIGNATURES_KEY] = kwargs.pop(_GEMINI_SIGNATURES_STORAGE_KEY)
    return messages


def build_updated_history(history_messages: list[dict], state: AgentState) -> list[dict]:
    """Returns the updated history_messages list -- FIFO-trimmed to
    HISTORY_TURN_COUNT, with this turn's messages appended."""
    redacted = _strip_thinking_blocks(_redact_tool_results(state["messages"], STALE_RESULT_MESSAGE))
    new_entry = {"messages": _drop_gemini_signatures(messages_to_dict(redacted)),
                 "timestamp": datetime.now(timezone.utc)}
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
        messages=_rename_gemini_signatures_for_storage(
            messages_to_dict(_redact_tool_results(state["messages"], PAUSE_REDACTED_MESSAGE))),
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
    for msg in messages_from_dict(_restore_gemini_signatures(messages)):
        tc = tc_by_id.get(msg.tool_call_id) if isinstance(msg, ToolMessage) else None
        if tc and tc["name"] in NUMERIC_SOURCE_TOOLS and msg.status != "error":
            tool_message = ToolMessage(content=json.dumps(tc["result"], ensure_ascii=False),
                                       name=tc["name"], tool_call_id=tc["id"], status="success")
            rebuilt.append(label_chartable_result(tool_message, tc["ref_id"]))
        else:
            rebuilt.append(msg)
    return rebuilt
