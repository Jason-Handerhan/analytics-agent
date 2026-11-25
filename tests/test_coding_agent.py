from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from coding_agent_app.coding_agent import CODING_RUBRIC, DASHBOARD_RUBRIC, CountingUsageHandler


def make_response(input_tokens: int, output_tokens: int) -> LLMResult:
    """What a chat model hands the handler when a call ends."""
    message = AIMessage(content="x", response_metadata={"model_name": "claude-x"},
                        usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens})
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def test_counting_usage_handler():
    """The handler still adds up usage by model, and counts each call, including one with no usage."""
    handler = CountingUsageHandler()
    handler.on_llm_end(make_response(100, 10))
    handler.on_llm_end(make_response(50, 5))
    handler.on_llm_end(LLMResult(generations=[[]]))
    assert handler.calls == 3
    assert handler.usage_metadata["claude-x"]["input_tokens"] == 150 and handler.usage_metadata["claude-x"]["output_tokens"] == 15


def test_plan_guides_hold_the_rubrics():
    """The main agent reads the reviewer's criteria in each plan guide, so a guide must hold the same text the reviewer judges by."""
    guides = Path(__file__).parent.parent / "context" / "subagent_plan_guides"
    assert CODING_RUBRIC.strip() in (guides / "code.md").read_text(encoding="utf-8")
    assert DASHBOARD_RUBRIC.strip() in (guides / "dashboard.md").read_text(encoding="utf-8")
