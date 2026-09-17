import uuid
from datetime import datetime, timezone
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Header, HTTPException
from google.cloud import firestore

from app.config import GCP_PROJECT_ID, TENANT_ID, EXPECTED_AUDIENCE, get_secret
from app.gateway.models import AskRequest, ConversationResponse
from app.orchestrator.models import AgentResponse

app = FastAPI()
_jwks_client = PyJWKClient(
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
)


# Lazy, not module-level globals: constructing these eagerly at import time
# means Layer 1 tests can't import this module without real credentials
# (google.cloud.firestore.AsyncClient() raises DefaultCredentialsError with
# none configured; get_secret() makes a real Secret Manager call). Deferring
# to first call — cached after that — keeps production behavior identical
# and makes both cleanly monkeypatchable in tests.
@lru_cache
def get_db() -> firestore.AsyncClient:
    return firestore.AsyncClient(project=GCP_PROJECT_ID)


@lru_cache
def get_gateway_api_key() -> str:
    return get_secret("gateway-api-key", GCP_PROJECT_ID)


def validate_entra_token(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=EXPECTED_AUDIENCE,
            issuer=f"https://sts.windows.net/{TENANT_ID}/",
        )
    except jwt.PyJWTError as e:
        unverified = jwt.decode(token, options={"verify_signature": False})
        raise HTTPException(
            401,
            f"Invalid token: {e}. Got iss={unverified.get('iss')!r}, "
            f"aud={unverified.get('aud')!r}",
        )


def validate_api_key(x_api_key: str) -> None:
    if x_api_key != get_gateway_api_key():
        raise HTTPException(401, "Invalid API key")


async def create_conversation(user_id: str) -> str:
    conversation_id = str(uuid.uuid4())
    await get_db().collection("sessions").document(conversation_id).set({
        "user_id": user_id,
        "recent_messages": [],
        "last_activity_at": datetime.now(timezone.utc),
    })
    return conversation_id


async def assert_owns_conversation(conversation_id: str, claims: dict) -> None:
    snap = await get_db().collection("sessions").document(conversation_id).get()
    if not snap.exists or snap.to_dict().get("user_id") != claims["oid"]:
        raise HTTPException(404, "Conversation not found.")


@app.post("/conversation", response_model=ConversationResponse)
async def post_conversation(authorization: str = Header(...), x_api_key: str = Header(...)):
    validate_api_key(x_api_key)
    claims = validate_entra_token(authorization)
    return ConversationResponse(conversation_id=await create_conversation(claims["oid"]))


@app.post("/ask", response_model=AgentResponse)
async def post_ask(
    body: AskRequest, authorization: str = Header(...), x_api_key: str = Header(...)
):
    validate_api_key(x_api_key)
    claims = validate_entra_token(authorization)
    await assert_owns_conversation(body.conversation_id, claims)
    # Canned — real synthesis, sources, and claims land with the LangGraph
    # loop in Phase 3
    return AgentResponse(
        answer_markdown=f"Echo: {body.question}",
        sources=[],
    )
