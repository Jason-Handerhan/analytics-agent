"""Layer 1 test for SubmitAnswerArgs validators -- no credentials needed (docs/testing.md)."""
import pytest
from pydantic import ValidationError

from app.orchestrator.tools import SubmitAnswerArgs

BASE = {"answer_markdown": "", "all_prose_numeric_claims": [], "suggested_follow_ups": []}


def test_submit_answer_args_validators():
    """Exactly-one answer/question rule, literal-newline unescape, leaked-tag strip,
    and markdown structure checks."""
    # Accepted: answer only, question only
    SubmitAnswerArgs(**{**BASE, "answer_markdown": "Done."})
    SubmitAnswerArgs(**{**BASE, "clarifying_question": "Which metric?"})

    # Rejected: both set, neither set, and whitespace-only counts as neither
    with pytest.raises(ValidationError, match="not both"):
        SubmitAnswerArgs(**{**BASE, "answer_markdown": "Done.", "clarifying_question": "Which?"})
    with pytest.raises(ValidationError, match="exactly one"):
        SubmitAnswerArgs(**BASE)
    with pytest.raises(ValidationError, match="exactly one"):
        SubmitAnswerArgs(**{**BASE, "answer_markdown": "   ", "clarifying_question": "  "})

    # Literal backslash-n becomes a newline
    assert SubmitAnswerArgs(**{**BASE, "answer_markdown": "a\\nb"}).answer_markdown == "a\nb"
    # A leaked closing tag and everything after it is dropped
    assert SubmitAnswerArgs(
        **{**BASE, "answer_markdown": "Done.</answer_markdown> junk"}).answer_markdown == "Done."

    # Ragged table and unclosed fence are both rejected
    with pytest.raises(ValidationError, match="table looks malformed"):
        SubmitAnswerArgs(**{**BASE, "answer_markdown": "| A | B |\n|---|---|\n| 1 |"})
    with pytest.raises(ValidationError, match="never closed"):
        SubmitAnswerArgs(**{**BASE, "answer_markdown": "```\ncode"})
