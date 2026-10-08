from typing import Literal

from pydantic import BaseModel, model_validator


class AskRequest(BaseModel):
    question: str = ""
    image_base64: str | None = None
    filter_context: list[dict] = []
    active_page: str | None = None
    conversation_id: str
    approval_decision: Literal["approved", "rejected"] | None = None

    @model_validator(mode="after")
    def _exactly_one_of_question_or_decision(self) -> "AskRequest":
        has_question = bool(self.question.strip())
        has_decision = self.approval_decision is not None
        if has_question == has_decision:
            raise ValueError("Provide exactly one of question or approval_decision.")
        return self


class ConversationResponse(BaseModel):
    conversation_id: str


class StatusResponse(BaseModel):
    """Live progress for a turn in flight. status is None when no turn is running."""
    status: str | None
    thinking_log: list[dict]


class AgentResponse(BaseModel):
    """The wire format the gateway returns."""
    answer_markdown: str
    sources: list[str]
    needs_approval: bool = False
    chart_urls: list[str] = []
    suggested_follow_ups: list[str] = []
    iteration_cap_hit: bool = False
    pending_query: str | None = None
    estimated_cost: str | None = None
    cost_cap_exceeded: bool = False
