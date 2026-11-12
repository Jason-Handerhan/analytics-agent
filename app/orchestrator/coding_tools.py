import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

import requests
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore, tasks_v2
from google.protobuf import duration_pb2
from langchain_core.tools import tool, InjectedToolArg
from pydantic import BaseModel, Field, model_validator

from app.coding_task_models import CodingAgentPlan
from app.coding_task_store import (
    background_task_doc, get_coding_agent_task, list_coding_agent_tasks, set_coding_task_pr_url, write_coding_agent_task)
from app.config import (
    CODING_AGENT_DEFAULT_BASE_BRANCH,
    CODING_AGENT_INVOKER_SA_EMAIL,
    CODING_AGENT_LOCATION,
    CODING_AGENT_QUEUE,
    CODING_AGENT_SERVICE_URL,
    GCP_PROJECT_ID,
    GITHUB_REPO_BRANCH,
    GITHUB_REPO_NAME,
    GITHUB_REPO_OWNER,
    MAX_REPO_FILE_CONTENT_CHARS,
    get_secret,
)
from app.orchestrator.tools import ToolError
from app.shared_helpers import session_doc

logger = logging.getLogger(__name__)


# --- save_coding_plan -------------------------------------------------------------

SAVE_CODING_PLAN_DESCRIPTION = (
    "Saves a draft of a coding task's plan, to build and revise with the user. The user must approve the plan "
    "before you can start the coding subagent with start_coding_task. "
    "Call it when the user wants a change made to the ML repo's code, and again each time the plan changes: "
    "every save replaces the draft and counts as a new version, and the user must approve each new version. "
    "Set discard to true instead of a plan to delete the draft. "
    "Call it only once per round, and never in the same round as start_coding_task."
)


class SaveCodingPlanArgs(BaseModel):
    plan: CodingAgentPlan | None = Field(
        None, description="The full plan. Every save replaces the previous draft. Leave out when discarding.")
    discard: bool = Field(
        False, description="Set to true instead of a plan to delete the saved draft, for example when the user drops "
                           "the task.")

    @model_validator(mode="after")
    def _exactly_one_of_plan_or_discard(self) -> "SaveCodingPlanArgs":
        """Requires a plan or discard=true, not both and not neither."""
        if bool(self.plan) == self.discard:
            raise ValueError("Set exactly one of plan (to save a draft) or discard=true (to delete the saved draft).")
        return self


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
    """The plan's kind line for this base branch: a new task from dev, or a revision of an existing branch."""
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
    plan: CodingAgentPlan | None = None,
    discard: bool = False,
) -> str:
    if discard:
        try:
            await session_doc(conversation_id).update({"coding_plan": firestore.DELETE_FIELD})
        except GoogleAPICallError as e:
            raise ToolError(f"Could not discard the plan ({e}). Retry once, or tell the user.") from e
        return "Discarded the saved coding plan."
    kind = await check_base_branch(plan.base_branch)
    try:
        doc = session_doc(conversation_id)
        previous = ((await doc.get()).to_dict() or {}).get("coding_plan") or {}
        version = previous.get("version", 0) + 1
        await doc.update({"coding_plan": {"plan": plan.model_dump(), "version": version, "saved_at": datetime.now(UTC)}})
    except GoogleAPICallError as e:
        raise ToolError(f"Could not save the plan ({e}). Retry once, or tell the user.") from e
    return (
        f'{kind} Reviewer: {"on" if plan.review else "off"}.\nSaved "{plan.name}" as version {version}. The user has NOT approved this plan yet. '
        "Show it to the user, starting with the line above, and get their explicit approval before calling "
        "start_coding_task. Every time the plan is saved again it must be shown to the user again for approval; "
        "an earlier approval does not carry over."
    )

save_coding_plan.handle_tool_error = True
save_coding_plan.handle_validation_error = str


# --- start_coding_task ------------------------------------------------------------

START_CODING_TASK_DESCRIPTION = (
    "Kicks off a background coding agent that builds the requirements in the saved coding plan and pushes the "
    "result to a branch. The latest version of the plan must have been approved by the user: shown to them after "
    "the last save_coding_plan call, with their approval given in a later message. Call it only once per round, "
    "and never in the same round as save_coding_plan. The result gives the task id."
)


@lru_cache
def get_tasks_client() -> tasks_v2.CloudTasksAsyncClient:
    return tasks_v2.CloudTasksAsyncClient()


