from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from app.config import AVAILABLE_MODELS, MODEL


# --- POST /conversation -----------------------------------------------------

class ConversationResponse(BaseModel):
    conversation_id: str


# --- POST /ask ---------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = ""
    image_base64: str | None = None
    filter_context: list[dict] = []
    active_page: str | None = None
    conversation_id: str
    approval_decision: Literal["approved", "rejected"] | None = None
    model: str = MODEL

    @field_validator("model")
    @classmethod
    def _model_must_be_available(cls, value: str) -> str:
        if value not in AVAILABLE_MODELS:
            raise ValueError(f"Unknown model: {value}. Available: {list(AVAILABLE_MODELS)}")
        return value

    @model_validator(mode="after")
    def _exactly_one_of_question_or_decision(self) -> "AskRequest":
        has_question = bool(self.question.strip())
        has_decision = self.approval_decision is not None
        if has_question == has_decision:
            raise ValueError("Provide exactly one of question or approval_decision.")
        return self


class AgentResponse(BaseModel):
    """The wire format the gateway returns."""
    answer_markdown: str
    answer_html: str
    sources: list[str]
    needs_approval: bool = False
    chart_urls: list[str] = []
    suggested_follow_ups: list[str] = []
    iteration_cap_hit: bool = False
    pending_query: str | None = None
    estimated_cost: str | None = None
    cost_cap_exceeded: bool = False


# --- GET /ask/status/{conversation_id} ---------------------------------------

class StatusResponse(BaseModel):
    """Live progress for a turn in flight. status is None when no turn is running."""
    status: str | None
    thinking_log: list[dict]


# --- POST /ask/cancel/{conversation_id} ---------------------------------------

class CancelResponse(BaseModel):
    """Confirms the flag was written -- not that the turn has actually stopped."""
    cancel_requested: bool


# --- GET /conversations -------------------------------------------------------

class ConversationSummary(BaseModel):
    """One row in GET /conversations -- title is the truncated first question."""
    conversation_id: str
    title: str
    last_activity_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]


# --- GET /conversations/{conversation_id}/history ------------------------------

class ConversationTurn(BaseModel):
    """One turn in GET /conversations/{id}/history -- shaped to match colChat."""
    question: str
    answer_html: str
    sources: list[str]
    suggested_follow_ups: list[str]
    needs_approval: bool


class ConversationHistoryResponse(BaseModel):
    turns: list[ConversationTurn]


# --- DELETE /conversations/{conversation_id} -----------------------------------

class DeleteConversationResponse(BaseModel):
    """Confirms the soft-delete marker was written."""
    deleted: bool
