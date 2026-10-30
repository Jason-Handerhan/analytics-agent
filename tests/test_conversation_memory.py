"""Layer 1 tests for app/orchestrator/conversation_memory/ -- the row builder
and the embedded text; the insert and the embedding call are thin external
calls (Layer 2)."""
import json
from datetime import datetime, timezone

from app.orchestrator.conversation_memory.embedding import build_embedding_text
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


def test_build_embedding_text():
    """Tags and the question and answer become labeled lines, empty tags are skipped, and a
    chart link (signed image, plain link, or on a CDN host) shrinks to its label while any
    other link stays."""
    chart_path = "charts/3c8eed8e-afcd-46bd-8081-7dd8e8ea48f6.png"
    signed_url = f"https://storage.googleapis.com/some-bucket/{chart_path}?X-Goog-Signature=abc123&X-Goog-Expires=604800"
    cdn_url = f"https://cdn.example.com/{chart_path}?sig=1"
    answer = (f"See ![Orders by day]({signed_url}) and [Orders again]({signed_url}) "
              f"and ![On CDN]({cdn_url}) and [docs](https://example.com/x.png).")

    text = build_embedding_text("Order counts", ["orders", "volume"], [], "How many orders?", answer)

    assert text == (
        "Title: Order counts\n"
        "Topics: orders, volume\n"
        "Question: How many orders?\n"
        "Answer: See Orders by day and Orders again and On CDN and [docs](https://example.com/x.png).")
    assert build_embedding_text("", [], [], "Q", "A") == "Question: Q\nAnswer: A"
