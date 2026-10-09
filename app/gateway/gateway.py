import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
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
    SESSIONS_TTL_DAYS,
    STATUS_MAX_NAMED_SOURCES,
    TENANT_ID,
    get_secret,
)
from app.gateway.entry_exit import (
    build_agent_response,
    build_history_messages,
    build_initial_state,
    build_pending_approval,
    build_updated_history,
    rebuild_paused_messages,
)
from app.gateway.models import AgentResponse, AskRequest, CancelResponse, ConversationResponse, StatusResponse
from app.orchestrator.orchestrator import graph, init_orchestrator
from app.orchestrator.shared_helpers import get_firestore_client, live_turn_doc
from app.orchestrator.state import AgentState
from app.telemetry.writer import build_telemetry_row, write_telemetry_row

logger = logging.getLogger(__name__)

GENERIC_TURN_ERROR_MESSAGE = "Something went wrong answering this question. Please try again."
REJECTED_MESSAGE = "You rejected this query. The turn has been cancelled."

app = FastAPI()
_jwks_client = PyJWKClient(
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
)


# --- Lazy getters -------------------------------------------------------

# Not a module-level global: constructing this eagerly at import time means
# Layer 1 tests can't import this module without real credentials
@lru_cache
def get_gateway_api_key() -> str:
    """The gateway's own API key, from Secret Manager."""
    return get_secret("gateway-api-key", GCP_PROJECT_ID)


# --- Auth ----------------------------------------------------------------

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


async def assert_owns_conversation(conversation_id: str, claims: dict) -> None:
    """Raises 404 unless claims owns this conversation."""
    snap = await session_doc(conversation_id).get()
    if not snap.exists or snap.to_dict().get("user_id") != claims["oid"]:
        raise HTTPException(404, "Conversation not found.")


# --- Conversation state ---------------------------------------------------

def session_doc(conversation_id: str):
    """The sessions document for a conversation."""
    return get_firestore_client().collection("sessions").document(conversation_id)


async def create_conversation(user_id: str) -> str:
    """Creates a new sessions document; returns its conversation_id."""
    conversation_id = str(uuid.uuid4())
    await session_doc(conversation_id).set({
        "user_id": user_id,
        "history_messages": [],
        "last_activity_at": datetime.now(timezone.utc) + timedelta(days=SESSIONS_TTL_DAYS),
    })
    return conversation_id


async def fetch_history_messages(conversation_id: str) -> list[dict]:
    """Reads sessions.history_messages -- [] if the doc doesn't exist yet."""
    snap = await session_doc(conversation_id).get()
    return snap.to_dict().get("history_messages", []) if snap.exists else []


async def write_history_messages(conversation_id: str, history_messages: list[dict]) -> None:
    """Writes the turn's updated history back, and bumps last_activity_at
    for the sessions TTL."""
    await session_doc(conversation_id).set(
        {"history_messages": history_messages,
         "last_activity_at": datetime.now(timezone.utc) + timedelta(days=SESSIONS_TTL_DAYS)},
        merge=True,
    )


# --- Live status (live_turns) --------------------------------------------------

SOURCE_NAMES = {
    "run_bigquery_sql": "BigQuery",
    "run_dax_query": "semantic model",
    "search_docs": "project documentation",
    "get_page_info": "dashboard page info",
    "get_measure_dax": "measure definitions",
    "list_repo_files": "code repository",
    "read_repo_file": "code repository",
}

STATUS_BY_NODE = {
    "agent": "Thought",
    "verify": "Verified results",
    "finalize": "Prepared your answer",
}


def join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]


def call_tool_status(tool_names: list[str]) -> str | None:
    """Status for one call_tool batch -- chart and combine take priority, then
    named sources up to STATUS_MAX_NAMED_SOURCES, then a count."""
    if "generate_chart" in tool_names:
        return "Prepared the chart"
    if "combine_results" in tool_names:
        return "Combined results"
    sources = sorted({SOURCE_NAMES[tn] for tn in tool_names if tn in SOURCE_NAMES}) #sorted set
    if not sources:
        return None
    if len(sources) > STATUS_MAX_NAMED_SOURCES:
        return f"Gathered results from {len(sources)} sources"
    return f"Gathered results from {join_names(sources)}"


def get_node_status(node: str, update: dict) -> str | None:
    """Status text for a finished node, or None if it has none."""
    if node == "call_tool":
        # Batch status from the tools it dispatched, ignoring submit_answer.
        msgs = [m for m in update.get("messages", []) if m.name != "submit_answer"]
        if msgs and all(m.status == "error" for m in msgs):
            return "Hit an issue, retrying..."
        return call_tool_status([m.name for m in msgs])
    if node == "verify":
        # A rejected answer writes nothing, so the agent's thought line stays up.
        return STATUS_BY_NODE["verify"] if update.get("verified") else None
    if node == "finalize":
        if update.get("needs_approval"):
            return "Needs your approval"
        if update.get("cancelled"):
            return "Cancelled"
        return STATUS_BY_NODE["finalize"]
    return STATUS_BY_NODE.get(node)


