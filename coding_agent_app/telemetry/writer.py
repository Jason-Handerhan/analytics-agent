"""Builds and writes the coding_task_telemetry row for a finished task."""
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

from google.cloud import bigquery

from app.coding_task_store import get_coding_agent_task
from app.config import CODING_AGENT_DEFAULT_BASE_BRANCH, CODING_TASK_TELEMETRY_TABLE, GCP_PROJECT_ID, TELEMETRY_DATASET
from coding_agent_app.telemetry.schema import SCHEMA

logger = logging.getLogger(__name__)


@dataclass
class RunDetails:
    """What running a task produced besides its stored result."""
    patch_bytes: bytes | None = None
    tool_calls: list[dict] = field(default_factory=list)    # one {"name", "count", "failed"} per tool the agent used
    agent_llm_calls: int = 0
    reviewer_llm_calls: int | None = None                   # None when the reviewer did not run
    agent_models: list[str] = field(default_factory=list)
    reviewer_models: list[str] = field(default_factory=list)
    langsmith_run_id: str | None = None
    sandbox_snapshot_id: str | None = None


def build_coding_task_row(task: dict, details: RunDetails) -> dict:
    """The telemetry row for a completed task document (its coding_agent_result is set) and the details of its run."""
    plan = task["plan"]
    result = task["coding_agent_result"]
    review = result["review"]
    agent_tokens = result["token_usage"]["agent"]
    reviewer_tokens = result["token_usage"]["reviewer"]     # None when the reviewer did not run

    return {
        "task_id": task["task_id"],
        "conversation_id": task["conversation_id"],
        "user_id": task["user_id"],
        "created_at": task["created_at"],
        "started_at": task["started_at"],
        "completed_at": task["completed_at"],

        "plan_name": plan["name"],
        "plan_goal": plan["goal"],
        "base_branch": plan["base_branch"],
        "is_revision": plan["base_branch"] != CODING_AGENT_DEFAULT_BASE_BRANCH,
        "review_requested": plan["review"],
        "plan_json": json.dumps(plan),

        "outcome": result["outcome"],
        "stop_reason": result["stop_reason"],
        "branch": result["branch"],
        "commit": result["commit"],
        "started_from_commit": result["started_from_commit"],
        "dev_moved": result["dev_moved"],
        "report": result["report"],
        "assumptions": result["assumptions"],
        "blocking_questions": result["blocking_questions"],
        "follow_up_questions": result["follow_up_questions"],
        "patch_problems": result["patch_problems"],
        "result_json": json.dumps(result),

        # a byte that is not valid UTF-8 becomes a replacement character, so the write cannot fail on it
        "patch": details.patch_bytes.decode("utf-8", errors="replace") if details.patch_bytes else None,
        "files_changed": len(result["files"]),
        "lines_added": sum(change["added"] for change in result["files"]),
        "lines_removed": sum(change["removed"] for change in result["files"]),

        "checks": [{"name": check["name"], "passed": check["passed"], "detail": check["detail"]} for check in result["checks"]],
        "checks_failed": sum(not check["passed"] for check in result["checks"]),

        "review_ran": review["ran"],
        "review_verdict": review["verdict"],
        "review_rounds": review["rounds"],
        "review_unverified": review["unverified"],
        "review_explanation": review["explanation"] or None,
        "review_criteria": [{"criterion": criterion["criterion"], "passed": criterion["passed"], "gap": criterion["gap"]}
                            for criterion in review["criteria"]],

        "agent_input_tokens": agent_tokens["input"],
        "agent_output_tokens": agent_tokens["output"],
        "agent_cache_read_tokens": agent_tokens["cache_read"],
        "agent_cache_write_tokens": agent_tokens["cache_write"],
        "agent_models": details.agent_models,
        "reviewer_input_tokens": reviewer_tokens["input"] if reviewer_tokens else None,
        "reviewer_output_tokens": reviewer_tokens["output"] if reviewer_tokens else None,
        "reviewer_cache_read_tokens": reviewer_tokens["cache_read"] if reviewer_tokens else None,
        "reviewer_cache_write_tokens": reviewer_tokens["cache_write"] if reviewer_tokens else None,
        "reviewer_models": details.reviewer_models,
        "agent_llm_calls": details.agent_llm_calls,
        "reviewer_llm_calls": details.reviewer_llm_calls,

        "tool_calls": details.tool_calls,

        "langsmith_run_id": details.langsmith_run_id,
        "cloud_run_revision": os.environ.get("K_REVISION"),
        "sandbox_snapshot_id": details.sandbox_snapshot_id,
    }


@lru_cache
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT_ID)


async def write_coding_task_row(task_id: str, details: RunDetails) -> None:
    """Writes the telemetry row for a task that has just completed. A failure is logged, not raised: the result is already stored."""
    try:
        row = build_coding_task_row(await get_coding_agent_task(task_id), details)
        table_id = f"{GCP_PROJECT_ID}.{TELEMETRY_DATASET}.{CODING_TASK_TELEMETRY_TABLE}"
        # insert_rows returns the rows BigQuery rejected, empty on success; the task id as row id stops a repeated write adding a second row
        rejected = await asyncio.to_thread(get_bq_client().insert_rows, table_id, [row], selected_fields=SCHEMA, row_ids=[task_id], timeout=30)
        if rejected:
            logger.error("coding task %s telemetry row rejected: %s", task_id, rejected)
    except Exception:
        logger.exception("coding task %s telemetry write failed", task_id)
