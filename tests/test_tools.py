"""Layer 1 tests for app/orchestrator/tools.py -- pure functions, no
LLM/BigQuery/MCP calls (docs/testing.md).
"""
import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

import app.orchestrator.orchestrator as orchestrator_module
import app.orchestrator.tools as tools_module
from app.orchestrator.tools import PAGE_INFO_DIR, PAGES, ToolError, get_measure_dax, get_page_info, run_bigquery_sql


# --- Fakes for run_bigquery_sql's cancellation race -- no real BigQuery/Firestore ---

class FakeRows(list):
    total_rows = 0


class FakeJob:
    """Stands in for bigquery.QueryJob -- result() blocks for `delay` seconds,
    same shape as the real synchronous job.result()."""
    def __init__(self, delay: float, job_id: str = "fake-job-1"):
        self.job_id = job_id
        self.total_bytes_billed = 0
        self.total_bytes_processed = 0
        self._delay = delay

    def result(self, timeout=None, max_results=None):
        time.sleep(self._delay)
        return FakeRows()


class FakeBQClient:
    """Stands in for bigquery.Client -- records which job ids get cancelled."""
    def __init__(self, delay: float):
        self._delay = delay
        self.cancelled_job_ids: list[str] = []

    def query(self, query, job_config=None):
        return FakeJob(self._delay)

    def cancel_job(self, job_id):
        self.cancelled_job_ids.append(job_id)


@pytest.mark.asyncio
async def test_get_measure_dax(monkeypatch):
    """A standalone measure returns just itself; one referencing others
    pulls them in via one-hop expansion."""
    monkeypatch.setattr(tools_module, "MEASURE_DAX", {
        "Solo Measure": "SUM(table[column])",
        "Combo Measure": "[Solo Measure] + [Other Measure]",
        "Other Measure": "COUNT(table[id])",
        "Unrelated Measure": "AVERAGE(table[value])",
    })

    # A measure with no reference to another measure -- returns just itself
    result = await get_measure_dax.coroutine(measure_names=["Solo Measure"])
    assert result == {"Solo Measure": "SUM(table[column])"}

    # A measure referencing two other measures -- both get pulled in, one hop
    result = await get_measure_dax.coroutine(measure_names=["Combo Measure"])
    assert result == {
        "Combo Measure": "[Solo Measure] + [Other Measure]",
        "Solo Measure": "SUM(table[column])",
        "Other Measure": "COUNT(table[id])",
    }


@pytest.mark.asyncio
async def test_get_page_info():
    """PAGES matches the real committed files, and every page returns its
    own file's exact content."""
    assert PAGES == tuple(sorted(p.stem for p in PAGE_INFO_DIR.glob("*.txt")))

    for page_name in PAGES:
        expected = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
        result = await get_page_info.coroutine(page_name=page_name)
        assert result == {"page_name": page_name, "content": expected}


# run_bigquery_sql -- the cancellation race (Level 2)

@pytest.mark.asyncio
async def test_run_bigquery_sql_cancel_wins_race(monkeypatch):
    """The watcher notices cancel_requested before the query finishes --
    raises ToolError and cancels the real BigQuery job."""
    fake_client = FakeBQClient(delay=5)  # long enough the watcher always wins
    monkeypatch.setattr(tools_module, "get_bq_client", lambda: fake_client)
    monkeypatch.setattr(tools_module, "is_cancelled", AsyncMock(return_value=True))

    with pytest.raises(ToolError, match="Cancelled by user request."):
        await run_bigquery_sql.coroutine(query="SELECT 1", conversation_id="c1")

    assert fake_client.cancelled_job_ids == ["fake-job-1"]


@pytest.mark.asyncio
async def test_run_bigquery_sql_query_wins_race(monkeypatch):
    """A fast query returns its real result before the watcher ever fires --
    no cancellation, no cancel_job call."""
    fake_client = FakeBQClient(delay=0)
    monkeypatch.setattr(tools_module, "get_bq_client", lambda: fake_client)
    monkeypatch.setattr(tools_module, "is_cancelled", AsyncMock(return_value=False))

    result_rows, artifact = await run_bigquery_sql.coroutine(query="SELECT 1", conversation_id="c1")
    assert result_rows == []
    assert artifact == {"bytes_billed": 0}
    assert fake_client.cancelled_job_ids == []


@pytest.mark.asyncio
async def test_gateway_timeout_cancellation_reaches_bigquery_job(monkeypatch):
    """A gateway-style asyncio.wait_for timeout, applied to the REAL compiled
    graph (not a direct call to run_bigquery_sql), must still reach its
    cancellation handler -- proves LangGraph's astream/dispatch internals
    propagate cancellation rather than swallowing it. approved_batch=True
    sends route_entry straight to call_tool, so no real LLM call is needed."""
    fake_client = FakeBQClient(delay=5)  # long enough our 0.1s timeout always wins
    monkeypatch.setattr(tools_module, "get_bq_client", lambda: fake_client)
    monkeypatch.setattr(tools_module, "is_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(orchestrator_module, "is_cancelled", AsyncMock(return_value=False))
    # ALL_TOOLS is only populated by init_orchestrator(), not called here --
    # this path only ever looks up run_bigquery_sql in it.
    monkeypatch.setattr(orchestrator_module, "ALL_TOOLS", {"run_bigquery_sql": run_bigquery_sql})

    state = {
        "conversation_id": "c1", "user_id": "u1", "question": "Q",
        "turn_started_at": datetime.now(timezone.utc), "filter_context": [], "active_page": None,
        "image_base64": None, "history_messages": [], "tool_calls": [], "errors": [],
        "llm_calls": 0, "bytes_consumed": 0, "bytes_consumed_baseline": 0,
        "iteration_count": 0, "verification_retry_count": 0, "length_retry_count": 0,
        "needs_approval": False, "cost_cap_exceeded": False, "iteration_cap_hit": False,
        "estimated_cost": None, "pending_queries": [], "largest_pending_query": None,
        "approval_decision": None, "deferred_dax": [], "chart_urls": [],
        "suggested_follow_ups": [], "all_prose_numeric_claims": [], "clarifying_question": "",
        "answer_markdown": "", "verified": False, "sources": [], "paused_at": None,
        "approved_batch": True,
        "messages": [AIMessage(content="", tool_calls=[
            {"name": "run_bigquery_sql", "args": {"query": "SELECT 1"}, "id": "call_1"}])],
    }

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(orchestrator_module.graph.ainvoke(state), timeout=0.1)

    assert fake_client.cancelled_job_ids == ["fake-job-1"]