async def log_thinking(live_turns_doc, update: dict, seq: int) -> int:
    """Appends the model's thinking to thinking_log, returning the new seq."""
    msg = update["messages"][0]
    thinking = "\n".join(block["thinking"] for block in msg.content
                         if isinstance(block, dict) and block.get("type") == "thinking").strip()
    if not thinking:
        return seq
    await live_turns_doc.update({"thinking_log": firestore.ArrayUnion(
        [{"seq": seq + 1, "text": thinking}])})
    return seq + 1


async def read_live_status(conversation_id: str) -> StatusResponse:
    """Reads the live_turns document for a turn in flight, or an empty status if none."""
    snap = await live_turn_doc(conversation_id).get()
    if not snap.exists:
        return StatusResponse(status=None, thinking_log=[])
    data = snap.to_dict()
    return StatusResponse(status=data.get("status"), thinking_log=data.get("thinking_log", []))


# --- Turn execution --------------------------------------------------------

async def consume_graph(initial_state: AgentState) -> AgentState:
    """Streams the graph, writing status and thinking to live_turns as each node finishes."""
    live_turns_doc = live_turn_doc(initial_state["conversation_id"])
    await live_turns_doc.set({"status": "Thinking...", "thinking_log": []}, merge=True)
    final_state = None
    seq = 0
    last_mark = time.monotonic()
    try:
        async for chunk in graph.astream(
            initial_state, stream_mode=["updates", "values"], version="v2"
        ):
            # Keep the latest full state for the response and history.
            if chunk["type"] == "values":
                final_state = chunk["data"]
                continue
            # The node that just finished, and what it returned.
            node, update = next(iter(chunk["data"].items()))
            if node == "agent":
                seq = await log_thinking(live_turns_doc, update, seq)
            status = get_node_status(node, update)
            # Skip the write for nodes with no status.
            if status is None:
                continue
            now = time.monotonic()
            await live_turns_doc.update({"status": f"{status} ({now - last_mark:.1f}s)"})
            last_mark = now
    finally:
        # Replace live turns doc with pending approval if needs approval,
        # otherwise delete the live turns doc. Cancelled wins over needs_approval.
        if final_state is not None and final_state["needs_approval"] and not final_state["cancelled"]:
            await live_turns_doc.set({
                "status": "Approval workflow",
                "pending_approval": build_pending_approval(final_state),
            })
        else:
            await live_turns_doc.delete()
    return final_state


async def write_rejected_telemetry(conversation_id: str, user_id: str, pending: dict) -> None:
    """Minimal telemetry row for a rejected approval -- no graph run,
    bytes_consumed/iteration_count stay 0."""
    now = datetime.now(timezone.utc)
    row = build_telemetry_row(
        conversation_id=conversation_id,
        user_id=user_id,
        question=pending["question"],
        answer_markdown=REJECTED_MESSAGE,
        turn_started_at=now,
        turn_completed_at=now,
        filter_context=pending["filter_context"],
        active_page=pending["active_page"],
        tool_calls=[],
        errors=[],
        prompt_tokens=0,
        completion_tokens=0,
        llm_calls=0,
        bytes_consumed=0,
        bytes_consumed_baseline=0,
        iteration_count=0,
        verified=False,
        verification_retry_count=0,
        length_retry_count=0,
        needs_approval=False,
        cost_cap_exceeded=False,
        cancelled=True,
        iteration_cap_hit=False,
        estimated_cost=pending["estimated_cost"],
        pending_queries=pending["pending_queries"],
        largest_pending_query=None,
        approval_decision="rejected",
        deferred_dax=pending["deferred_dax"],
        chart_urls=[],
        suggested_follow_ups=[],
        all_prose_numeric_claims=[],
        clarifying_question="",
    )
    await write_telemetry_row(row)


