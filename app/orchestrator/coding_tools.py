import asyncio
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Literal

import requests
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore, tasks_v2
from google.protobuf import duration_pb2
from langchain_core.tools import tool, InjectedToolArg
from pydantic import Field, model_validator

from app.coding_task_models import CodingAgentPlan
from app.coding_task_store import (
    background_task_doc, get_coding_agent_task, list_coding_agent_tasks, set_coding_task_pr_url, write_coding_agent_task)
from app.config import (
    CODING_AGENT_DEFAULT_BASE_BRANCH,
    CODING_AGENT_INVOKER_SA_EMAIL,
    CODING_AGENT_LOCATION,
    CODING_AGENT_QUEUE,
    CODING_AGENT_SERVICE_URL,
    CONTEXT_DIR,
    GCP_PROJECT_ID,
    GITHUB_REPO_BRANCH,
    GITHUB_REPO_NAME,
    GITHUB_REPO_OWNER,
    MAX_REPO_FILE_CONTENT_CHARS,
    get_secret,
)
from app.markdown_display_formatting import build_pull_request_body
from app.orchestrator.tools import FlatToolArgs, ToolError
from app.shared_helpers import get_plan_draft, session_doc

logger = logging.getLogger(__name__)


# --- save_coding_plan -------------------------------------------------------------

SAVE_CODING_PLAN_DESCRIPTION = (
    "Saves a draft of a coding task's plan, to build and change with the user, and start with start_coding_task once the "
    "user has approved it. Call it when the user wants a change made to the ML repo's code or to the dashboard. "
    "Before writing or changing a plan, read the guide for its type unless it is already in your context: call it with only "
    "guide set to 'code' or 'dashboard'. It provides the essential information for building the plan in collaboration with the user. "
    "A plan is for one type, never both: when a request needs both, plan one now and the other after it finishes. "
    "The dashboard guide says how to tell whether a dashboard change needs a code plan first. "
    "Settle the plan by working out what you can yourself and asking the user for the rest. "
    "Every save replaces the draft and counts as a new version, so send the whole plan each time. "
    "Save a plan before showing it to the user, and save each change before describing it to the user. "
    "Set discard to true instead of a plan to delete the draft. "
    "Call it only once per round, and never in the same round as start_coding_task or open_pull_request."
)


class SaveCodingPlanArgs(FlatToolArgs):
    plan: CodingAgentPlan | None = Field(
        None, description="The full plan. Every save replaces the previous draft. Leave out when discarding or reading a guide.")
    discard: bool = Field(
        False, description="Set to true instead of a plan to delete the saved draft, for example when the user drops "
                           "the task.")
    guide: Literal["code", "dashboard"] | None = Field(None, description=(
        "Set this alone, with no plan, to read the guide for a type before writing a plan: it provides the essential information "
        "for building the plan in collaboration with the user. 'code' changes the ML repo's code (SQLX, Python, notebooks). "
        "'dashboard' changes the Power BI dashboard or its semantic model."))

    @model_validator(mode="after")
    def _exactly_one_action(self) -> "SaveCodingPlanArgs":
        """Requires exactly one of plan, discard=true or guide."""
        if sum([self.plan is not None, self.discard, self.guide is not None]) != 1:
            raise ValueError("Set exactly one of plan (to save a draft), discard=true (to delete the saved draft) or guide "
                             "(to read the guide for a plan type).")
        return self


# The guide for each plan type, returned by save_coding_plan(guide=...). Read at import: a missing file stops the app from
# starting, like the page-info files.
PLAN_GUIDES = {name: (CONTEXT_DIR / "subagent_plan_guides" / f"{name}.md").read_text(encoding="utf-8")
               for name in ("code", "dashboard")}


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


