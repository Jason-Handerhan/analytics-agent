"""Layer 1 tests for app/orchestrator/conversation_memory/ -- the row builder
only; the insert is a thin BigQuery call (Layer 2)."""
import json
from datetime import datetime, timezone

from app.orchestrator.conversation_memory.schema import CONVERSATION_FINDINGS_SCHEMA
from app.orchestrator.conversation_memory.writer import build_findings_row

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def _tool_call(name, args, success=True, ref_id=None):
    return {"id": "t", "name": name, "args": args, "query_text": None, "result": {"rows": [1]},
            "success": success, "error": None, "ref_id": ref_id, "bytes_billed": None,
            "started_at": NOW, "completed_at": NOW}


def test_build_findings_row():
    row = build_findings_row(
        conversation_id="c1", user_id="u1", answered_at=NOW, question="Q" * 40,
        answer_markdown="A" * 80, verified_numbers=[1.5, 2.0], filter_context=[{"table": "t"}],
        active_page="Overview", sources=["Query warehouse"],
        title="reorder rate by department", topics=["reorder rate"], entities=["Dairy Eggs"],
        tool_calls=[
            _tool_call("run_bigquery_sql", {"query": "SELECT 1", "label": "x"}, ref_id="r1"),
            _tool_call("generate_chart", {"source_ref": "r1", "spec": {}}),
            _tool_call("run_bigquery_sql", {"query": "BAD"}, success=False),
            _tool_call("submit_answer", {"answer_markdown": "A"}),
        ],
    )

    assert [tc["name"] for tc in row["tool_calls"]] == ["run_bigquery_sql", "generate_chart"]
    sql_call = row["tool_calls"][0]
    assert sql_call["ref_id"] == "r1"
    assert json.loads(sql_call["args"]) == {"query": "SELECT 1", "label": "x"}
    assert json.loads(row["tool_calls"][1]["args"])["source_ref"] == "r1"
    assert row["approx_tokens"] == (40 + 80 + len("[1.5, 2.0]")) // 4
    assert row["verified_numbers"] == [1.5, 2.0]
    assert (row["title"], row["topics"], row["entities"]) == ("reorder rate by department", ["reorder rate"], ["Dairy Eggs"])
    assert set(row) == {f.name for f in CONVERSATION_FINDINGS_SCHEMA}
