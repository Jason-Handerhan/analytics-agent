"""Shared by the coding-agent service (writes) and the gateway (reads): the plan the main agent writes, and the stored task and result."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from app.config import CODING_AGENT_DEFAULT_BASE_BRANCH

# Judged by a grader after the agent says it is done; the task is the first message in the transcript.
# Add criteria as new failure types appear.
CODING_RUBRIC = """
Judge only the lines the diff adds or changes, and what those lines refer to. Problems that already exist in code the diff does not touch are not failures. A criterion that does not apply to this task passes. Clearing the saved execution count of a notebook cell that was edited is expected, not a violation.
1. Every deliverable the task asks for is present in the diff, not only described in the agent's report or its todo list.
2. Every column named in an assertion line (such as rowConditions) that the diff adds or changes is produced by that table's own query.
3. Every column or alias that a SQL line added or changed by the diff reads is produced by the table or CTE it comes from, and is in scope where it is used.
4. Every feature name that the diff adds to a Python feature list is a column that definitions/gold/final_ml_features_table.sqlx produces.
5. Every function, import or constant used by the Python lines the diff adds or changes exists.
6. Every caller of a function whose signature the diff changes still works.
7. Every claim the report makes about what the diff changes is true, and it claims no change the diff lacks. Claims about what was run are not judged.
8. The final report lists every file the diff changes and says what changed in each.
9. The diff contains nothing the task did not ask for, and no scratch or debug files.
10. The added tests do not parse source code, copy a production list, run a training script, or read real data or a saved model, and no helper's only job is calling another function.
11. For each .sqlx file the diff changes, the transcript lists a valid dry run of its composed SQL (/tmp/composed/<table>.sql).
"""


# --- the plan: written by the main agent through save_coding_plan, so it is validated ---

class SuggestedCode(BaseModel):
    purpose: str = Field(description="What this code is for and how the coding agent should use it.")
    file: str | None = Field(None, description="The repo file the code would be implemented in, if it belongs to one.")
    code: str


class FileToImitate(BaseModel):
    path: str
    what: str = Field(description="What to follow in this file, specifically: its column documentation block, how it "
                                  "forward-fills, its test style.")


class CodingAgentPlan(BaseModel):
    name: str = Field(max_length=100, description="A short title the user will recognize: what it changes and where, such as "
                                                  "'LGBM ranker' or 'Aisle pacing ratio for the ranker'.")
    
    goal: str = Field(description="What this change achieves and why, in one or two sentences, stated as the result and not the "
                                  "work. No files, steps or limits (those go in requirements and constraints), and no numbers.")
    
    base_branch: str = Field(CODING_AGENT_DEFAULT_BASE_BRANCH, description=(
        f"`{CODING_AGENT_DEFAULT_BASE_BRANCH}` for a new task: the coding agent starts from it and pushes a new branch. To continue an "
        "earlier task, set this to the `branch` in that task's result, copied exactly: the coding agent starts from that branch and adds "
        "its commit to it. Continue an earlier task when the new work needs, fixes or extends its changes; independent work stays on "
        f"`{CODING_AGENT_DEFAULT_BASE_BRANCH}`. Ask the user if it is unclear which."))
    
    requirements: list[str] = Field(min_length=1, description=(
        "The deliverables, one item per thing to build or change, each naming the table, file or model it applies to. Split "
        "anything joined by 'and' that touches more than one place. For example: 'Add user_aisle_pacing_ratio to "
        "prelim_user_aisle_features', 'Forward-fill the new columns in skeleton_features', 'Expose user_aisle_pacing_ratio in "
        "final_ml_features_table and add it to rowConditions', 'Pass the feature to the LGBM ranker only', 'Create a notebook "
        "comparing the ranker with and without the feature, including gain importance'. How to tell it works is not a "
        "requirement (put the test behaviours in tests), and what not to do goes in constraints."))
    
    tests: list[str] = Field([], description=(
        "Only what the compile, the SQL dry runs and linting cannot check: the behaviours the new pytest tests must pin down, each "
        "as one sentence (for example 'select_features returns the feature only for the ranker'). Ask for the fewest tests that "
        "would fail if the behaviour broke; no tests of wiring, training, real data or SQL. Leave empty when the change needs no "
        "new test."))
    
    constraints: list[str] = Field([], description="Rules the change must follow: paths it may or may not touch, naming "
                                                   "conventions, models or tables that must stay as they are, a limit on scope.")
    
    files_to_imitate: list[FileToImitate] = Field([], description="Existing repo files whose style or structure the new code "
                                                                  "should follow.")
    
    decisions: list[str] = Field([], description=(
        "Choices the user made that settle something the agent would otherwise guess (which of two approaches, a name, a "
        "threshold, what is out of scope), each as an instruction with the user's reason if they gave one. If the user dictated "
        "specific logic or SQL, write it here in full. Do not list your own suggestions here; put code you suggest in "
        "suggested_code."))
    
    suggested_code: list[SuggestedCode] = Field([], description=(
        "Code you suggest as a starting point. The coding agent treats it as unverified, and the repo wins on any conflict. Do not "
        "paste code that already exists in the repo; name the file in files_to_imitate instead."))
    
    review: bool = Field(False, description=(
        "Whether a reviewer checks the finished work against the criteria below and sends the coding agent back to fix what "
        "fails. It increases token cost and run time, more for a larger task. Tell the user what it checks when you ask.\n"
        "The criteria:\n" + CODING_RUBRIC))


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
    """One criterion of CODING_RUBRIC as the reviewer judged it."""
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
