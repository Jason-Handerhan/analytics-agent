from pydantic import BaseModel


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