async def check_base_branch(base_branch: str) -> str:
    """The plan's task type line for this base branch: a new task from dev, or a revision of an existing branch."""
    if base_branch == CODING_AGENT_DEFAULT_BASE_BRANCH:
        return f"NEW TASK: a new branch from {CODING_AGENT_DEFAULT_BASE_BRANCH}."
    try:
        eligible = await asyncio.to_thread(list_candidate_branches)
    except requests.RequestException as e:
        raise ToolError(f"Could not list the repo's branches from GitHub ({e}). Retry once, or tell the user.") from e
    if base_branch not in eligible:
        raise ToolError(
            f"base_branch '{base_branch}' is not an existing branch. Eligible branches: {eligible}. "
            f"Use '{CODING_AGENT_DEFAULT_BASE_BRANCH}' for a new task."
        )
    return f"REVISION: continues branch {base_branch}."


@tool(args_schema=SaveCodingPlanArgs, description=SAVE_CODING_PLAN_DESCRIPTION)
async def save_coding_plan(
    conversation_id: Annotated[str, InjectedToolArg],
    guides_read: Annotated[list[str], InjectedToolArg],
    plan: CodingAgentPlan | None = None,
    discard: bool = False,
    guide: Literal["code", "dashboard"] | None = None,
) -> str:
    if guide:
        return PLAN_GUIDES[guide]
    if plan and plan.subagent_type not in guides_read:
        raise ToolError(f"Plan not saved. Call save_coding_plan with guide set to '{plan.subagent_type}' first, then rewrite the plan "
                        "following the guide and save it again.")
    if discard:
        try:
            await session_doc(conversation_id).update({"coding_plan": firestore.DELETE_FIELD})
        except GoogleAPICallError as e:
            raise ToolError(f"Could not discard the plan ({e}). Retry once, or tell the user.") from e
        return "Discarded the saved coding plan."
    task_type = await check_base_branch(plan.base_branch)
    try:
        doc = session_doc(conversation_id)
        previous = ((await doc.get()).to_dict() or {}).get("coding_plan") or {}
        version = previous.get("version", 0) + 1
        saved_at = datetime.now(UTC)
        await doc.update({"coding_plan": {"plan": plan.model_dump(), "version": version, "saved_at": saved_at,
                                          "first_saved_at": previous.get("first_saved_at", saved_at)}})
    except GoogleAPICallError as e:
        raise ToolError(f"Could not save the plan ({e}). Retry once, or tell the user.") from e
    return (
        f'Saved "{plan.name}" as version {version}. '
        "If the user's latest message explicitly approved the plan as it was last shown and asked for only the changes you "
        "just saved, call start_coding_task next. Otherwise the "
        "user has not seen this version. Show it to them, beginning with this line: "
        f'"{task_type} Reviewer: {"on" if plan.review else "off"}." Then wait for their reply before calling start_coding_task. '
        "An answer to a question you asked, such as turning the reviewer on, is not approval of the plan."
    )

save_coding_plan.handle_tool_error = True
save_coding_plan.handle_validation_error = str


# --- start_coding_task ------------------------------------------------------------

START_CODING_TASK_DESCRIPTION = (
    "Kicks off a background coding agent that builds the requirements in the saved coding plan and pushes the result to a "
    "branch. Call it when the user has explicitly approved the plan, either as last shown to them or with the changes you "
    "just saved at their request. Never call it in the same round as save_coding_plan, open_pull_request or "
    "run_bigquery_sql. The result gives the task id, which you use in later calls and don't quote to the user."
)


@lru_cache
def get_tasks_client() -> tasks_v2.CloudTasksAsyncClient:
    return tasks_v2.CloudTasksAsyncClient()


async def check_start_coding_task(conversation_id: str, turn_started_at: datetime) -> tuple[CodingAgentPlan, str]:
    """The saved plan and its task type line, or a ToolError saying why no task can start."""
    draft = await get_plan_draft(conversation_id)
    if not draft:
        raise ToolError("There is no saved coding plan. Draft one with save_coding_plan, show it to the user, and get "
                        "their approval first.")
    if draft.get("first_saved_at", draft["saved_at"]) >= turn_started_at:
        raise ToolError("This plan was first saved this turn, so the user has not seen any version of it. Show it to "
                        "the user and wait for their reply; start it in a later turn.")
    plan = CodingAgentPlan.model_validate(draft["plan"])
    task_type = await check_base_branch(plan.base_branch)
    return plan, task_type


