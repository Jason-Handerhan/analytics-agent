"""Firestore reads and writes of the coding tasks (background_tasks/{task_id}), shared by the gateway, which creates and reads them,
and the coding agent service, which runs them."""
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

from app.coding_task_models import CodingAgentPlan, CodingAgentResult, CodingAgentTask
from app.shared_helpers import get_firestore_client

TASK_TTL_DAYS = 30  # Firestore's TTL policy deletes a task this long after it was created or completed


class TaskNotFound(Exception):
    """There is no task with that id."""


class TaskNotQueued(Exception):
    """The task is not waiting to run: it is running, finished, or was already picked up."""


def background_task_doc(task_id: str):
    """The background_tasks document for a task."""
    return get_firestore_client().collection("background_tasks").document(task_id)


async def write_coding_agent_task(conversation_id: str, user_id: str, plan: CodingAgentPlan) -> str:
    """Write a new task, queued, and return its id."""
    now = datetime.now(timezone.utc)
    task = CodingAgentTask(task_id=str(uuid.uuid4()), conversation_id=conversation_id, user_id=user_id, plan=plan.model_dump(),
                           created_at=now, expires_at=now + timedelta(days=TASK_TTL_DAYS))
    await background_task_doc(task.task_id).create(asdict(task))
    return task.task_id


async def get_coding_agent_task(task_id: str) -> dict | None:
    """The task document as stored, or None if there is no such task."""
    snapshot = await background_task_doc(task_id).get()
    return snapshot.to_dict() if snapshot.exists else None


# The decorator makes this function a Firestore transaction. The read below and the update at the end commit together or not at
# all, and if the document changed after the read, Firestore throws the attempt away and runs the whole function again.
# So when two calls arrive for one task, the second one reads "running" and is refused.
@firestore.async_transactional
async def _mark_running(transaction, doc, task_id: str) -> dict:
    """Moves a queued task to running and returns it."""
    snapshot = await doc.get(transaction=transaction)
    if not snapshot.exists:
        raise TaskNotFound(f"There is no coding task {task_id}.")
    task = snapshot.to_dict()
    if task["status"] != "queued":
        raise TaskNotQueued(f"Coding task {task_id} is {task['status']}, not queued, so it will not run again.")
    changes = {"status": "running", "started_at": datetime.now(timezone.utc), "progress": "Starting..."}
    transaction.update(doc, changes)
    return {**task, **changes}


async def mark_coding_task_running(task_id: str) -> dict:
    """Mark a queued task as running and return it. Raises TaskNotFound or TaskNotQueued, so a replayed or forged call does nothing."""
    # .transaction() is a fresh, unstarted transaction object for this call. The decorator starts it, runs _mark_running with it,
    # commits it, and runs _mark_running again if the document changed in between.
    return await _mark_running(get_firestore_client().transaction(), background_task_doc(task_id), task_id)


async def update_progress(task_id: str, progress: str) -> None:
    """Overwrite the one-line stage the task is at."""
    await background_task_doc(task_id).update({"progress": progress})


async def complete_coding_task(task_id: str, result: CodingAgentResult) -> None:
    """Write the result and mark the task completed in one update."""
    now = datetime.now(timezone.utc)
    await background_task_doc(task_id).update({"status": "completed", "coding_agent_result": asdict(result), "completed_at": now,
                                               "expires_at": now + timedelta(days=TASK_TTL_DAYS)})


async def mark_coding_task_notified(task_id: str) -> None:
    """Mark a completed task as shown to the main agent."""
    await background_task_doc(task_id).update({"status": "notified"})
