"""Layer 1 tests for app/orchestrator/tools.py -- pure functions, no
LLM/BigQuery/MCP calls (docs/testing.md).
"""
import asyncio
import base64
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from matplotlib.colors import to_rgba

import app.orchestrator.orchestrator as orchestrator_module
import app.orchestrator.tools as tools_module
from app.config import SHAP_FEATURE_DISPLAY_NAMES
from app.orchestrator.tools import PAGE_INFO_DIR, PAGES, ToolError, get_measure_dax, get_page_info, run_bigquery_sql


# --- run_bigquery_sql: description and the cancellation race ---------------------------------


def test_run_bigquery_sql_description_has_few_shot_examples():
    """BigQuery's few-shot examples are appended to the tool's own
    description, not the static context block (.claude/rules/orchestrator.md)."""
    assert "EXAMPLES" in run_bigquery_sql.description
    assert "reorder rate by department" in run_bigquery_sql.description
    assert all(label in run_bigquery_sql.description for label in SHAP_FEATURE_DISPLAY_NAMES.values())


# Test doubles for the cancellation race: no real BigQuery or Firestore.
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


# --- Lookup tools: get_measure_dax, get_page_info --------------------------------------------


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
    own file's text plus a base64-encoded screenshot of the same page, with
    no filter_context affecting anything except Model Performance's chart
    plot type."""
    assert PAGES == tuple(sorted(p.stem for p in PAGE_INFO_DIR.glob("*.txt")))

    for page_name in PAGES:
        expected_text = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
        expected_image = base64.b64encode((PAGE_INFO_DIR / f"{page_name}.png").read_bytes()).decode()
        result = await get_page_info.coroutine(page_name=page_name, filter_context=[])
        assert result == [
            {"type": "text", "text": f"Page: {page_name}\n\n{expected_text}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{expected_image}"}},
        ]

    # Beeswarm selected -- Model Performance's image switches.
    beeswarm_context = [{"Value": {"filter_column": "Feature_Importance_Chart_Types[Chart_Types]",
                                    "value": "Beeswarm"}}]
    expected_beeswarm_image = base64.b64encode(
        (PAGE_INFO_DIR / "Model Performance Beeswarm.png").read_bytes()).decode()
    result = await get_page_info.coroutine(page_name="Model Performance", filter_context=beeswarm_context)
    assert result[1] == {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{expected_beeswarm_image}"}}


# --- generate_beeswarm: the renderer ---------------------------------------------------------
# Pure, no BigQuery or GCS.


def _beeswarm_rows(fig) -> dict[int, dict]:
    """Per feature row (0 = bottom): point count, gray-point count, median x."""
    gray = to_rgba("#777777")
    points: dict[int, dict] = {}
    for collection in fig.axes[0].collections:
        offsets = np.asarray(collection.get_offsets())
        if len(offsets) == 0:
            continue
        facecolor = collection.get_facecolor()
        is_gray = len(facecolor) == 1 and np.allclose(facecolor[0], gray)
        for row, x in zip(np.round(offsets[:, 1]).astype(int), offsets[:, 0]):
            entry = points.setdefault(row, {"x": [], "gray": 0})
            entry["x"].append(x)
            entry["gray"] += is_gray
    return {row: {"points": len(e["x"]), "gray": e["gray"], "median_x": np.median(e["x"])}
            for row, e in points.items()}


def test_render_beeswarm():
    """Each feature's row holds that feature's own points (label and data stay
    aligned, ordered by importance), and only a null-filled feature's 999s plot gray."""
    features = tools_module.BEESWARM_FEATURES
    n = 200
    rng = np.random.default_rng(0)
    # Each feature's SHAP values sit around its own index, so a row's median x identifies it.
    df = pd.DataFrame({
        **{f"raw_{feature}": rng.random(n) for feature in features},
        **{f"shap_{feature}": index + rng.normal(0, 0.05, n) for index, feature in enumerate(features)},
    })
    null_filled = tools_module.NULL_FILLED_FEATURES[0]
    df.loc[:19, f"raw_{null_filled}"] = tools_module.NULL_FILL_VALUE              # 20 gray points
    df.loc[:4, "raw_user_distinct_product_count"] = tools_module.NULL_FILL_VALUE  # a real value: stays colored
    importance = np.arange(len(features), dtype=float)   # later features rank higher

    fig = tools_module._render_beeswarm(df, importance, "LightGBM Classifier", "Test title")
    labels = [t.get_text() for t in fig.axes[0].get_yticklabels()]   # bottom to top
    rows = _beeswarm_rows(fig)
    for index, feature in enumerate(features):
        assert labels[index] == SHAP_FEATURE_DISPLAY_NAMES[feature]
        assert rows[index]["points"] == n
        assert rows[index]["median_x"] == pytest.approx(index, abs=0.05)
        assert rows[index]["gray"] == (20 if feature == null_filled else 0)


# --- Memory tools: recall and search ---------------------------------------------------------


def _overview_rows(token_counts):
    return [{"turn": i, "answered_at": f"2026-10-26T12:0{i}:00+00:00", "approx_tokens": tokens,
             "sources": ["Query warehouse"], "title": f"title {i}", "topics": [f"topic {i}"],
             "entities": [f"entity {i}"], "question": f"Question {i}"}
            for i, tokens in enumerate(token_counts, start=1)]


