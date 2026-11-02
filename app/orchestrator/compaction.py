"""Compaction: shrinks a conversation's stored history before the next question is answered.

A turn's first message carries additional_kwargs["turn_start"] (when the turn started); a stored
summary's first message carries additional_kwargs["summary"] and ["covers_to"].
"""
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, messages_to_dict
from pydantic import BaseModel, Field

from app.config import COMPACTION_KEEP_TURNS, COMPACTION_MODEL
from app.orchestrator.conversation_memory.reader import fetch_compaction_rows
from app.orchestrator.conversation_memory.writer import build_summary_row
from app.orchestrator.state import StoredTurn

logger = logging.getLogger(__name__)


# --- Helpers (history and ledger) -----------------------------------------------------------

def _first_message_kwargs(stored_turn: StoredTurn) -> dict:
    return stored_turn.messages[0]["data"]["additional_kwargs"]


def _is_summary(stored_turn: StoredTurn) -> bool:
    return bool(_first_message_kwargs(stored_turn).get("summary"))


def measure_history_size(stored_history: list[StoredTurn]) -> tuple[int, int]:
    """Returns (estimated_tokens, stored_bytes). estimated_tokens adds up the text length of every
    message's content and tool calls and divides by 4 -- a rough prompt size, not a provider token
    count. stored_bytes is the JSON size of the whole stored history, a proxy for its Firestore size."""
    characters = 0
    for stored_turn in stored_history:
        for message in stored_turn.messages:
            data = message["data"]
            characters += len(json.dumps(data["content"])) + len(json.dumps(data.get("tool_calls", [])))
    return characters // 4, len(json.dumps([asdict(turn) for turn in stored_history], default=str))


def split_history_for_compaction(
    stored_history: list[StoredTurn], keep_turns: int
) -> tuple[StoredTurn | None, list[StoredTurn], list[StoredTurn]]:
    """Returns (previous_summary or None, turns_to_summarize, turns_to_keep).

    If the history starts with a stored summary, that record is set aside -- it isn't a turn.
    The newest keep_turns turns are kept and every older turn is summarized."""
    previous_summary = None
    turns = stored_history
    if stored_history and _is_summary(stored_history[0]):
        previous_summary = stored_history[0]
        turns = stored_history[1:]

    cutoff = max(len(turns) - keep_turns, 0)
    turns_to_summarize = turns[:cutoff]
    turns_to_keep = turns[cutoff:]
    return previous_summary, turns_to_summarize, turns_to_keep


def find_compaction_time_window(
    previous_summary: StoredTurn | None, turns_to_keep: list[StoredTurn]
) -> tuple[datetime | None, datetime]:
    """Returns (after, before): the ledger rows being summarized were answered after the previous
    summary stopped (after is None without one) and before the oldest kept turn started.
    Takes the outputs of split_history_for_compaction."""
    first_kept_turn_start = _first_message_kwargs(turns_to_keep[0])["turn_start"]
    before = datetime.fromisoformat(first_kept_turn_start)

    after = None
    if previous_summary is not None:
        previous_summary_covers_to = _first_message_kwargs(previous_summary)["covers_to"]
        after = datetime.fromisoformat(previous_summary_covers_to)

    return after, before


def find_summarized_turn_range(previous_summary: StoredTurn | None, rows: list[dict]) -> tuple[int, int]:
    """Returns (first_turn, last_turn): the numbers in the conversation_findings ledger of the rows
    being summarized (the output of fetch_compaction_rows). They start right after the previous
    summary's last_turn (at 1 without one)."""
    first_turn = 1
    if previous_summary is not None:
        first_turn = _first_message_kwargs(previous_summary)["last_turn"] + 1

    last_turn = first_turn + len(rows) - 1
    return first_turn, last_turn


# --- Summarizer (the agent call) ------------------------------------------------------------

class TurnRangeSummary(BaseModel):
    """What the summarizing model returns for a range of verified turns."""
    summary_markdown: str = Field(
        description="A short synthesis of what these turns found, grouped by subject rather than "
                    "by turn. Combine questions that revisited the same subject. Refer to a chart "
                    "by its label (the text inside the brackets of its image link), never its URL. "
                    "Describe findings in words; state a figure only when it is the headline of a "
                    "finding, and only if it is in the verified numbers list. Never compute a new "
                    "number.")
    title: str = Field(
        description="A short label for what these turns were about, 3-8 words, no numbers.")
    topics: list[str] = Field(
        description="Short noun phrases for what these turns covered, no numbers.")


SUMMARIZER_INSTRUCTIONS = """You summarize a stretch of an analytics conversation so an assistant can \
pick it back up after the original turns are removed from its context.

You receive each verified turn's question and the answer the user was given, then one list of \
verified numbers. Write the summary from these turns only.

Write one summary of what was learned, grouped by subject -- not a turn-by-turn log. Combine \
questions that revisited the same subject into a single finding, and state each finding once. \
Mention what was asked only where it is needed to make a finding clear. Do not refer to turns by \
number. Mention a chart only when it supports a finding. Keep it short: a few brief paragraphs or \
bullets.

Numbers: describe findings in words. State a figure only when it is the headline of a finding, and \
only if it appears in the verified numbers list. Never compute a new number.

Refer to a chart by its label, never its URL. Keep the title and topics free of numbers."""


