"""Shared by the coding-agent service (writes) and the gateway (reads): the plan the main agent writes, and the stored task and result."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from app.config import CODING_AGENT_DEFAULT_BASE_BRANCH

# --- the plan: written by the main agent through save_coding_plan, so it is validated ---

class CodingAgentPlan(BaseModel):
    subagent_type: Literal["code", "dashboard"] = Field(description=(
        "'code' changes the ML repo's code: SQLX tables, Python, and notebooks. 'dashboard' changes the Power BI dashboard: its "
        "pages and visuals and its semantic model (measures, columns, tables brought into the model)."))

    name: str = Field(max_length=100, description=(
        "A short title the user will recognize: what it changes and where, such as 'LGBM ranker' or "
        "'Aisle pacing ratio for the ranker'."))

    goal: str = Field(min_length=1, description=(
        "What this change achieves and why, in one or two sentences, stated as the result and not the work. No files, steps or "
        "limits (those go in the description), and no numbers."))

    scope: str = Field(min_length=1, max_length=600, description=(
        "What is changing and where, in a few sentences (for example 'Add a Cost page with cost cards and a cost-by-model chart; "
        "no changes to other pages')."))

    base_branch: str = Field(CODING_AGENT_DEFAULT_BASE_BRANCH, description=(
        f"`{CODING_AGENT_DEFAULT_BASE_BRANCH}` for a new task: the coding agent starts from it and pushes a new branch. To make a "
        "revision of an earlier task, set this to the `branch` in that task's result, copied exactly: the coding agent starts from "
        "that branch and adds its commit to it. Make a revision when the new work needs, fixes or extends that task's changes; "
        f"independent work stays on `{CODING_AGENT_DEFAULT_BASE_BRANCH}`. Ask the user if it is unclear which."))

    review: bool = Field(False, description=(
        "Whether a reviewer checks the finished work and sends the coding agent back to fix what fails. The criteria for each "
        "type are in its guide: read it to tell the user what is checked. It increases token cost and run time, more for a larger "
        "task. Set it yourself: on for larger or riskier changes, off for small ones. When you show the plan, say whether it is "
        "on and why, and what it does. The user approving the plan approves this setting for this plan only: keep it through "
        "every version of the plan unless they change it, and don't raise it again. A different build is a new plan, so choose "
        "and state it again. If the user says they want it on or off for all plans going forward, record that with manage_notes, "
        "then use it for every plan without recommending, until they say otherwise."))

    new_pages: int | None = Field(None, ge=0, le=3, description=(
        "Dashboard plans only: how many new pages this plan adds, at most 3: 0 if it adds none (an edit, a delete or a model-only "
        "change). A larger dashboard is planned in parts, "
        "one plan after another."))

    audience: str | None = Field(None, description=(
        "Dashboard plans only: who uses the dashboard and how. Needed when the plan adds a new page or a new dashboard."))

    description: str = Field(min_length=1, max_length=20000, description=(
        "The plan itself, in markdown, with the information the guide for this type calls for. State each point once. Do not repeat yourself."))

    @model_validator(mode="after")
    def _dashboard_only_fields(self) -> "CodingAgentPlan":
        if self.subagent_type == "dashboard":
            if self.new_pages is None:
                raise ValueError("A dashboard plan needs new_pages: how many new pages it adds (0 if none), at most 3.")
        elif self.new_pages is not None or self.audience is not None:
            raise ValueError("new_pages and audience apply only to a dashboard plan (subagent_type 'dashboard'). Leave them out of a code plan.")
        return self


class CodingPlanDraft(TypedDict, total=False):
    """The conversation's draft coding plan; empty when none is saved or after it is started."""
    plan: dict   # a CodingAgentPlan, as saved by save_coding_plan
    version: int
    saved_at: datetime
    first_saved_at: datetime


# --- the stored task and its result: only our own code writes and reads them, so they are plain dataclasses ---

Outcome = Literal["succeeded", "failed", "needs_input", "stopped"]
# queued, running and completed are written by the coding agent app; the main agent's node moves completed to notified when it first shows the task
TaskStatus = Literal["queued", "running", "completed", "notified"]


@dataclass
class FileChange:
    path: str
    added: int
    removed: int


@dataclass
class FinalCheck:
    """One automated check on the finished patch, run by our code: validation, applying to dev, compile, dependencies, ruff, pytest."""
    name: str
    passed: bool
    detail: str | None = None       # the tail of the output, only when it failed


@dataclass
class ReviewCriterion:
    """One criterion of the reviewer's rubric as the reviewer judged it."""
    criterion: str
    passed: bool
    gap: str | None = None          # what is missing, when it failed


@dataclass
class ReviewEvaluation:
    """The reviewer's final evaluation of the work."""
    ran: bool
    verdict: Literal["satisfied", "needs_revision", "max_iterations_reached"] | None = None
    rounds: int = 0
    explanation: str = ""
    criteria: list[ReviewCriterion] = field(default_factory=list)
    unverified: bool = False        # the reviewer gave no complete per-criterion accounting, so the criteria may be incomplete


@dataclass
class Todo:
    content: str
    status: str


@dataclass
class TokenCounts:
    """Tokens the model provider bills for, summed over a set of model calls."""
    input: int = 0              # everything sent to the model, including the cache_read and cache_write parts
    cache_read: int = 0         # input served from the prompt cache, billed at a discount
    cache_write: int = 0        # input written to the prompt cache
    output: int = 0


@dataclass
class TokenUsage:
    agent: TokenCounts
    reviewer: TokenCounts | None = None


@dataclass
class CodingAgentResult:
    """Written once by the service at the end of a task."""
    outcome: Outcome
    review: ReviewEvaluation
    token_usage: TokenUsage
    # recursion_limit: the agent used all its allowed steps, usually a sign the task is too large
    stop_reason: Literal["recursion_limit", "timeout", "token_budget", "context_too_long",
                         "model_unavailable", "sandbox_error", "internal_error", "cancelled"] | None = None
    report: str = ""
    files: list[FileChange] = field(default_factory=list)
    checks: list[FinalCheck] = field(default_factory=list)
    patch_problems: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    blocking_questions: list[str] = field(default_factory=list)   # needs_input: answers the agent needed before it could build
    follow_up_questions: list[str] = field(default_factory=list)  # the build is done; points the agent wants the user to settle
    todos: list[Todo] = field(default_factory=list)
    branch: str | None = None
    commit: str | None = None
    started_from_commit: str | None = None
    dev_moved: bool = False


@dataclass
class CodingAgentTask:
    """The Firestore document background_tasks/{task_id}. plan holds CodingAgentPlan.model_dump()."""
    task_id: str
    conversation_id: str
    user_id: str
    plan: dict
    created_at: datetime
    status: TaskStatus = "queued"
    progress: str = ""
    cancel_requested: bool = False
    coding_agent_result: CodingAgentResult | None = None
    pr_url: str | None = None       # set when the draft PR is opened
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None   # Firestore's TTL policy deletes the document at this time


# --- what the main agent keeps of a task while it follows it: not stored, built from the task and read only by the main agent ---

class CodingTaskNotification(TypedDict):
    """Enough of a task to show it while it runs and to announce it once it finishes."""
    task_id: str
    name: str
    status: TaskStatus
    progress: str             # the stage line the coding agent app overwrites while the task runs
    outcome: Outcome | None   # set once completed
    branch: str | None        # set once completed, when a branch was pushed