@tool(description=START_CODING_TASK_DESCRIPTION)
async def start_coding_task(
    conversation_id: Annotated[str, InjectedToolArg],
    user_id: Annotated[str, InjectedToolArg],
    turn_started_at: Annotated[datetime, InjectedToolArg],
) -> str:
    doc = session_doc(conversation_id)
    plan, task_type = await check_start_coding_task(conversation_id, turn_started_at)

    task_id = await write_coding_agent_task(conversation_id, user_id, plan)
    client = get_tasks_client()
    try:
        await client.create_task(
            parent=client.queue_path(GCP_PROJECT_ID, CODING_AGENT_LOCATION, CODING_AGENT_QUEUE),
            task=tasks_v2.Task(
                # named by the task id, so Cloud Tasks refuses a second task with the same name
                name=client.task_path(GCP_PROJECT_ID, CODING_AGENT_LOCATION, CODING_AGENT_QUEUE, task_id),
                # Cloud Tasks POSTs to the coding agent app's run endpoint for this task
                http_request=tasks_v2.HttpRequest(
                    http_method=tasks_v2.HttpMethod.POST,
                    url=f"{CODING_AGENT_SERVICE_URL}/run/{task_id}",
                    # the request is signed as the invoker service account, the only caller the private app accepts
                    oidc_token=tasks_v2.OidcToken(service_account_email=CODING_AGENT_INVOKER_SA_EMAIL,
                                                  audience=CODING_AGENT_SERVICE_URL),
                ),
                # how long Cloud Tasks waits for the app to answer: 30 minutes, the maximum for an HTTP task
                dispatch_deadline=duration_pb2.Duration(seconds=1800),
            ),
        )
    except GoogleAPICallError as e:
        await background_task_doc(task_id).delete()
        raise ToolError(f"Could not queue the task ({e}). The plan is still saved. Retry once, or tell the user.") from e

    check_text = ("It runs in the background, usually a few minutes. The user can follow it in the background tasks window, "
                  "opened with the terminal icon to the left of the model button, below the text box. "
                  "You are notified the moment it finishes, and can check on it any time with get_coding_task using this task id.")
    try:
        await doc.update({"coding_plan": firestore.DELETE_FIELD})
    except GoogleAPICallError as e:
        return (f"Started coding task {task_id} ({task_type}). The saved draft could not be cleared ({e}). Do not start it "
                "again: call save_coding_plan with discard=true to delete it, and tell the user the task started. "
                + check_text)
    return f"Started coding task {task_id} ({task_type}). Tell the user it has started. " + check_text

start_coding_task.handle_tool_error = True
start_coding_task.handle_validation_error = str


# --- get_coding_task --------------------------------------------------------------

