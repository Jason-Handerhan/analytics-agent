"""Layer 1 tests for app/gateway/gateway.py -- no real credentials needed (docs/testing.md).

Auth failure modes, one valid-credentials test per endpoint, and the live status
path: consume_graph's write sequence and the GET /ask/status route.
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict

from app.config import MAX_QUESTION_CHARS, SESSIONS_TTL_DAYS
import app.gateway.gateway as gateway
from app.gateway.gateway import app, validate_api_key, validate_entra_token

client = TestClient(app)

OWNER_OID = "11111111-2222-3333-4444-555555555555"


class _Doc:
    """A Firestore document stand-in: serves its canned data and records every write."""

    def __init__(self, data=None):
        self._data = data
        self.writes = []  # (method, data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data

    async def get(self):
        return self

    async def set(self, data, merge=False):
        self.writes.append(("set", data))

    async def update(self, data):
        self.writes.append(("update", data))

    async def delete(self):
        self.writes.append(("delete", None))


def _patch_docs(monkeypatch, session=None, live=None):
    """Swaps the two document helpers for fakes; returns (session, live)."""
    session = session or _Doc({"user_id": OWNER_OID})
    live = live or _Doc()
    monkeypatch.setattr(gateway, "session_doc", lambda conversation_id: session)
    monkeypatch.setattr(gateway, "live_turn_doc", lambda conversation_id: live)
    return session, live


def _tamper(token: str, **overrides) -> str:
    """Re-encodes the payload with different claims, leaving the original
    signature untouched."""
    header_b64, payload_b64, sig_b64 = token.split(".")
    padding = "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    payload.update(overrides)
    new_payload = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    return f"{header_b64}.{new_payload}.{sig_b64}"


# --- Auth failure modes, tested once against the shared functions -------

def test_expired_token_rejected(make_token, patch_jwks):
    """Token whose exp has already passed."""
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {make_token(expired=True)}")
    assert exc.value.status_code == 401


def test_wrong_audience_rejected(make_token, patch_jwks):
    """Token issued for a different application."""
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {make_token(audience='api://some-other-app')}")
    assert exc.value.status_code == 401


def test_wrong_issuer_rejected(make_token, patch_jwks):
    """Token issued by a different tenant."""
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(
            f"Bearer {make_token(issuer='https://login.microsoftonline.com/wrong-tenant/v2.0')}"
        )
    assert exc.value.status_code == 401


def test_not_microsoft_signed_rejected(make_token, other_signing_key, patch_jwks):
    """Token signed with a key the JWKS endpoint doesn't serve."""
    forged = make_token(signing_key=other_signing_key)
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {forged}")
    assert exc.value.status_code == 401


def test_tampered_payload_rejected(make_token, patch_jwks):
    """Valid signature, tampered payload."""
    tampered = _tamper(make_token(), oid="attacker-oid")
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {tampered}")
    assert exc.value.status_code == 401


def test_wrong_api_key_rejected(monkeypatch):
    """x-api-key header doesn't match the gateway's own key."""
    monkeypatch.setattr(gateway, "get_gateway_api_key", lambda: "expected-key")
    with pytest.raises(HTTPException) as exc:
        validate_api_key("wrong-key")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_owner_rejected(monkeypatch):
    """Conversation exists but belongs to a different user."""
    _patch_docs(monkeypatch, session=_Doc({"user_id": "someone-elses-oid"}))

    with pytest.raises(HTTPException) as exc:
        await gateway.assert_owns_conversation("some-id", {"oid": "my-oid"})
    assert exc.value.status_code == 404


# --- One valid-credentials test per endpoint -----------------------------

@pytest.fixture
def auth_headers(make_token):
    return {"Authorization": f"Bearer {make_token()}", "x-api-key": "expected-key"}


def _patch_gateway(monkeypatch):
    monkeypatch.setattr(gateway, "get_gateway_api_key", lambda: "expected-key")


