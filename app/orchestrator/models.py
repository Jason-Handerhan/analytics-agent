from pydantic import BaseModel


class Claim(BaseModel):
    text: str
    numeric_value: float | None = None
    source_tool_call_id: str | None = None


class AgentResponse(BaseModel):
    """The wire format the gateway returns. Schema of record:
    .claude/rules/orchestrator.md — implement exactly."""
    answer_markdown: str
    sources: list[str]
    needs_approval: bool = False
    chart_url: str | None = None
    claims: list[Claim] = []
    suggested_follow_ups: list[str] = []
    iteration_cap_hit: bool = False
    pending_query: str | None = None
    estimated_cost: str | None = None
    cost_cap_exceeded: bool = False
