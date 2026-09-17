"""Shared Layer 1 fixtures — no real credentials needed (docs/testing.md)."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import EXPECTED_AUDIENCE, TENANT_ID

_REAL_ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"


@pytest.fixture(scope="session")
def signing_key():
    """One throwaway RSA keypair for the session — stands in for the key
    Entra would actually sign with."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_signing_key():
    """A second, unrelated keypair — for the 'not Microsoft-signed' case."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def make_token(signing_key):
    """Mint a self-signed JWT shaped like a real Entra token. Every claim is
    overridable; defaults are all valid. Mirrors
    notebooks/phase1_gateway.ipynb's _make_test_token."""

    def _make(
        *,
        signing_key=signing_key,
        audience=EXPECTED_AUDIENCE,
        issuer=_REAL_ISSUER,
        oid="11111111-2222-3333-4444-555555555555",
        kid="test-key-1",
        expired=False,
    ) -> str:
        now = int(time.time())
        claims = {
            "oid": oid,
            "aud": audience,
            "iss": issuer,
            "iat": now,
            "exp": now - 10 if expired else now + 3600,
        }
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": kid})

    return _make


@pytest.fixture
def patch_jwks(monkeypatch, signing_key):
    """Serve our test key's public half instead of hitting Entra's real
    JWKS endpoint — this is the whole reason validate_entra_token is
    testable with no real Entra dependency."""
    import app.gateway.main as gateway_main

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    monkeypatch.setattr(
        gateway_main._jwks_client,
        "get_signing_key_from_jwt",
        lambda token: _FakeSigningKey(signing_key.public_key()),
    )
