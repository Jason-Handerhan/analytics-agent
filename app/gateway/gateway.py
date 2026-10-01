import asyncio
import logging
import uuid
from datetime import datetime, timezone
from functools import lru_cache

import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Header, HTTPException
from google.cloud import firestore

from app.config import (
    EXPECTED_AUDIENCE,
    GATEWAY_TURN_TIMEOUT_SECONDS,
    GCP_PROJECT_ID,
    MAX_QUESTION_CHARS,
    TENANT_ID,
    get_secret,
)
from app.gateway.models import AskRequest, ConversationResponse
from app.orchestrator.models import AgentResponse
from app.orchestrator.orchestrator import (
    AgentState,
    build_agent_response,
    build_initial_state,
    graph,
    init_orchestrator,
)

logger = logging.getLogger(__name__)

app = FastAPI()
_jwks_client = PyJWKClient(
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
)


# --- Helper functions --------------------------------------------------

# Lazy, not module-level globals: constructing these eagerly at import time
# means Layer 1 tests can't import this module without real credentials
@lru_cache
def get_db() -> firestore.AsyncClient:
    """Lazy, cached Firestore client."""
    return firestore.AsyncClient(project=GCP_PROJECT_ID)


@lru_cache
def get_gateway_api_key() -> str:
    """The gateway's own API key, from Secret Manager."""
    return get_secret("gateway-api-key", GCP_PROJECT_ID)


def validate_entra_token(authorization: str) -> dict:
    """Verifies a bearer token against Entra's JWKS; returns its claims."""
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
    """Checks the x-api-key header against the gateway's own key."""
    if x_api_key != get_gateway_api_key():
        raise HTTPException(401, "Invalid API key")


async def create_conversation(user_id: str) -> str:
    """Creates a new sessions document; returns its conversation_id."""
    conversation_id = str(uuid.uuid4())
    await get_db().collection("sessions").document(conversation_id).set({
        "user_id": user_id,
        "recent_messages": [],
        "last_activity_at": datetime.now(timezone.utc),
    })
    return conversation_id


async def assert_owns_conversation(conversation_id: str, claims: dict) -> None:
    """Raises 404 unless claims owns this conversation."""
    snap = await get_db().collection("sessions").document(conversation_id).get()
    if not snap.exists or snap.to_dict().get("user_id") != claims["oid"]:
        raise HTTPException(404, "Conversation not found.")


async def run_agent_turn(body: AskRequest, user_id: str) -> AgentResponse:
    """Runs one turn through the graph; falls back to a plain AgentResponse
    on timeout or error."""
    await init_orchestrator()
    initial_state = build_initial_state(
        question=body.question,
        conversation_id=body.conversation_id,
        user_id=user_id,
        filter_context=body.filter_context,
        active_page=body.active_page,
        image_base64=body.image_base64,
    )

    async def _consume_graph() -> AgentState:
        """Consumes the graph's astream, returning its final state."""
        final_state = None
        async for chunk in graph.astream(initial_state, stream_mode=["updates", "values"]):
            kind, data = chunk
            if kind == "values":
                final_state = data
        return final_state

    try:
        final_state = await asyncio.wait_for(_consume_graph(), timeout=GATEWAY_TURN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # TODO(item 12): also write cancel_requested to live_turns here, so a
        # BigQuery call in flight gets the real cancel_job() treatment instead
        # of just being abandoned (docs/build-order.md).
        logger.warning("Turn %s timed out after %ss", body.conversation_id, GATEWAY_TURN_TIMEOUT_SECONDS)
        return AgentResponse(
            answer_markdown="This question took too long to answer. Try narrowing it and asking again.",
            sources=[],
        )
    except Exception:
        logger.exception("Turn %s failed unexpectedly", body.conversation_id)
        return AgentResponse(
            answer_markdown="Something went wrong answering this question. Please try again.",
            sources=[],
        )

    return build_agent_response(final_state)


# --- Endpoints -----------------------------------------------------------

@app.post("/conversation", response_model=ConversationResponse)
async def post_conversation(authorization: str = Header(...), x_api_key: str = Header(...)):
    """Mints a new conversation_id."""
    validate_api_key(x_api_key)
    claims = validate_entra_token(authorization)
    return ConversationResponse(conversation_id=await create_conversation(claims["oid"]))


@app.post("/ask", response_model=AgentResponse)
async def post_ask(
    body: AskRequest, authorization: str = Header(...), x_api_key: str = Header(...)
):
    """Validates the request, then runs one agent turn."""
    validate_api_key(x_api_key)
    claims = validate_entra_token(authorization)
    await assert_owns_conversation(body.conversation_id, claims)
    if len(body.question) > MAX_QUESTION_CHARS:
        raise HTTPException(400, "Question too long -- please shorten it.")
    return await run_agent_turn(body, claims["oid"])
