import asyncio
import base64
import logging
import os
from functools import lru_cache

from fastapi import FastAPI, HTTPException

from app.coding_task_store import TaskNotFound, TaskNotQueued
from app.config import GCP_PROJECT_ID, LANGSMITH_TRACING, get_secret
from coding_agent_app.orchestration import run_coding_task

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("coding_agent_app").setLevel(logging.INFO)  # our own task lines; libraries log warnings and above

app = FastAPI()


@lru_cache
def load_environment() -> None:
    """Put the keys the agent's libraries read into the environment, once per process. Not at import, so importing this module
    needs no credentials."""
    os.environ["ANTHROPIC_API_KEY"] = get_secret("anthropic-api-key", GCP_PROJECT_ID)
    os.environ["LANGSMITH_API_KEY"] = get_secret("langsmith-api-key", GCP_PROJECT_ID)
    os.environ["LANGSMITH_TRACING"] = LANGSMITH_TRACING
    os.environ["LANGCHAIN_PROJECT"] = "coding-subagent"

    # git sends the push token as an authorization header on every request to GitHub; it never goes into a URL or a command line
    push_token = get_secret("github-agent-push-token", GCP_PROJECT_ID).strip()
    credentials = base64.b64encode(f"x-access-token:{push_token}".encode()).decode()
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    os.environ["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"


@app.post("/run/{task_id}")
async def run_task(task_id: str) -> dict:
    """Run one queued coding task and respond when it has finished. A task that ends in an error still responds 200, because its
    outcome is in the stored result; 404 and 409 mean the call did nothing."""
    await asyncio.to_thread(load_environment)
    try:
        await run_coding_task(task_id)
    except TaskNotFound as error:
        raise HTTPException(404, str(error)) from error
    except TaskNotQueued as error:
        raise HTTPException(409, str(error)) from error
    return {"task_id": task_id, "status": "completed"}