def test_build_conversation_overview():
    """Totals, the newest turns that fit the budget, the summaries passed through, question
    truncation, and the empty conversation."""
    rows = _overview_rows([100, 200, 300])
    rows[0]["question"] = "x" * 400
    summaries = [{"first_turn": 1, "last_turn": 2, "title": "early work", "summary": "What was found."}]
    overview = tools_module.build_conversation_overview(rows, summaries, token_budget=550)

    assert overview["summaries"] == summaries
    assert overview["total_turns"] == 3 and overview["total_approx_tokens"] == 600   # summaries add nothing
    assert overview["max_recent_turns_within_budget"] == 2     # 300 + 200 fit; adding 100 would not
    assert overview["first_answered_at"] == rows[0]["answered_at"]
    assert (overview["turns"][1]["title"], overview["turns"][1]["topics"], overview["turns"][1]["entities"]) == (
        "title 2", ["topic 2"], ["entity 2"])
    assert len(overview["turns"][0]["question"]) == tools_module.OVERVIEW_QUESTION_MAX_CHARS
    assert overview["turns"][0]["question"].endswith("...")

    error = str(tools_module._over_budget_error(overview, 1, 3, 600))
    assert "Turns 1-3" in error and '"max_recent_turns_within_budget": 2' in error

    empty = tools_module.build_conversation_overview([], [], token_budget=550)
    assert empty["total_turns"] == 0 and empty["max_recent_turns_within_budget"] == 0
    assert empty["summaries"] == []
    assert empty["first_answered_at"] is None


@pytest.mark.parametrize("start, end, last_n, expected", [
    (None, None, None, (1, 5)),   # no arguments: the whole conversation
    (None, None, 2, (4, 5)),
    (None, None, 9, (1, 5)),     # last_n larger than the conversation: everything
    (2, 3, None, (2, 3)),
])
def test_resolve_turn_range(start, end, last_n, expected):
    assert tools_module.resolve_turn_range(5, start, end, last_n) == expected


def test_resolve_turn_range_rejects_bad_ranges():
    with pytest.raises(ToolError, match="5 verified turns"):
        tools_module.resolve_turn_range(5, 1, 9, None)
    with pytest.raises(ToolError, match="no verified turns"):
        tools_module.resolve_turn_range(0, None, None, None)


def test_get_conversation_findings_args_validator():
    """Arguments that don't belong with the chosen view are rejected."""
    Args = tools_module.GetConversationFindingsArgs
    assert Args(view="overview").view == "overview"
    assert Args(view="turns").last_n is None     # no range: every turn
    assert Args(view="turns", last_n=3).last_n == 3
    assert Args(view="turns", start_turn=2, end_turn=4, include_tool_calls=True).end_turn == 4

    for bad in [
        {"view": "overview", "last_n": 2},
        {"view": "overview", "include_tool_calls": True},
        {"view": "turns", "start_turn": 2},
        {"view": "turns", "end_turn": 2},
        {"view": "turns", "last_n": 2, "end_turn": 4},
        {"view": "turns", "last_n": 0},
        {"view": "turns", "start_turn": 0, "end_turn": 2},
        {"view": "turns", "start_turn": 4, "end_turn": 2},
    ]:
        with pytest.raises(ValueError):
            Args(**bad)


def test_search_conversation_findings_args_validator():
    """Arguments that don't belong with the chosen mode are rejected."""
    Args = tools_module.SearchConversationFindingsArgs
    assert Args(mode="search", query="shap features").top_k == tools_module.SEARCH_TOP_K_DEFAULT
    assert Args(mode="search", query="x", top_k=tools_module.SEARCH_TOP_K_MAX, scope="user").scope == "user"
    assert Args(mode="tags", scope="user").mode == "tags"

    for bad in [
        {"mode": "search"},
        {"mode": "search", "query": "   "},
        {"mode": "search", "query": "x", "top_k": 0},
        {"mode": "search", "query": "x", "top_k": tools_module.SEARCH_TOP_K_MAX + 1},
        {"mode": "tags", "query": "x"},
        {"mode": "tags", "topics": ["shap"]},
        {"mode": "tags", "entities": ["banana"]},
        {"mode": "tags", "include_tool_calls": True},
    ]:
        with pytest.raises(ValueError):
            Args(**bad)


# --- Task plan: manage_tasks -----------------------------------------------------------------


def test_manage_tasks_args_validator():
    """Blank, overlong and surplus tasks, a second task in progress, and an oversized scope are
    rejected; an omitted scope, an empty list and padded text are valid."""
    Args = tools_module.ManageTasksArgs

    def task(text="Do a thing", status="todo"):
        return {"text": text, "status": status}

    assert Args(tasks=[task(), task(status="in_progress")], scope="Model: X").scope == "Model: X"
    assert Args(tasks=[task()]).scope is None          # omitted: keep the saved scope
    assert Args(tasks=[]).tasks == []                  # an empty list clears the plan
    assert Args(tasks=[task("  padded  ")], scope="  s  ").tasks[0].text == "padded"

    for bad in [
        {"tasks": [task(text="   ")]},
        {"tasks": [task(text="x" * (tools_module.TASK_MAX_CHARS + 1))]},
        {"tasks": [task() for _ in range(tools_module.TASKS_MAX_COUNT + 1)]},
        {"tasks": [task(status="in_progress"), task(status="in_progress")]},
        {"tasks": [task()], "scope": "s" * (tools_module.TASK_SCOPE_MAX_CHARS + 1)},
        {"tasks": [task(status="finished")]},
    ]:
        with pytest.raises(ValueError):
            Args(**bad)