GET_CODING_TASK_DESCRIPTION = (
    "Reads the background coding agent's tasks for this conversation. Call it when a message says a coding task "
    "finished, or when the user asks about earlier coding work. With no task_id, returns a summary list of the "
    "conversation's tasks, most recent first (up to 10): task_id, name, goal, status, outcome, branch, pr_url, "
    "completed_at, the changed file paths, and the blocking questions of a task that needs input. With a task_id, "
    "returns that task: its status and progress, and once it has finished, the result.\n\n"
    "Result fields. Verified facts, produced by code and not by an agent: outcome (succeeded, failed, needs_input or stopped), stop_reason (why a "
    "stopped task stopped), files (path, lines added and removed), checks (compile, lint, tests and the like: name and "
    "whether it passed; detail only for a failed one), branch, commit, dev_moved (the base branch changed while the "
    "task ran) and compare_url (where the user reviews the changes on GitHub). The reviewer agent's opinion: review (its "
    "verdict, and for each criterion that failed, what is missing). A separate agent read the changes against a "
    "rubric, so this is its opinion and can be wrong: trust a failed check over the reviewer, and treat a failed "
    "criterion as something to look at, not proof. The coding agent's claims, not verified: "
    "report, assumptions, blocking_questions and follow_up_questions.\n\n"
    "By outcome. needs_input: relay the blocking_questions and ask the user to answer them. succeeded: tell the user "
    "what changed from the report and files, give the compare_url (and the pr_url if a pull request is open), relay "
    "the follow_up_questions, and offer to open a pull request unless one is already open. "
    "failed: show the failed checks and offer a revision. stopped: explain the stop_reason.\n\n"
    "Options, each only with a task_id: diff returns the branch's full diff from GitHub; include_plan returns the plan "
    "the task was started from; include_todos returns the coding agent's own to-do list.\n\n"
    "The numbers in a coding task (counts, lines, durations) are context, not data."
)


class GetCodingTaskArgs(FlatToolArgs):
    task_id: str | None = Field(
        None, description="The task to read. Leave out to get a summary list of this conversation's coding tasks, "
                          "most recent first.")
    diff: bool = Field(
        False, description="With task_id: also return the branch's full diff from GitHub, to review the actual code "
                           "changes.")
    include_plan: bool = Field(
        False, description="With task_id: also return the plan the task was started from, for when the user asks what "
                           "the plan was and it is not in the conversation.")
    include_todos: bool = Field(
        False, description="With task_id: also return the coding agent's own to-do list for the task, which shows the "
                           "steps it took. Set it only if the user directly asks about the steps the coding agent took "
                           "to complete the task.")

    @model_validator(mode="after")
    def _options_need_a_task_id(self) -> "GetCodingTaskArgs":
        """diff, include_plan and include_todos apply to one task."""
        if self.task_id is None and (self.diff or self.include_plan or self.include_todos):
            raise ValueError("diff, include_plan and include_todos need a task_id.")
        return self


def build_task_summary(task: dict) -> dict:
    """One row of the task list: enough to match a task to what the user is asking about."""
    result = task.get("coding_agent_result") or {}
    summary = {
        "task_id": task["task_id"],
        "name": task["plan"]["name"],
        "goal": task["plan"]["goal"],
        "status": task["status"],
        "outcome": result.get("outcome"),
        "branch": result.get("branch"),
        "pr_url": task.get("pr_url"),
        "completed_at": task["completed_at"].isoformat() if task.get("completed_at") else None,
        "files": [change["path"] for change in result.get("files", [])],
    }
    if result.get("outcome") == "needs_input":
        summary["blocking_questions"] = result["blocking_questions"]
    return summary


def build_task_detail(task: dict) -> dict:
    """One task: its status and progress, and once it has finished, the result and the link to review it."""
    detail = {"task_id": task["task_id"], "name": task["plan"]["name"], "status": task["status"], "progress": task["progress"]}
    result = task.get("coding_agent_result")
    if result is None:
        return detail
    review = result["review"]
    detail |= {
        "outcome": result["outcome"],
        "stop_reason": result["stop_reason"],
        "report": result["report"],
        "files": result["files"],
        "checks": result["checks"],
        "review": {
            "ran": review["ran"],
            "verdict": review["verdict"],
            "explanation": review["explanation"],
            "failed_criteria": [{"criterion": criterion["criterion"], "gap": criterion["gap"]}
                                for criterion in review["criteria"] if not criterion["passed"]],
            "unverified": review["unverified"],
        },
        "assumptions": result["assumptions"],
        "blocking_questions": result["blocking_questions"],
        "follow_up_questions": result["follow_up_questions"],
        "branch": result["branch"],
        "commit": result["commit"],
        "dev_moved": result["dev_moved"],
    }
    if result["branch"]:
        detail["compare_url"] = (f"https://github.com/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/compare/"
                                 f"{task['plan']['base_branch']}...{result['branch']}")
    return detail


