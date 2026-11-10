import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated

import requests
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore
from langchain_core.tools import tool, InjectedToolArg
from pydantic import BaseModel, Field

from app.coding_task_models import CodingAgentPlan
from app.config import (
    CODING_AGENT_DEFAULT_BASE_BRANCH,
    GCP_PROJECT_ID,
    GITHUB_REPO_BRANCH,
    GITHUB_REPO_NAME,
    GITHUB_REPO_OWNER,
    get_secret,
)
from app.orchestrator.tools import ToolError
from app.shared_helpers import get_firestore_client, session_doc

logger = logging.getLogger(__name__)


# --- save_coding_plan -------------------------------------------------------------

SAVE_CODING_PLAN_DESCRIPTION = (
    "Saves a draft of a coding task's plan, to build and revise with the user. The user must approve the plan "
    "before you can start the coding subagent with start_coding_task. "
    "Call it when the user wants a change made to the ML repo's code, and again each time the plan changes: "
    "every save replaces the draft and counts as a new version, and the user must approve each new version."
)


class SaveCodingPlanArgs(BaseModel):
    plan: CodingAgentPlan = Field(description="The full plan. Every save replaces the previous draft.")


def list_candidate_branches() -> list[str]:
    """Every branch on the repo except main and dev, read from GitHub: branches the agent pushed
    and branches a person created by hand."""
    token = get_secret("github-read-token", GCP_PROJECT_ID)
    names: list[str] = []
    page = 1
    while True:
        response = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/branches",
            params={"per_page": 100, "page": page},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
        batch = [branch["name"] for branch in response.json()]
        names += batch
        if len(batch) < 100:
            break
        page += 1
    return [name for name in names if name not in (GITHUB_REPO_BRANCH, CODING_AGENT_DEFAULT_BASE_BRANCH)]


@firestore.async_transactional
async def _save_plan(transaction, doc, plan: CodingAgentPlan) -> int:
    """Writes the draft with the next version number; Firestore reruns it from the read if the document changed first."""
    snapshot = await doc.get(transaction=transaction)
    version = (snapshot.to_dict().get("coding_plan") or {}).get("version", 0) + 1
    transaction.update(doc, {"coding_plan": {"plan": plan.model_dump(), "version": version, "saved_at": datetime.now(UTC)}})
    return version


@tool(args_schema=SaveCodingPlanArgs, description=SAVE_CODING_PLAN_DESCRIPTION)
async def save_coding_plan(plan: CodingAgentPlan, conversation_id: Annotated[str, InjectedToolArg]) -> str:
    if plan.base_branch == CODING_AGENT_DEFAULT_BASE_BRANCH:
        kind = f"NEW TASK: a new branch from {CODING_AGENT_DEFAULT_BASE_BRANCH}."
    else:
        try:
            eligible = await asyncio.to_thread(list_candidate_branches)
        except requests.RequestException as e:
            raise ToolError(f"Could not list the repo's branches from GitHub ({e}). Retry once, or tell the user.") from e
        if plan.base_branch not in eligible:
            raise ToolError(
                f"base_branch '{plan.base_branch}' is not an existing branch. Eligible branches: {eligible}. "
                f"Use '{CODING_AGENT_DEFAULT_BASE_BRANCH}' for a new task."
            )
        kind = f"REVISION: continues branch {plan.base_branch}."
    try:
        version = await _save_plan(get_firestore_client().transaction(), session_doc(conversation_id), plan)
    except GoogleAPICallError as e:
        raise ToolError(f"Could not save the plan ({e}). Retry once, or tell the user.") from e
    return (
        f'{kind}\nSaved "{plan.name}" as version {version}. The user has NOT approved this plan yet. '
        "Show it to the user, starting with the line above, and get their explicit approval before calling "
        "start_coding_task. Every time the plan is saved again it must be shown to the user again for approval; "
        "an earlier approval does not carry over."
    )

save_coding_plan.handle_tool_error = True
save_coding_plan.handle_validation_error = str
