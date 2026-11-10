import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import anthropic
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.errors import GraphRecursionError
from langsmith.sandbox import SandboxClient, SandboxClientError

from app.coding_task_models import (CODING_RUBRIC, CodingAgentPlan, CodingAgentResult, FileChange, ReviewCriterion, ReviewEvaluation,
                                    Todo, TokenCounts, TokenUsage)
from app.coding_task_store import complete_coding_task, mark_coding_task_running, update_progress
from app.config import CODING_AGENT_DEFAULT_BASE_BRANCH, REPO_URL
from coding_agent_app.apply_and_verify import (ApplyPatchResult, FinalChecks, apply_patch_and_push, build_final_checks,
                                               verify_patch_in_clean_sandbox)
from coding_agent_app.coding_agent import PatchCheckMiddleware, StructuredCodingAgentResponse, build_coding_agent
from coding_agent_app.patch import RESTORE_ENDINGS, WORK_DIR, PatchFile, read_patch

logger = logging.getLogger(__name__)

SNAPSHOT_NAME = "coding-agent-snapshot"  # the sandbox snapshot every run starts from
RECURSION_LIMIT = 200
AGENT_TIMEOUT_SECONDS = 20 * 60  # the agent's working time; the sandbox setup and the checks after it are not counted
SKILLS_DIR = Path(__file__).parent / "skills"
SKILL_FILES = {"notebooks": ("SKILL.md", "read_notebook.py", "write_notebook.py", "edit_notebook.py"),
               "sqlx": ("SKILL.md", "compose_query.py")}


class SandboxSetupError(RuntimeError):
    """The sandbox could not be set up for a run: there is no ready snapshot, or the clone failed."""

# Goes ahead of the plan, which is sent to the agent as JSON
TASK_PREAMBLE = """The task below is a plan, as JSON. Build what its requirements list, and follow its decisions: choices the user already made.
The suggested_code was drafted by another agent and never run or compiled; the repo is the source of truth, so if a snippet conflicts with the repo or with a decision, follow those and say so in your report. The files_to_imitate show the style to follow."""


def sum_token_counts(usage_metadata_list: list[dict]) -> TokenCounts:
    """Adds up the usage_metadata dicts of a list of model calls."""
    input_tokens = output_tokens = cache_read = cache_write = 0
    for usage in usage_metadata_list:
        input_tokens += usage["input_tokens"]
        output_tokens += usage["output_tokens"]
        details = usage.get("input_token_details", {})
        cache_read += details.get("cache_read", 0)
        cache_write += details.get("cache_creation", 0) + details.get("ephemeral_5m_input_tokens", 0) + details.get("ephemeral_1h_input_tokens", 0)
    return TokenCounts(input=input_tokens, output=output_tokens, cache_read=cache_read, cache_write=cache_write)


@dataclass
class AgentRun:
    """What running the agent in its sandbox produced, as build_coding_result needs it."""
    final_state: dict                              # the agent's latest state: its messages and todo list
    answer: StructuredCodingAgentResponse          # the agent's final answer; an empty one if it stopped before finishing
    stop_reason: str | None                        # why it stopped before finishing (recursion_limit, timeout); None if it finished
    patch_check: PatchCheckMiddleware              # holds the agent's last patch and its problems
    rubric_evaluations: list                       # the reviewer's evaluation of each grading round
    agent_usage: UsageMetadataCallbackHandler      # the agent's token usage, by model
    reviewer_usage: UsageMetadataCallbackHandler   # the reviewer's token usage, by model
    sandbox_start_commit: str                      # the commit the sandbox's clone started from