def format_compaction_rows_for_summarizer(rows: list[dict]) -> str:
    """What the summarizing model reads: one plain-text block per ledger row (title, topics,
    entities, question, answer), then one list of every verified number across the rows."""
    blocks = []
    verified_numbers = []
    for row in rows:
        lines = [f"[{row['answered_at']:%Y-%m-%d %H:%M}]" + (f" {row['title']}" if row["title"] else "")]
        if row["topics"]:
            lines.append(f"Topics: {', '.join(row['topics'])}")
        if row["entities"]:
            lines.append(f"Entities: {', '.join(row['entities'])}")
        lines += [f"Question: {row['question']}", f"Answer: {row['answer_markdown']}"]
        blocks.append("\n".join(lines))

        for verified_number in row["verified_numbers"]:
            if verified_number not in verified_numbers:
                verified_numbers.append(verified_number)

    blocks.append(f"Verified numbers (the only numbers you may state): {verified_numbers}")
    return "\n\n".join(blocks)


async def summarize_turn_range(formatted_rows: str) -> tuple[TurnRangeSummary, int, int]:
    """Asks COMPACTION_MODEL to summarize the text from format_compaction_rows_for_summarizer.
    Returns (summary, prompt_tokens, completion_tokens)."""
    model = ChatAnthropic(model=COMPACTION_MODEL).bind_tools(
        [TurnRangeSummary], tool_choice="TurnRangeSummary", strict=True)
    response = await model.ainvoke([
        SystemMessage(content=SUMMARIZER_INSTRUCTIONS),
        HumanMessage(content=formatted_rows),
    ])
    summary = TurnRangeSummary.model_validate(response.tool_calls[0]["args"])
    usage = response.usage_metadata
    return summary, usage["input_tokens"], usage["output_tokens"]


# --- Pipeline -------------------------------------------------------------------------------

@dataclass
class CompactionResult:
    """What the gateway gets back: the history to use from here on, the summary row to write (if
    one was made), and the fields the telemetry row records."""
    stored_history: list[StoredTurn]
    summary_row: dict | None = None
    compacted: bool = False
    failed: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0


def build_stored_summary(
    summary: TurnRangeSummary, covers_from: datetime, covers_to: datetime, first_turn: int, last_turn: int
) -> StoredTurn:
    """Wraps the model's summary as the stored record that goes first in the history, in place of
    the turns it summarizes. first_turn and last_turn are the summarized turns' numbers in the
    conversation_findings ledger. covers_to and last_turn are saved on it so the next compaction
    knows where it stopped."""
    label = (
        f"Summary of verified turns {first_turn}-{last_turn} of this conversation (answered "
        f"{covers_from:%Y-%m-%d %H:%M} to {covers_to:%Y-%m-%d %H:%M} UTC). It is a recap, not a live "
        "result: figures in it cannot be cited. For the exact turns and figures, call "
        f'get_conversation_findings with view="turns", start_turn={first_turn}, end_turn={last_turn}.')
    heading = f"**{summary.title}**\n\n" if summary.title else ""
    message = HumanMessage(
        content=f"{label}\n\n{heading}{summary.summary_markdown}",
        additional_kwargs={"summary": True, "covers_to": covers_to.isoformat(), "last_turn": last_turn},
    )
    return StoredTurn(messages=messages_to_dict([message]), timestamp=datetime.now(timezone.utc))


async def compact_history_pipeline(
    conversation_id: str, user_id: str, stored_history: list[StoredTurn]
) -> CompactionResult:
    """Replaces the turns older than the newest COMPACTION_KEEP_TURNS with one summary built from
    their turn rows in conversation_findings. The gateway decides whether the history needs compacting (it is over a
    size threshold) and saves the returned history and summary row. Any error is
    logged and the history comes back unchanged. Compaction can never fail a turn."""
    previous_summary, _, turns_to_keep = split_history_for_compaction(stored_history, COMPACTION_KEEP_TURNS)

    try:
        after, before = find_compaction_time_window(previous_summary, turns_to_keep)
        rows = await fetch_compaction_rows(conversation_id, after, before)

        if not rows:
            raise ValueError("No ledger rows to summarize")

        summary, prompt_tokens, completion_tokens = await summarize_turn_range(
            format_compaction_rows_for_summarizer(rows))

        first_turn, last_turn = find_summarized_turn_range(previous_summary, rows)
        covers_from = rows[0]["answered_at"]
        covers_to = rows[-1]["answered_at"]

        stored_summary = build_stored_summary(summary, covers_from, covers_to, first_turn, last_turn)
        summary_row = build_summary_row(
            conversation_id, user_id, covers_from, covers_to, first_turn, last_turn,
            summary.title, summary.summary_markdown, summary.topics, datetime.now(timezone.utc))
    except Exception:
        logger.exception("Compaction failed for conversation %s; history left as it was", conversation_id)
        return CompactionResult(stored_history=stored_history, failed=True)

    return CompactionResult(
        stored_history=[stored_summary, *turns_to_keep],
        summary_row=summary_row,
        compacted=True,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