class _FakeGraph:
    """Stands in for the real compiled graph -- yields one canned "values"
    chunk, the same v2 dict shape graph.astream produces."""

    async def astream(self, initial_state, stream_mode=None, version=None):
        yield {"type": "values", "data": {
            "messages": [],  # build_updated_history's write-back needs this key
            "answer_markdown": "There were 551,399 orders.",
            "sources": ["Query warehouse"],
            "needs_approval": False,
            "chart_urls": [],
            "suggested_follow_ups": [],
            "iteration_cap_hit": False,
            "pending_queries": [],
            "estimated_cost": None,
            "cost_cap_exceeded": False,
            "largest_pending_query": None,
        }}


def test_post_conversation_success(monkeypatch, patch_jwks, auth_headers):
    """Valid credentials -- mints a conversation_id and writes its sessions doc."""
    _patch_gateway(monkeypatch)
    session, _ = _patch_docs(monkeypatch)
    resp = client.post("/conversation", headers=auth_headers)
    assert resp.status_code == 200
    assert "conversation_id" in resp.json()
    assert [method for method, _ in session.writes] == ["set"]
    assert session.writes[0][1]["last_activity_at"] > datetime.now(timezone.utc) + timedelta(
        days=SESSIONS_TTL_DAYS - 1)


def test_post_ask_success(monkeypatch, patch_jwks, auth_headers):
    """Valid credentials -- runs the (faked) graph and returns its answer."""
    _patch_gateway(monkeypatch)
    session, live = _patch_docs(monkeypatch)
    monkeypatch.setattr(gateway, "init_orchestrator", AsyncMock())
    monkeypatch.setattr(gateway, "graph", _FakeGraph())

    resp = client.post(
        "/ask", headers=auth_headers,
        json={"question": "How many orders last week?", "conversation_id": "some-id"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer_markdown"] == "There were 551,399 orders."
    assert body["sources"] == ["Query warehouse"]
    assert [method for method, _ in live.writes] == ["set", "delete"]
    assert session.writes[0][1]["last_activity_at"] > datetime.now(timezone.utc) + timedelta(
        days=SESSIONS_TTL_DAYS - 1)


def test_question_too_long_rejected(monkeypatch, patch_jwks, auth_headers):
    """Question over MAX_QUESTION_CHARS is rejected before a turn starts."""
    _patch_gateway(monkeypatch)
    _patch_docs(monkeypatch)
    resp = client.post(
        "/ask", headers=auth_headers,
        json={"question": "x" * (MAX_QUESTION_CHARS + 1), "conversation_id": "some-id"},
    )
    assert resp.status_code == 400


def _fake_pending_approval() -> dict:
    """A PendingApproval-shaped dict, as stored under live_turns.pending_approval."""
    now = datetime.now(timezone.utc)
    return {
        "conversation_id": "some-id",
        "question": "How many orders?",
        "filter_context": [{"filter_column": "department", "value": "produce"}],
        "active_page": "Financial Impact",
        "messages": messages_to_dict([HumanMessage(content="How many orders?")]),
        "pending_queries": [{"id": "c1", "query": "SELECT 1", "estimated_bytes": 999}],
        "deferred_dax": [],
        "tool_calls": [{
            "id": "c1", "name": "run_bigquery_sql", "args": {"query": "SELECT 1"},
            "query_text": "SELECT 1", "result": [{"n": 1}], "success": True, "error": None,
            "ref_id": "ref_1", "bytes_billed": 1234, "started_at": now, "completed_at": now,
        }],
        "iteration_count": 2,
        "bytes_consumed": 1000,
        "estimated_cost": "$5.00",
        "paused_at": now,
    }


class _RecordingGraph:
    """Captures the initial_state it's invoked with; yields one canned final state."""

    def __init__(self):
        self.captured_state = None

    async def astream(self, initial_state, stream_mode=None, version=None):
        self.captured_state = initial_state
        yield {"type": "values", "data": {
            "messages": [],
            "answer_markdown": "There were 3 orders.",
            "sources": ["Query warehouse"],
            "needs_approval": False,
            "chart_urls": [],
            "suggested_follow_ups": [],
            "iteration_cap_hit": False,
            "pending_queries": [],
            "estimated_cost": None,
            "cost_cap_exceeded": False,
            "largest_pending_query": None,
        }}


def test_post_ask_resume_approved_seeds_state(monkeypatch, patch_jwks, auth_headers):
    """Approving a pending approval rebuilds initial_state from it, not from the request body."""
    _patch_gateway(monkeypatch)
    pending = _fake_pending_approval()
    _patch_docs(monkeypatch, live=_Doc({"pending_approval": pending}))
    monkeypatch.setattr(gateway, "init_orchestrator", AsyncMock())
    recording_graph = _RecordingGraph()
    monkeypatch.setattr(gateway, "graph", recording_graph)

    resp = client.post(
        "/ask", headers=auth_headers,
        json={"conversation_id": "some-id", "approval_decision": "approved"},
    )

    assert resp.status_code == 200
    seeded = recording_graph.captured_state
    assert seeded["question"] == pending["question"]
    assert seeded["filter_context"] == pending["filter_context"]
    assert seeded["active_page"] == pending["active_page"]
    assert seeded["tool_calls"] == pending["tool_calls"]
    assert seeded["iteration_count"] == pending["iteration_count"]
    assert seeded["bytes_consumed"] == pending["bytes_consumed"]
    assert seeded["bytes_consumed_baseline"] == pending["bytes_consumed"]
    assert seeded["approved_batch"] is True
    assert seeded["approval_decision"] == "approved"
    assert seeded["messages"][-1].content == "How many orders?"


def test_post_ask_resume_rejected_returns_message(monkeypatch, patch_jwks, auth_headers):
    """Rejecting a pending approval returns the canned message -- no graph run."""
    _patch_gateway(monkeypatch)
    pending = _fake_pending_approval()
    _, live = _patch_docs(monkeypatch, live=_Doc({"pending_approval": pending}))
    monkeypatch.setattr(gateway, "write_telemetry_row", AsyncMock())

    resp = client.post(
        "/ask", headers=auth_headers,
        json={"conversation_id": "some-id", "approval_decision": "rejected"},
    )

    assert resp.status_code == 200
    assert resp.json()["answer_markdown"] == gateway.REJECTED_MESSAGE
    assert live.writes == [("delete", None)]


# --- Live status: consume_graph and GET /ask/status ----------------------

HEADERS = {"Authorization": "Bearer stand-in", "x-api-key": "stand-in"}

AGENT_CHUNK = {"type": "updates", "data": {"agent": {
    "messages": [AIMessage(content=[{"type": "thinking", "thinking": "Plan it."}])],
    "llm_calls": 1}}}
CALL_TOOL_CHUNK = {"type": "updates", "data": {"call_tool": {"messages": [
    ToolMessage(content="[]", name="run_bigquery_sql", tool_call_id="c1", status="success")]}}}
CHECK_CHUNK = {"type": "updates", "data": {"check_length": {"length_retry_count": 0}}}
VERIFY_CHUNK = {"type": "updates", "data": {"verify": {"verified": True}}}
FINALIZE_CHUNK = {"type": "updates", "data": {"finalize": {"answer_markdown": "There were 3 orders."}}}
FINAL_VALUES = {"type": "values", "data": {
    "answer_markdown": "There were 3 orders.", "verified": True, "needs_approval": False}}


class _ScriptedGraph:
    """Yields canned v2 chunks, then raises error if one is given."""

    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error

    async def astream(self, initial_state, stream_mode=None, version=None):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


def _live_writes(doc):
    """The live document's writes in order, with ArrayUnion unwrapped to its values."""
    writes = []
    for method, data in doc.writes:
        if method == "update" and "thinking_log" in data:
            data = {"thinking_log": data["thinking_log"].values}
        writes.append((method, data))
    return writes


@pytest.mark.asyncio
async def test_consume_graph_writes_status_and_cleans_up(monkeypatch):
    """Each finished node writes its status with the time since the previous step;
    the agent's thinking goes to thinking_log; the document is deleted at the end."""
    _, live = _patch_docs(monkeypatch)
    monkeypatch.setattr(gateway, "graph", _ScriptedGraph([
        AGENT_CHUNK, CALL_TOOL_CHUNK, CHECK_CHUNK, VERIFY_CHUNK, FINALIZE_CHUNK, FINAL_VALUES]))
    ticks = iter([0.0, 1.5, 3.0, 4.0, 4.5])
    monkeypatch.setattr(gateway, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    final_state = await gateway.consume_graph({"conversation_id": "conv-1"})

    assert final_state == FINAL_VALUES["data"]
    assert _live_writes(live) == [
        ("set", {"status": "Thinking...", "thinking_log": []}),
        ("update", {"thinking_log": [{"seq": 1, "text": "Plan it."}]}),
        ("update", {"status": "Thought (1.5s)"}),
        ("update", {"status": "Gathered results from BigQuery (1.5s)"}),
        ("update", {"status": "Verified results (1.0s)"}),
        ("update", {"status": "Prepared your answer (0.5s)"}),
        ("delete", None),
    ]


PAUSED_VALUES = {"type": "values", "data": {
    "conversation_id": "conv-1",
    "question": "How many orders?",
    "filter_context": [{"filter_column": "department", "value": "produce"}],
    "active_page": "Financial Impact",
    "messages": [HumanMessage(content="How many orders?")],
    "pending_queries": [{"id": "c1", "query": "SELECT 1", "estimated_bytes": 999}],
    "deferred_dax": [{"id": "c2", "dax": "EVALUATE 'x'"}],
    "tool_calls": [],
    "iteration_count": 2,
    "bytes_consumed": 1000,
    "estimated_cost": "$5.00",
    "paused_at": datetime.now(timezone.utc),
    "needs_approval": True,
}}


@pytest.mark.asyncio
async def test_consume_graph_replaces_doc_with_pending_approval_on_pause(monkeypatch):
    """A paused turn's live_turns doc is replaced with just the PendingApproval
    -- every field mapped, including the messages_to_dict reshape -- never deleted."""
    _, live = _patch_docs(monkeypatch)
    monkeypatch.setattr(gateway, "graph", _ScriptedGraph([PAUSED_VALUES]))

    await gateway.consume_graph({"conversation_id": "conv-1"})

    expected = PAUSED_VALUES["data"]
    assert live.writes == [
        ("set", {"status": "Thinking...", "thinking_log": []}),
        ("set", {"pending_approval": {
            "conversation_id": expected["conversation_id"],
            "question": expected["question"],
            "filter_context": expected["filter_context"],
            "active_page": expected["active_page"],
            "messages": messages_to_dict(expected["messages"]),
            "pending_queries": expected["pending_queries"],
            "deferred_dax": expected["deferred_dax"],
            "tool_calls": expected["tool_calls"],
            "iteration_count": expected["iteration_count"],
            "bytes_consumed": expected["bytes_consumed"],
            "estimated_cost": expected["estimated_cost"],
            "paused_at": expected["paused_at"],
        }}),
    ]


@pytest.fixture
def owner_stubs(monkeypatch):
    monkeypatch.setattr(gateway, "validate_api_key", lambda x_api_key: None)
    monkeypatch.setattr(gateway, "validate_entra_token", lambda authorization: {"oid": "user-1"})


def test_get_ask_status_returns_live_progress(monkeypatch, owner_stubs):
    """A running turn's status and thinking_log come back as the response body."""
    _patch_docs(
        monkeypatch,
        session=_Doc({"user_id": "user-1"}),
        live=_Doc({"status": "Thought (1.5s)", "thinking_log": [{"seq": 1, "text": "Plan it."}]}),
    )

    resp = client.get("/ask/status/conv-1", headers=HEADERS)

    assert resp.status_code == 200
    assert resp.json() == {"status": "Thought (1.5s)", "thinking_log": [{"seq": 1, "text": "Plan it."}]}
