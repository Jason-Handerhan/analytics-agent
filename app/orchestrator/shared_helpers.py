"""Small helpers shared between the gateway and the orchestrator to enable
potential future refactoring of the gateway into a separate service.
"""
from functools import lru_cache

from google.cloud import firestore
from langchain_core.messages import ToolMessage

from app.config import GCP_PROJECT_ID


# Not a module-level global: constructing this eagerly at import time means
# Layer 1 tests can't import this module without real credentials.
@lru_cache
def get_firestore_client() -> firestore.AsyncClient:
    """Lazy, cached Firestore client."""
    return firestore.AsyncClient(project=GCP_PROJECT_ID)


def live_turn_doc(conversation_id: str):
    """The live_turns document for a turn in flight."""
    return get_firestore_client().collection("live_turns").document(conversation_id)


async def is_cancelled(conversation_id: str) -> bool:
    """Reads live_turns.cancel_requested for a turn in flight."""
    snap = await live_turn_doc(conversation_id).get()
    return bool(snap.exists and snap.to_dict().get("cancel_requested", False))


def label_chartable_result(msg: ToolMessage, ref_id: str) -> ToolMessage:
    """Rebuilds a tool result's message with a visible reference label for
    the model to copy into generate_chart's source_ref."""
    return ToolMessage(
        content=f"Reference id for charting this result: {ref_id}\n{msg.content}",
        name=msg.name, tool_call_id=msg.tool_call_id, status=msg.status,
    )