def fetch_branch_diff(base_branch: str, branch: str) -> str:
    """The unified diff of a branch against its base branch from GitHub. The three-dot form diffs against the merge
    base, so only the branch's own changes show."""
    response = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/compare/{base_branch}...{branch}",
        headers={"Authorization": f"Bearer {get_secret('github-read-token', GCP_PROJECT_ID)}",
                 "Accept": "application/vnd.github.diff"},
        timeout=30,
    )
    response.raise_for_status()
    return response.text


@tool(args_schema=GetCodingTaskArgs, description=GET_CODING_TASK_DESCRIPTION)
async def get_coding_task(
    conversation_id: Annotated[str, InjectedToolArg],
    task_id: str | None = None,
    diff: bool = False,
    include_plan: bool = False,
    include_todos: bool = False,
) -> dict:
    if task_id is None:
        return {"tasks": [build_task_summary(task) for task in await list_coding_agent_tasks(conversation_id)]}
    task = await get_coding_agent_task(task_id)
    if task is None or task["conversation_id"] != conversation_id:
        raise ToolError(f"There is no coding task {task_id} in this conversation. Call get_coding_task with no task_id "
                        "to list them.")
    detail = build_task_detail(task)
    if include_plan:
        detail["plan"] = task["plan"]
    result = task.get("coding_agent_result")
    if (include_todos or diff) and result is None:
        raise ToolError(f"Coding task {task_id} is {task['status']}, so it has no to-dos or diff yet. They exist once it "
                        "has finished.")
    if include_todos:
        detail["todos"] = result["todos"]
    if diff:
        if not result["branch"]:
            raise ToolError(f"Coding task {task_id} pushed no branch, so there is no diff.")
        try:
            diff_text = await asyncio.to_thread(fetch_branch_diff, task["plan"]["base_branch"], result["branch"])
        except requests.RequestException as e:
            raise ToolError(f"Could not read the diff from GitHub ({e}). Retry once, or tell the user.") from e
        if len(diff_text) > MAX_REPO_FILE_CONTENT_CHARS:
            raise ToolError(f"The diff is {len(diff_text):,} characters, over the {MAX_REPO_FILE_CONTENT_CHARS:,} limit. "
                            "Give the user the compare_url to review it on GitHub instead.")
        detail["diff"] = diff_text
    return detail

get_coding_task.handle_tool_error = True
get_coding_task.handle_validation_error = str


# --- open_pull_request ------------------------------------------------------------

OPEN_PULL_REQUEST_DESCRIPTION = (
    "Opens a draft pull request into dev from the branch a finished coding task pushed, and returns its link. Call it "
    "only after the user has asked for a pull request or said yes to your offer of one. Never call it in the same round "
    "as save_coding_plan, start_coding_task or run_bigquery_sql."
)


class OpenPullRequestArgs(FlatToolArgs):
    task_id: str = Field(description="The coding task to open the pull request for.")


def find_open_pull_request(branch: str) -> str | None:
    """The link of the open pull request from this branch, or None. GitHub is asked, not the stored task, since a person can open or close one by hand."""
    response = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/pulls",
        headers={"Authorization": f"Bearer {get_secret('github-pr-token', GCP_PROJECT_ID)}"}, timeout=30,
        params={"head": f"{GITHUB_REPO_OWNER}:{branch}", "state": "open"})
    response.raise_for_status()     # an HTTP error raises, so a failed lookup is not read as "no open pull request"
    open_pulls = response.json()
    return open_pulls[0]["html_url"] if open_pulls else None


