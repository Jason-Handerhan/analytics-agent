from pydantic import BaseModel


class AskRequest(BaseModel):
    question: str
    image_base64: str | None = None
    filter_context: list[dict] = []
    active_page: str | None = None
    conversation_id: str


class ConversationResponse(BaseModel):
    conversation_id: str