@tool(description=START_CODING_TASK_DESCRIPTION)
async def start_coding_task(
    conversation_id: Annotated[str, InjectedToolArg],
    user_id: Annotated[str, InjectedToolArg],
    turn_started_at: Annotated[datetime, InjectedToolArg],
) -> str:
    doc = session_doc(conversation_id)
    draft = ((await doc.get()).to_dict() or {}).get("coding_plan")
    if not draft:
        raise ToolError("There is no saved coding plan. Draft one with save_coding_plan, show it to the user, and get "
                        "their approval first.")
    if draft["saved_at"] >= turn_started_at:
        raise ToolError(f"Version {draft['version']} was saved this turn, so the user has not approved it. Show it to "
                        "the user and wait for their reply; start it in a later turn.")
    plan = CodingAgentPlan.model_validate(draft["plan"])
    kind = await check_base_branch(plan.base_branch)

    task_id = await write_coding_agent_task(conversation_id, user_id, plan)
    client = get_tasks_client()
    try:
        await client.create_task(
            parent=client.queue_path(GCP_PROJECT_ID, CODING_AGENT_LOCATION, CODING_AGENT_QUEUE),
            task=tasks_v2.Task(
                name=client.task_path(GCP_PROJECT_ID, CODING_AGENT_LOCATION, CODING_AGENT_QUEUE, task_id),
                http_request=tasks_v2.HttpRequest(
                    http_method=tasks_v2.HttpMethod.POST,
                    url=f"{CODING_AGENT_SERVICE_URL}/run/{task_id}",
                    oidc_token=tasks_v2.OidcToken(service_account_email=CODING_AGENT_INVOKER_SA_EMAIL,
                                                  audience=CODING_AGENT_SERVICE_URL),
                ),
                dispatch_deadline=duration_pb2.Duration(seconds=1800),
            ),
        )
    except GoogleAPICallError as e:
        await background_task_doc(task_id).delete()
        raise ToolError(f"Could not queue the task ({e}). The plan is still saved. Retry once, or tell the user.") from e

    check_text = ("It runs in the background, usually a few minutes. When it finishes you will be told at the start of "
                  "a later turn, and you can check on it any time with get_coding_task using this task id.")
    try:
        await doc.update({"coding_plan": firestore.DELETE_FIELD})
    except GoogleAPICallError as e:
        return (f"Started coding task {task_id} ({kind}). The saved draft could not be cleared ({e}). Do not start it "
                "again: call save_coding_plan with discard=true to delete it, and tell the user the task started. "
                + check_text)
    return f"Started coding task {task_id} ({kind}). Tell the user it has started. " + check_text

start_coding_task.handle_tool_error = True
start_coding_task.handle_validation_error = str


# --- get_coding_task --------------------------------------------------------------

GET_CODING_TASK_DESCRIPTION = (
    "Reads the background coding agent's tasks for this conversation. With no task_id, returns a summary list of the "
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
    "what changed from the report and files, give the compare_url, relay the follow_up_questions, and offer a review. "
    "failed: show the failed checks and offer a revision. stopped: explain the stop_reason.\n\n"
    "Options, each only with a task_id: diff returns the branch's full diff from GitHub; include_plan returns the plan "
    "the task was started from; include_todos returns the coding agent's own to-do list.\n\n"
    "The numbers in a coding task (counts, lines, durations) are context, not data."
)


class GetCodingTaskArgs(BaseModel):
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
    "only after the user has asked for a pull request or said yes to your offer of one."
)


class OpenPullRequestArgs(BaseModel):
    task_id: str = Field(description="The coding task to open the pull request for.")


@dataclass
class PullRequestBody:
    """Every part of the pull request description, in the order it displays. A part left empty is left out."""
    summary: str = ""
    changes: str = ""
    checks: str = ""
    reviewer_agent: str = ""
    coding_agent_report: str = ""
    assumptions: str = ""
    follow_up_questions: str = ""
    footer: str = ""

    def render(self) -> str:
        headed_sections = {"Changes": self.changes, "Checks": self.checks, "Reviewer agent": self.reviewer_agent,
                           "Coding agent's report": self.coding_agent_report, "Assumptions": self.assumptions,
                           "Follow-up questions": self.follow_up_questions}
        parts = [self.summary, *[f"## {heading}\n{text}" for heading, text in headed_sections.items() if text], self.footer]
        return "\n\n".join(parts)