def count_commits_ahead(branch: str) -> int:
    """How many commits the branch has that dev does not."""
    response = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/compare/"
        f"{CODING_AGENT_DEFAULT_BASE_BRANCH}...{branch}",
        headers={"Authorization": f"Bearer {get_secret('github-read-token', GCP_PROJECT_ID)}"}, timeout=30,
        params={"per_page": 1})
    response.raise_for_status()     # an HTTP error raises, so a failed lookup is not read as "no commits"
    return response.json()["ahead_by"]


async def get_finished_task(conversation_id: str, task_id: str) -> dict:
    """The coding task, or a ToolError saying why a pull request can't be opened from it: it doesn't exist in this
    conversation, hasn't finished, or pushed no branch."""
    task = await get_coding_agent_task(task_id)
    if task is None or task["conversation_id"] != conversation_id:
        raise ToolError(f"There is no coding task {task_id} in this conversation. Call get_coding_task with no task_id "
                        "to list them.")
    result = task.get("coding_agent_result")
    if result is None:
        raise ToolError(f"Coding task {task_id} is {task['status']}. A pull request can be opened once it has finished.")
    if not result["branch"]:
        raise ToolError(f"Coding task {task_id} ended {result['outcome']} and pushed no branch, so there is nothing to "
                        "open a pull request from.")
    return task


async def check_can_open_pull_request(branch: str) -> None:
    """Raises a ToolError saying why a pull request can't be opened from the branch: GitHub is unreachable, one is
    already open, or the branch has nothing new."""
    try:
        open_pr_url, commits_ahead = await asyncio.gather(
            asyncio.to_thread(find_open_pull_request, branch), asyncio.to_thread(count_commits_ahead, branch))
    except requests.RequestException as e:
        raise ToolError(f"Could not reach GitHub ({e}). Retry once, or tell the user.") from e
    if open_pr_url:
        raise ToolError(f"Branch {branch} already has an open pull request: {open_pr_url}. If this task was a revision, "
                        "its commit is on that branch, so the pull request already includes it. Give the user the link.")
    if commits_ahead == 0:
        raise ToolError(f"Branch {branch} has no commits ahead of {CODING_AGENT_DEFAULT_BASE_BRANCH}, so there is "
                        "nothing to open a pull request for. It may already be merged.")


def create_draft_pull_request(branch: str, title: str, body: str) -> str:
    """Opens the draft pull request and returns its link."""
    # the PR token, not the read token: it is the only one allowed to open pull requests
    headers = {"Authorization": f"Bearer {get_secret('github-pr-token', GCP_PROJECT_ID)}"}
    pulls_url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/pulls"

    # open a draft pull request from the task's branch into dev
    response = requests.post(pulls_url, headers=headers, timeout=30, json={
        "title": title, "head": branch, "base": CODING_AGENT_DEFAULT_BASE_BRANCH, "body": body, "draft": True})

    # an error goes back to the agent with GitHub's message.
    if not response.ok:
        raise ToolError(f"GitHub did not open the pull request ({response.status_code}): {response.text}. "
                        "Tell the user what it said.")
    return response.json()["html_url"]


@tool(args_schema=OpenPullRequestArgs, description=OPEN_PULL_REQUEST_DESCRIPTION)
async def open_pull_request(conversation_id: Annotated[str, InjectedToolArg], task_id: str) -> str:
    task = await get_finished_task(conversation_id, task_id)
    branch = task["coding_agent_result"]["branch"]
    await check_can_open_pull_request(branch)

    try:
        pr_url = await asyncio.to_thread(create_draft_pull_request, branch, task["plan"]["name"], build_pull_request_body(task))
    except requests.RequestException as e:
        raise ToolError(f"Could not reach GitHub ({e}). Retry once, or tell the user.") from e

    await set_coding_task_pr_url(task_id, pr_url)
    return f"Opened a draft pull request into {CODING_AGENT_DEFAULT_BASE_BRANCH}: {pr_url}. Give the user the link."

open_pull_request.handle_tool_error = True
open_pull_request.handle_validation_error = str
