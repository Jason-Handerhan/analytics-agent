"""Layer 1 tests for app/gateway/gateway.py auth — no real credentials needed.

Lean by design: scoped to auth-bypass prevention specifically, not every
failure mode that exists. Protocol/format checks (missing "Bearer " prefix,
a structurally malformed token) are deliberately not tested here — removing
either doesn't let anything bypass auth, it just changes which line of code
produces the same 401. Everything below does prevent a real bypass if
removed: a stolen/old token, a token for a different app or tenant, a
forged signature, an edited payload, or a missing shared secret.

Auth failure modes are tested once, directly against the shared
validate_entra_token/validate_api_key functions — both endpoints call the
same code, so there's no value in repeating each failure mode per endpoint.
Endpoint tests cover only the one thing direct calls can't: the full
request/response shape with valid credentials.
"""
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.gateway.gateway as gateway
from app.gateway.gateway import app, validate_api_key, validate_entra_token

client = TestClient(app)


def _tamper(token: str, **overrides) -> str:
    """Re-encode the payload with different claims but leave the original
    signature untouched — the signature was computed over the OLD payload,
    so this is what a tampered-in-transit token looks like."""
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
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {make_token(expired=True)}")
    assert exc.value.status_code == 401


def test_wrong_audience_rejected(make_token, patch_jwks):
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {make_token(audience='api://some-other-app')}")
    assert exc.value.status_code == 401


def test_wrong_issuer_rejected(make_token, patch_jwks):
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(
            f"Bearer {make_token(issuer='https://login.microsoftonline.com/wrong-tenant/v2.0')}"
        )
    assert exc.value.status_code == 401


def test_not_microsoft_signed_rejected(make_token, other_signing_key, patch_jwks):
    """Signed with a real, well-formed key — just not the one the JWKS
    endpoint actually serves. This is what a forged token looks like."""
    forged = make_token(signing_key=other_signing_key)
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {forged}")
    assert exc.value.status_code == 401


def test_tampered_payload_rejected(make_token, patch_jwks):
    """Valid signature, but for a different payload than the one that
    arrived — the classic tamper-in-transit case."""
    tampered = _tamper(make_token(), oid="attacker-oid")
    with pytest.raises(HTTPException) as exc:
        validate_entra_token(f"Bearer {tampered}")
    assert exc.value.status_code == 401


def test_wrong_api_key_rejected(monkeypatch):
    monkeypatch.setattr(gateway, "get_gateway_api_key", lambda: "expected-key")
    with pytest.raises(HTTPException) as exc:
        validate_api_key("wrong-key")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_owner_rejected(monkeypatch):
    """A conversation that's real, just not this token's — without this
    check, any signed-in user could read/act on anyone else's conversation
    by ID alone."""
    snapshot = MagicMock()
    snapshot.exists = True
    snapshot.to_dict.return_value = {"user_id": "someone-elses-oid"}
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.get = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(gateway, "get_db", lambda: fake_db)

    with pytest.raises(HTTPException) as exc:
        await gateway.assert_owns_conversation("some-id", {"oid": "my-oid"})
    assert exc.value.status_code == 404


# --- One valid-credentials test per endpoint -----------------------------

@pytest.fixture
def fake_db():
    db = MagicMock()
    doc_ref = db.collection.return_value.document.return_value
    doc_ref.set = AsyncMock(return_value=None)
    # assert_owns_conversation's .get() — defaults to "exists, owned by the
    # same oid auth_headers' token carries", so /ask's happy path passes.
    snapshot = MagicMock()
    snapshot.exists = True
    snapshot.to_dict.return_value = {"user_id": "11111111-2222-3333-4444-555555555555"}
    doc_ref.get = AsyncMock(return_value=snapshot)
    return db


@pytest.fixture
def auth_headers(make_token):
    return {"Authorization": f"Bearer {make_token()}", "x-api-key": "expected-key"}


def _patch_gateway(monkeypatch, fake_db):
    monkeypatch.setattr(gateway, "get_gateway_api_key", lambda: "expected-key")
    monkeypatch.setattr(gateway, "get_db", lambda: fake_db)


def test_post_conversation_success(monkeypatch, patch_jwks, fake_db, auth_headers):
    _patch_gateway(monkeypatch, fake_db)
    resp = client.post("/conversation", headers=auth_headers)
    assert resp.status_code == 200
    assert "conversation_id" in resp.json()
    fake_db.collection.assert_called_with("sessions")
    fake_db.collection.return_value.document.return_value.set.assert_awaited_once()


def test_post_ask_success(monkeypatch, patch_jwks, fake_db, auth_headers):
    _patch_gateway(monkeypatch, fake_db)
    resp = client.post(
        "/ask", headers=auth_headers,
        json={"question": "How many orders last week?", "conversation_id": "some-id"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer_markdown"] == "Echo: How many orders last week?"
    assert body["sources"] == []