async def run_agent_turn(body: AskRequest, user_id: str) -> AgentResponse:
    """Runs one turn through the graph; falls back to a plain AgentResponse
    on timeout or error."""
    await init_orchestrator()

    # Handle Live Turns doc first if this is an approval resume to ensure
    # immediate rejection or immediate status update to thinking.
    if body.approval_decision is not None:
        live_doc = live_turn_doc(body.conversation_id)
        snap = await live_doc.get()
        pending = snap.to_dict().get("pending_approval") if snap.exists else None
        if pending is None:
            # Rare enough (the frontend disables double-submits) not to need
            # its own message -- same fallback as any other unexpected failure.
            return AgentResponse(answer_markdown=GENERIC_TURN_ERROR_MESSAGE, sources=[])

        # Approval Workflow: Rejected Branch -- no graph run, minimal telemetry row.
        if body.approval_decision == "rejected":
            await live_doc.delete()
            await write_rejected_telemetry(body.conversation_id, user_id, pending)
            return AgentResponse(answer_markdown=REJECTED_MESSAGE, sources=[])

        # Approval Workflow: set status to thinking (mitigate "Needs your approval" status display)
        await live_doc.set({"status": "Thinking...", "thinking_log": []}, merge=True)

    history_messages = await fetch_history_messages(body.conversation_id)

    # Build initial state for approved resume
    if body.approval_decision == "approved":
        initial_state = build_initial_state(
            question=pending["question"],
            conversation_id=body.conversation_id,
            user_id=user_id,
            filter_context=pending["filter_context"],
            active_page=pending["active_page"],
            image_base64=None,
            history_messages=build_history_messages(history_messages),
        )
        initial_state["messages"] = rebuild_paused_messages(pending["messages"], pending["tool_calls"])
        initial_state["tool_calls"] = pending["tool_calls"]
        initial_state["iteration_count"] = pending["iteration_count"]
        initial_state["bytes_consumed"] = pending["bytes_consumed"]
        initial_state["bytes_consumed_baseline"] = pending["bytes_consumed"]
        initial_state["approved_batch"] = True
        initial_state["approval_decision"] = "approved"

    #Build initial state for a normal turn (not an approval resume)
    else:
        initial_state = build_initial_state(
            question=body.question,
            conversation_id=body.conversation_id,
            user_id=user_id,
            filter_context=body.filter_context,
            active_page=body.active_page,
            image_base64=body.image_base64,
            history_messages=build_history_messages(history_messages),
        )

    #Run the Graph with a timeout, returning a generic error message on any exception
    try:
        final_state = await asyncio.wait_for(consume_graph(initial_state), timeout=GATEWAY_TURN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Turn %s timed out after %ss", body.conversation_id, GATEWAY_TURN_TIMEOUT_SECONDS)
        return AgentResponse(
            answer_markdown="This question took too long to answer. Try narrowing it and asking again.",
            sources=[],
        )
    except Exception:
        logger.exception("Turn %s failed unexpectedly", body.conversation_id)
        return AgentResponse(answer_markdown=GENERIC_TURN_ERROR_MESSAGE, sources=[])

    if not final_state["needs_approval"] and not final_state["cancelled"]:
        await write_history_messages(
            body.conversation_id, build_updated_history(history_messages, final_state)
        )
    return build_agent_response(final_state)


# --- Endpoints -----------------------------------------------------------

@app.post("/conversation", response_model=ConversationResponse)
async def post_conversation(authorization: str = Header(...), x_api_key: str = Header(...)):
    """Mints a new conversation_id."""
    validate_api_key(x_api_key)
    claims = await asyncio.to_thread(validate_entra_token, authorization)
    return ConversationResponse(conversation_id=await create_conversation(claims["oid"]))


@app.post("/ask", response_model=AgentResponse)
async def post_ask(
    body: AskRequest, authorization: str = Header(...), x_api_key: str = Header(...)
):
    """Validates the request, then runs one agent turn."""
    validate_api_key(x_api_key)
    claims = await asyncio.to_thread(validate_entra_token, authorization)
    await assert_owns_conversation(body.conversation_id, claims)
    if len(body.question) > MAX_QUESTION_CHARS:
        raise HTTPException(400, "Question too long -- please shorten it.")
    return await run_agent_turn(body, claims["oid"])


@app.get("/ask/status/{conversation_id}", response_model=StatusResponse)
async def get_ask_status(
    conversation_id: str, authorization: str = Header(...), x_api_key: str = Header(...)
):
    """Reads the live status and thinking log for a turn in progress."""
    validate_api_key(x_api_key)
    claims = await asyncio.to_thread(validate_entra_token, authorization)
    await assert_owns_conversation(conversation_id, claims)
    return await read_live_status(conversation_id)


@app.post("/ask/cancel/{conversation_id}", response_model=CancelResponse)
async def post_ask_cancel(
    conversation_id: str, authorization: str = Header(...), x_api_key: str = Header(...)
):
    """Flags a turn in progress for cancellation -- writes the flag, doesn't
    kill anything directly. The graph checks it cooperatively."""
    validate_api_key(x_api_key)
    claims = await asyncio.to_thread(validate_entra_token, authorization)
    await assert_owns_conversation(conversation_id, claims)
    await live_turn_doc(conversation_id).set({"cancel_requested": True}, merge=True)
    return CancelResponse(cancel_requested=True)