def build_coding_result(agent_run: AgentRun, patch_files: list[PatchFile], apply_patch_result: ApplyPatchResult | None,
                        final_checks: FinalChecks | None) -> CodingAgentResult:
    """Maps what a run produced onto CodingAgentResult. final_checks is None when the agent stopped to ask questions."""
    answer = agent_run.answer

    # outcome: stopped if the agent hit a limit, needs_input if it stopped to ask blocking questions, else
    # succeeded or failed by the final checks
    if agent_run.stop_reason:
        outcome = "stopped"
    elif answer.blocking_questions:
        outcome = "needs_input"
    elif final_checks.passed:
        outcome = "succeeded"
    else:
        outcome = "failed"

    # review: the reviewer's last evaluation is its verdict; no evaluations means the reviewer did not run
    if agent_run.rubric_evaluations:
        last_evaluation = agent_run.rubric_evaluations[-1]
        criteria = [ReviewCriterion(criterion=criterion["name"], passed=criterion["passed"], gap=criterion.get("gap"))
                    for criterion in last_evaluation["criteria"]]
        review = ReviewEvaluation(ran=True, verdict=last_evaluation["result"], rounds=len(agent_run.rubric_evaluations),
                                  explanation=last_evaluation["explanation"], criteria=criteria, unverified=last_evaluation["unverified"])
    else:
        review = ReviewEvaluation(ran=False)

    # token usage: the agent's and the reviewer's usage_metadata, each summed
    agent_usage_metadata = list(agent_run.agent_usage.usage_metadata.values())
    reviewer_usage_metadata = list(agent_run.reviewer_usage.usage_metadata.values())
    token_usage = TokenUsage(agent=sum_token_counts(agent_usage_metadata),
                             reviewer=sum_token_counts(reviewer_usage_metadata) if reviewer_usage_metadata else None)

    # files changed, checks, patch problems and the agent's todos
    checks = final_checks.checks if final_checks else []
    files = [FileChange(path=file.path, added=file.added_lines or 0, removed=file.removed_lines or 0) for file in patch_files]
    patch_problems = agent_run.patch_check.fixable_violations + agent_run.patch_check.unfixable_violations
    todos = [Todo(content=todo["content"], status=todo["status"]) for todo in agent_run.final_state.get("todos", [])]

    # where the patch ended up: the branch and commit pushed (None if nothing was pushed), and whether dev moved since the
    # sandbox started (False if the patch was never applied to dev)
    branch = apply_patch_result.branch if apply_patch_result else None
    commit = apply_patch_result.commit if apply_patch_result else None
    dev_moved = apply_patch_result.dev_moved if apply_patch_result else False

    return CodingAgentResult(outcome=outcome, review=review, token_usage=token_usage, stop_reason=agent_run.stop_reason, report=answer.report,
                             assumptions=answer.assumptions, blocking_questions=answer.blocking_questions,
                             follow_up_questions=answer.follow_up_questions, files=files, checks=checks,
                             patch_problems=patch_problems, todos=todos,
                             branch=branch, commit=commit, started_from_commit=agent_run.sandbox_start_commit, dev_moved=dev_moved)


def prepare_sandbox(sandbox, base_branch: str) -> str:
    """Clone the base branch into the sandbox and upload the line-ending script and the skills. Returns the commit the clone
    started from, which every later comparison is made against."""
    clone = sandbox.run(f"git clone --depth 1 --branch {base_branch} {REPO_URL} {WORK_DIR}", timeout=300)
    if clone.exit_code != 0:
        raise SandboxSetupError(f"git clone of branch {base_branch} failed: {clone.stderr.strip()}")
    sandbox.write("/tmp/restore_endings.py", RESTORE_ENDINGS)

    # the skills go outside the clone, so they never appear in the patch
    for skill, names in SKILL_FILES.items():
        sandbox.run(f"mkdir -p /skills/{skill}")
        for name in names:
            sandbox.write(f"/skills/{skill}/{name}", (SKILLS_DIR / skill / name).read_bytes())
    return sandbox.run(f"git -C {WORK_DIR} rev-parse HEAD").stdout.strip()


