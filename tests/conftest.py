"""Shared Layer 1 fixtures — no real credentials needed (docs/testing.md)."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import EXPECTED_AUDIENCE, TENANT_ID

# v1-format issuer, matching what validate_entra_token expects
_REAL_ISSUER = f"https://sts.windows.net/{TENANT_ID}/"


@pytest.fixture(scope="session")
def signing_key():
    """RSA keypair for the test session."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_signing_key():
    """A second, unrelated RSA keypair."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def make_token(signing_key):
    """Mints a self-signed JWT shaped like a real Entra token; every claim
    is overridable, defaults are valid."""

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
    """Serves the test key's public half instead of Entra's real JWKS
    endpoint."""
    import app.gateway.gateway as gateway

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    monkeypatch.setattr(
        gateway._jwks_client,
        "get_signing_key_from_jwt",
        lambda token: _FakeSigningKey(signing_key.public_key()),
    )