def build_pull_request_body(task: dict) -> str:
    """The description of the pull request, built only from the task's stored plan and result."""
    result = task["coding_agent_result"]
    review = result["review"]
    passed_checks = sum(check["passed"] for check in result["checks"])
    body = PullRequestBody()

    # Summary: the goal and the outcome, with no heading
    body.summary = (f"**Goal:** {task['plan']['goal']}\n\n"
                    f"**Outcome:** {result['outcome']}, {passed_checks} of {len(result['checks'])} checks passed")

    # Changes: one line per changed file
    body.changes = "\n".join(f"- `{change['path']}` (+{change['added']} -{change['removed']})" for change in result["files"])

    # Checks: passed or failed for each, with the output of a failed one
    check_lines = []
    for check in result["checks"]:
        check_lines.append(f"- {'passed' if check['passed'] else 'FAILED'}: {check['name']}")
        if check["detail"]:
            check_lines.append(f"```\n{check['detail']}\n```")
    body.checks = "\n".join(check_lines)

    # Reviewer agent: its verdict and the criteria it failed, only when it ran
    if review["ran"]:
        failed_criteria = [f"- {criterion['criterion']}: {criterion['gap']}"
                           for criterion in review["criteria"] if not criterion["passed"]]
        body.reviewer_agent = "\n".join([f"Verdict: {review['verdict']}. {review['explanation']}", *failed_criteria])

    # Coding agent's report
    body.coding_agent_report = result["report"]

    # Assumptions and follow-up questions: empty, and so left out, when there are none
    body.assumptions = "\n".join(f"- {item}" for item in result["assumptions"])
    body.follow_up_questions = "\n".join(f"- {item}" for item in result["follow_up_questions"])

    # Footer
    body.footer = f"---\nOpened from coding task `{task['task_id']}`."
    return body.render()


def create_draft_pull_request(branch: str, title: str, body: str) -> str:
    """Opens the draft pull request and returns its link, or the link of the one already open for the branch."""
    # the PR token, not the read token: it is the only one allowed to open pull requests
    headers = {"Authorization": f"Bearer {get_secret('github-pr-token', GCP_PROJECT_ID)}"}
    pulls_url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/pulls"

    # open a draft pull request from the task's branch into dev
    response = requests.post(pulls_url, headers=headers, timeout=30, json={
        "title": title, "head": branch, "base": CODING_AGENT_DEFAULT_BASE_BRANCH, "body": body, "draft": True})

    # GitHub refuses a second pull request for the same branch with a 422, so the branch already has an open one
    if response.status_code == 422 and "already exists" in response.text:
        
        # find that open pull request by its branch and return its link
        open_pulls = requests.get(pulls_url, headers=headers, timeout=30,
                                  params={"head": f"{GITHUB_REPO_OWNER}:{branch}", "state": "open"})
        open_pulls.raise_for_status()
        return open_pulls.json()[0]["html_url"]

    # any other issue goes back to the agent with GitHub's message.
    if not response.ok:
        raise ToolError(f"GitHub did not open the pull request ({response.status_code}): {response.text}. "
                        "Tell the user what it said.")
    return response.json()["html_url"]


@tool(args_schema=OpenPullRequestArgs, description=OPEN_PULL_REQUEST_DESCRIPTION)
async def open_pull_request(conversation_id: Annotated[str, InjectedToolArg], task_id: str) -> str:
    task = await get_coding_agent_task(task_id)
    
    if task is None or task["conversation_id"] != conversation_id:
        raise ToolError(f"There is no coding task {task_id} in this conversation. Call get_coding_task with no task_id "
                        "to list them.")
    
    if task.get("pr_url"):
        return f"Coding task {task_id} already has a pull request: {task['pr_url']}"
    
    result = task.get("coding_agent_result")
    if result is None:
        raise ToolError(f"Coding task {task_id} is {task['status']}. A pull request can be opened once it has finished.")
    
    if not result["branch"]:
        raise ToolError(f"Coding task {task_id} ended {result['outcome']} and pushed no branch, so there is nothing to "
                        "open a pull request from.")
    try:
        pr_url = await asyncio.to_thread(create_draft_pull_request, result["branch"], task["plan"]["name"],
                                         build_pull_request_body(task))
    except requests.RequestException as e:
        raise ToolError(f"Could not reach GitHub ({e}). Retry once, or tell the user.") from e
    
    await set_coding_task_pr_url(task_id, pr_url)
    return f"Opened a draft pull request into {CODING_AGENT_DEFAULT_BASE_BRANCH}: {pr_url}. Give the user the link."

open_pull_request.handle_tool_error = True
open_pull_request.handle_validation_error = str