async def execute_coding_run(task_id: str, task_text: str, review: bool, base_branch: str, new_branch: str | None,
                             is_revision: bool, commit_message: str) -> CodingAgentResult:
    """Run the coding agent on a task and return its result. The agent works in a sandbox; a patch that passes validation is
    applied to the base branch and pushed (to new_branch for a first task, onto base_branch for a revision), then verified in a
    second, clean sandbox. review turns the grader on. Each step writes its stage to the task's progress, and both sandboxes are
    deleted before this returns."""
    client = SandboxClient()
    snapshot_id = next((s.id for s in client.list_snapshots(name_contains=SNAPSHOT_NAME) if s.name == SNAPSHOT_NAME and s.status == "ready"), None)
    if snapshot_id is None:
        raise SandboxSetupError(f"No ready sandbox snapshot named {SNAPSHOT_NAME}: build it before running a task")

    # ---- the agent works in its own sandbox
    await update_progress(task_id, "Starting the sandbox...")
    with client.sandbox(snapshot=snapshot_id) as sandbox:
        sandbox_start_commit = await asyncio.to_thread(prepare_sandbox, sandbox, base_branch)
        agent, patch_check, rubric_evaluations, agent_usage, reviewer_usage = build_coding_agent(sandbox, sandbox_start_commit)

        await update_progress(task_id, "The agent is working...")
        final_state = {}
        stop_reason = None
        try:
            async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
                # stream_mode="values" yields the whole state after every step, so final_state is always the latest
                # an empty rubric turns the grader off
                async for state in agent.astream({"messages": [{"role": "user", "content": task_text}], "rubric": CODING_RUBRIC if review else ""},
                                                 config={"recursion_limit": RECURSION_LIMIT}, stream_mode="values"):
                    final_state = state
        except GraphRecursionError:
            stop_reason = "recursion_limit"
        except TimeoutError:
            stop_reason = "timeout"

    # a stopped run has no structured answer
    answer = StructuredCodingAgentResponse(report="") if stop_reason else final_state["structured_response"]
    agent_run = AgentRun(final_state=final_state, answer=answer, stop_reason=stop_reason, patch_check=patch_check,
                         rubric_evaluations=rubric_evaluations, agent_usage=agent_usage, reviewer_usage=reviewer_usage,
                         sandbox_start_commit=sandbox_start_commit)

    # ---- after the agent: apply the patch, push it, and check it in a clean sandbox

    # The agent stopped to ask questions before building anything: there is no patch to validate, apply or verify,
    # and no checks to run.
    if answer.blocking_questions:
        patch_files, apply_patch_result, final_checks = [], None, None
    else:
        patch_bytes = patch_check.patch_bytes
        patch_problems = patch_check.fixable_violations + patch_check.unfixable_violations
        patch_files = read_patch(patch_bytes) if not patch_problems else []
        apply_patch_result = None
        clean_sandbox_verification = None

        # only a patch that passed validation goes on
        if not patch_problems:
            await update_progress(task_id, "Applying the patch to the base branch...")
            apply_patch_result = await asyncio.to_thread(apply_patch_and_push, patch_bytes, sandbox_start_commit, base_branch,
                                                         new_branch=new_branch, is_revision=is_revision, commit_message=commit_message)
            await update_progress(task_id, "Checking the patch in a clean sandbox...")
            clean_sandbox_verification = await asyncio.to_thread(verify_patch_in_clean_sandbox, client, snapshot_id, patch_bytes,
                                                                 [file.path for file in patch_files], apply_patch_result.dev_moved,
                                                                 sandbox_start_commit, base_branch)
        final_checks = build_final_checks(patch_problems, apply_patch_result, clean_sandbox_verification)

    return build_coding_result(agent_run, patch_files, apply_patch_result, final_checks)


async def run_coding_task(task_id: str) -> None:
    """Run a queued coding task and write its result. Raises TaskNotFound or TaskNotQueued, leaving the task untouched, if it is
    not waiting to run. Once it is running, every ending, an error included, leaves a completed task with a result."""
    
    task = await mark_coding_task_running(task_id) # This also returns the task
    logger.info("coding task %s started", task_id)

    # What the finally writes if the request is cut off before a real result exists
    result = CodingAgentResult(outcome="stopped", review=ReviewEvaluation(ran=False), token_usage=TokenUsage(agent=TokenCounts()),
                               stop_reason="cancelled", report="The service stopped before this task finished. Run the task again.")
    try:
        plan = CodingAgentPlan.model_validate(task["plan"])

        # a new task pushes a branch of its own, named after the plan; a revision adds its commit to the branch it continues
        if plan.base_branch == CODING_AGENT_DEFAULT_BASE_BRANCH:
            # the plan name as lowercase words joined by hyphens; anything but letters and digits is dropped, because git rejects some characters
            words = "".join(character if character.isalnum() else " " for character in plan.name.lower()).split()
            new_branch = f"agent/{'-'.join(words)}-{task_id[:6]}"
            is_revision = False
        else:
            new_branch = None
            is_revision = True

        # the agent is sent the preamble, then the plan as JSON without review and base_branch, which only the service uses
        task_text = f"{TASK_PREAMBLE}\n\n{plan.model_dump_json(indent=2, exclude={'review', 'base_branch'})}"
        commit_message = f"agent: {plan.name} (task {task_id})\n\n{plan.goal}"

        result = await execute_coding_run(task_id=task_id, task_text=task_text, review=plan.review, base_branch=plan.base_branch,
                                          new_branch=new_branch, is_revision=is_revision, commit_message=commit_message)
    #Exception Handling
    except Exception as error:
        logger.exception("coding task %s failed", task_id)
        if isinstance(error, anthropic.APIError):
            stop_reason, advice = "model_unavailable", "The model API failed. Running the task again usually works."
        elif isinstance(error, (SandboxClientError, SandboxSetupError)):
            stop_reason, advice = "sandbox_error", "The sandbox failed. Running the task again usually works."
        else:
            stop_reason, advice = "internal_error", "The service hit an unexpected error."
        result.stop_reason, result.report = stop_reason, f"{advice} ({type(error).__name__}: {error})"

    finally:
        #Always write the result and mark the task completed
        await complete_coding_task(task_id, result)
        logger.info("coding task %s completed: %s", task_id, result.outcome)
