import asyncio
from typing import Any

from deepagents import FilesystemMiddleware, HarnessProfile, RubricMiddleware, create_deep_agent, register_harness_profile
from deepagents.backends import LangSmithSandbox
from deepagents.backends.protocol import ExecuteResponse
from deepagents.middleware.rubric import GRADER_SYSTEM_PROMPT
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware, hook_config
from langchain.agents.structured_output import ProviderStrategy
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langsmith.sandbox import CommandTimeoutError
from pydantic import BaseModel, Field

from coding_agent_app.patch import WORK_DIR, build_patch, read_patch, validate_notebooks, validate_patch
from coding_agent_app.tools import build_dry_run_sql, build_show_changes, get_table_schema, list_tables

MODEL = "claude-sonnet-5-5"
MAX_REPORT_CHARS = 8_000  # the agent's final report
GRADER_CHUNK_CHARS = 3_900  # the framework cuts each message the reviewer reads at 4,000 characters
EXECUTE_TIMEOUT_SECONDS = 120  # the longest one shell command may run

# ExecuteResponse is a command's result (output text and exit code). When a command times out, returning one makes the execute tool
# send the agent a ToolMessage with that text, instead of the timeout error ending the run. 124 is the exit code Linux uses for a timeout.
TIMED_OUT = ExecuteResponse(exit_code=124, truncated=False, output=(
    f"The command ran for more than {EXECUTE_TIMEOUT_SECONDS} seconds and was stopped. Commands here must finish quickly. If it ran tests, "
    "run only the test file you wrote and not the whole suite; if that file is what is slow, make its tests smaller: a few rows built inside "
    "the test, and no model training. Do not run notebooks or training scripts. If a check cannot be made quick, skip it and say in your "
    "report what you could not run."))


# How a shell command reaches the sandbox: the agent calls the execute tool, the tool passes the command (and any timeout the agent
# chose) to the sandbox backend, and the backend runs it in the sandbox with one of two methods, execute (sync) or aexecute (async).
# TimeLimitedSandbox is an exact copy of the library's LangSmithSandbox except for those two methods. Each one caps the timeout, then
# runs the library's original method with the capped value.
class TimeLimitedSandbox(LangSmithSandbox):
    """The sandbox backend with a hard time limit on every command. A command that runs over is stopped and the agent is told in the
    command's output; the library would raise, which ends the whole run."""

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        try:
            # super() is the library's original execute. The timeout it gets is capped.
            return super().execute(command, timeout=min(timeout or EXECUTE_TIMEOUT_SECONDS, EXECUTE_TIMEOUT_SECONDS))
        # the library raises when a command runs over; the agent gets a normal command result instead
        except CommandTimeoutError:
            return TIMED_OUT

    # the same as execute, for the async path
    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        try:
            return await super().aexecute(command, timeout=min(timeout or EXECUTE_TIMEOUT_SECONDS, EXECUTE_TIMEOUT_SECONDS))
        except CommandTimeoutError:
            return TIMED_OUT


SANDBOX_SYSTEM_PROMPT = f"""You are a coding agent working in a clone of a Dataform ML feature pipeline at {WORK_DIR} (definitions/ holds the SQLX, src/ and config/ the training code).

WHERE YOU WORK
You have a shell, and the Dataform CLI is installed. You may change any code file in the repo, but never touch .git/, .github/, dashboard/ or images/, and never change the device settings in its files. Do not run git commit or git push; the changes are collected afterwards. Paths in the task like /definitions are relative to {WORK_DIR}. Search only inside {WORK_DIR}; never run grep or find from /, which walks the whole machine and times out. Put scratch files in /tmp, never in the repo. This sandbox has no GPU and no BigQuery access (the BigQuery tools run outside it), and the repo's training code sets device='cuda' or 'gpu'.

BEFORE YOU BUILD
Read the task and what it points to in the repo before you edit anything. Check that the plan and the repo tell you everything you need to build it correctly. If something essential is missing, stop and finish with blocking_questions instead of guessing. Do this as soon as you notice the gap, before the work or in the middle of it: asking early costs a short run, a wrong guess costs a whole one. Ask only when the answer would change what you build and neither the plan nor the repo settles it. For anything smaller, choose the sensible option, follow the neighboring code, and list it under assumptions.

HOW YOU WORK
Read before you edit. Edit one file at a time: never send several edit_file calls for the same file in one step, because concurrent edits to one path are rejected. Before you edit, call write_todos with one item per concrete step: break each deliverable into its edits and checks. A new column or feature needs a step for every place it is defined, passed through, exposed and used. Add a step for checking each changed .sqlx table with the sqlx skill.

CHECKING YOUR WORK
Verify as you go, not only at the end: after each change to a .sqlx file, follow the sqlx skill (/skills/sqlx/SKILL.md), which compiles the project and checks that the changed SQL runs in BigQuery, and fix any error it reports. A SQL change is not done until that check passes, and it must pass again after your last SQL edit. For Python, run the function you changed on a tiny hand-built DataFrame before writing the test. Fix failures before moving on. Never run a notebook or a training script. Run only the test files you wrote or changed. Before your final answer, compare your todo list with what you changed.

CODE AND TESTS
Write code a person can read top to bottom: plain, idiomatic code in the style of the files around it, descriptive names, and comments only where the logic is not obvious. Keep related logic together in one function that serves one goal; add a helper only when the code is reused or the function would otherwise be hard to follow, and never one whose only job is calling another function. Write the smallest tests that would fail if the behavior broke: call the function with simple inputs and assert the result. Do not parse source files or copy production lists into a test. Keep a test's setup visible in the test; add a fixture only when it makes the tests shorter and clearer. A test runs in a few seconds: never run the training scripts or load data or models in a test. If a test fits a model, use small synthetic data and pass device='cpu' in the test only.

DEPENDENCIES
Do not add a dependency unless the change truly needs it and the value justifies it; never install a package for something you can do without one. If you must add one, add it to requirements.txt: verification runs in a separate sandbox that installs only what requirements.txt lists, and fails without it.

YOUR FINAL ANSWER
You finish with a structured answer: report, assumptions, blocking_questions and follow_up_questions. It is the only thing kept, and the description of each field says what belongs in it."""


# How the grader reviews, added to the middleware's own grader prompt (which keeps its role, injection defenses and verdicts).
GRADER_SYSTEM_PROMPT_WITH_TOOLS = GRADER_SYSTEM_PROMPT + (
    "\n\nStart by calling show_changes: the diff is the evidence for what the agent changed, and the agent's report is only its claim. "
    "Use ls, read_file, glob and grep only to confirm what the changed lines refer to, not to review other code. "
    "The transcript lists each dry run the agent made, which is what BigQuery accepted or rejected."
)


class StructuredCodingAgentResponse(BaseModel):
    """The agent's final answer, in this shape."""
    report: str = Field(description=(
        "Your final report on the finished work, the whole of it every time: if you are sent back to fix something, fix it and write the "
        "entire report again, never \"unchanged\" or \"as before\". Describe only the final state of the work, never reviews, feedback, "
        "rewrites or earlier versions. Say which files you changed and what changed in each, and what you could not run or check. If you added a dependency, say concisely why "
        f"it was needed. Under {MAX_REPORT_CHARS:,} characters."))
    assumptions: list[str] = Field([], description=(
        "Choices you made because neither the plan nor the repo settled them, each as one sentence stating what you assumed, so the user "
        "can check them. Not questions: those go in follow_up_questions."))
    blocking_questions: list[str] = Field([], description=(
        "Questions you must have answered before you can build this correctly: the answer would change what you build, and neither the "
        "plan nor the repo settles it. Write each so the user can answer without reading the code: what you need to know, why it "
        "matters, and the options you see."))
    follow_up_questions: list[str] = Field([], description=(
        "Questions for the user that do not stop the build: you finished the work and want them to confirm or decide something next, such "
        "as whether to apply the same change elsewhere. Not for things you needed before building (blocking_questions) or choices you "
        "made yourself (assumptions)."))


def compact_transcript(messages: list[BaseMessage]) -> list[BaseMessage]:
    """What the grader reads: the task, the agent's latest todo list, its dry runs and its final report as
    plain text. The task and the report are split into several messages because the framework cuts each one at 4,000 characters.
    The diff comes from show_changes."""
    todo_calls = [call for m in messages if isinstance(m, AIMessage) for call in m.tool_calls if call["name"] == "write_todos"]
    if todo_calls:
        todos = "\n".join(f"[{item['status']}] {item['content']}" for item in todo_calls[-1]["args"]["todos"])
    else:
        todos = "(the agent made no todo list)"

    dry_runs = [SystemMessage(f"A dry run by the agent ({m.status}): {m.text}")
                for m in messages if isinstance(m, ToolMessage) and m.name == "dry_run_sql"]

    final_answer = StructuredCodingAgentResponse.model_validate_json(messages[-1].text)  # the last message is the agent's structured answer, as JSON
    task, report = messages[0].text, final_answer.report
    task_parts = [HumanMessage(task[start:start + GRADER_CHUNK_CHARS]) for start in range(0, len(task), GRADER_CHUNK_CHARS)]
    report_parts = [AIMessage(report[start:start + GRADER_CHUNK_CHARS]) for start in range(0, len(report), GRADER_CHUNK_CHARS)]
    return [*task_parts, SystemMessage(f"The agent's latest todo list:\n{todos}"), *dry_runs, *report_parts]


class PatchCheckMiddleware(AgentMiddleware):
    """When the agent says it is done, build its patch and validate it. Fixable problems send the agent back to correct
    them, up to max_retries times. The last patch and violations are kept: those exact bytes are the ones the job ships.
    An answer with blocking questions ends the run instead: nothing was built, so there is no patch to check."""

    def __init__(self, sandbox, sandbox_start_commit: str, max_retries: int = 2):
        super().__init__()                                  # initialize parent class attributes
        self.sandbox = sandbox                              # the agent's sandbox
        self.sandbox_start_commit = sandbox_start_commit    # the commit the sandbox's clone started from
        self.max_retries = max_retries                      # how many times the agent may be sent back
        self.retries = 0                                    # how many times it has been sent back so far
        self.patch_bytes = b""                              # the agent's latest patch
        self.restored_endings = ""                          # output of the line-ending restore
        self.fixable_violations = []                        # problems the agent can fix

        # problems that fail the task; replaced on the first check, and stays if the agent never finishes
        # (for example, it hits the recursion limit)
        self.unfixable_violations = ["The agent did not finish, so its patch was never checked."]

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime):
        answer = state["structured_response"]

        # Blocking questions (needs_input): the agent stopped before building, so skip the patch checks and the reviewer.
        if answer.blocking_questions:
            self.fixable_violations, self.unfixable_violations = [], []
            return {"rubric": ""}  # an empty rubric skips the reviewer

        # Build the patch
        self.patch_bytes, self.restored_endings = await asyncio.to_thread(build_patch, self.sandbox, self.sandbox_start_commit)

        # Validate the patch
        self.fixable_violations, self.unfixable_violations = validate_patch(self.patch_bytes)

        # If there are no unfixable violations, check the patch's notebooks and the final report length
        if not self.unfixable_violations:
            if self.patch_bytes.strip():  # an empty patch has no files

                # notebooks (fixable): each notebook the agent added or changed must be valid notebook JSON
                notebook_paths = [file.path for file in read_patch(self.patch_bytes) if file.path.endswith(".ipynb")]
                self.fixable_violations += await asyncio.to_thread(validate_notebooks, self.sandbox, notebook_paths)

            # Validate the report (fixable): it must not be empty, and must be under MAX_REPORT_CHARS characters
            if not answer.report.strip():
                self.fixable_violations.append("Your report is empty. Finish again with your report, or with blocking_questions "
                                               "if you cannot proceed.")
            elif len(answer.report) > MAX_REPORT_CHARS:
                self.fixable_violations.append(f"Your final report is {len(answer.report):,} characters; the limit is {MAX_REPORT_CHARS:,}. "
                                               "Shorten it, keeping every item the task asks for.")

        # an unfixable problem fails the task whatever else happens, so skip the reviewer's cost
        if self.unfixable_violations:
            return {"rubric": ""}  # an empty rubric skips the reviewer

        # fixable problems and retries left: send the agent back with every problem listed
        if self.fixable_violations and self.retries < self.max_retries:
            self.retries += 1
            problems = "\n".join(f"- {message}" for message in self.fixable_violations)
            feedback = f"A check of your changes found problems you can fix:\n{problems}\nFix them, then finish again with your final answer."
            return {"messages": [HumanMessage(feedback)], "jump_to": "model"}

        # Continue to reviewer if there are fixable problems but no retries left, or if there are no problems at all
        return None


class CountingUsageHandler(UsageMetadataCallbackHandler):
    """Adds up token usage by model, like its parent, and counts the model calls it sees."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        super().on_llm_end(response, **kwargs)
        # the parent's lock, as it uses for its own usage update: two model calls can end at the same time when agents run in
        # parallel (for example parallel subagents). Our loop is not expected to, but the count stays consistent with the parent
        with self._lock:
            self.calls += 1


def build_coding_agent(sandbox, sandbox_start_commit: str) -> tuple:
    """Build the coding agent for one sandbox. Returns the agent, the patch_check middleware (read afterwards for the
    patch and its violations), the list that collects the reviewer's evaluations, and the token usage of the agent and of
    the reviewer."""

    # the framework rejects a second edit to the same file in one response; add rule to tool descriptions
    one_call_rule = ("\n\nDo not call this tool more than once for the same file in a single response: "
                     "combine changes to one file into one call, or make them in separate steps.")
    notebook_rule = "\n\nDo not use this tool on a .ipynb notebook; use the notebooks skill (/skills/notebooks/SKILL.md)."
    execute_rule = (f"\n\nEvery command is stopped after {EXECUTE_TIMEOUT_SECONDS} seconds. This overrides the timeout option: a longer "
                    f"timeout you set is cut to {EXECUTE_TIMEOUT_SECONDS} seconds.")

    # FilesystemMiddleware: adds the file tools (ls, read_file, write_file, edit_file, glob, grep).
    # TimeLimitedSandbox: the backend that runs the tools inside the sandbox, with a time limit on every command.
    # Built here only to read the default tool descriptions.
    default_descriptions = {t.name: t.description for t in FilesystemMiddleware(backend=TimeLimitedSandbox(sandbox)).tools}

    # HarnessProfile: per-model settings for the deep agent; this one appends rules to four tool descriptions.
    register_harness_profile(f"anthropic:{MODEL}", HarnessProfile(tool_description_overrides={
        "execute": default_descriptions["execute"] + execute_rule,
        "read_file": default_descriptions["read_file"] + notebook_rule,
        "edit_file": default_descriptions["edit_file"] + one_call_rule + notebook_rule,
        "write_file": default_descriptions["write_file"] + one_call_rule + notebook_rule}))

    # the agent's tool for checking SQL files against BigQuery; built here because it reads files from this sandbox
    dry_run_sql = build_dry_run_sql(sandbox)

    # the reviewer's tools: the diff of everything changed so far, and read-only file tools (ls, read_file, glob, grep)
    show_changes = build_show_changes(sandbox, WORK_DIR, sandbox_start_commit)
    read_only_tools = [t for t in FilesystemMiddleware(backend=TimeLimitedSandbox(sandbox)).tools if t.name in ("ls", "read_file", "glob", "grep")]
    rubric_evaluations = []

    # CountingUsageHandler: adds up token usage by model and counts the model calls it is attached to.
    # One for the agent and one for the reviewer, so their usage stays apart.
    agent_usage = CountingUsageHandler()
    reviewer_usage = CountingUsageHandler()

    # PatchCheckMiddleware (ours): after the agent finishes, builds the patch from the sandbox and checks it.
    patch_check = PatchCheckMiddleware(sandbox, sandbox_start_commit)

    # TodoListMiddleware: adds the write_todos tool so the agent keeps a task list while it works.
    # RubricMiddleware: when the agent finishes, a grader agent scores the work and sends it back to fix failed criteria.
    #   grader_middleware: middleware for the grader agent itself; AnthropicPromptCachingMiddleware caches its repeated prompt prefix.
    #   prepare_messages_for_grader: shapes the transcript the grader sees.
    #   on_evaluation: called with each grading evaluation.

    agent = create_deep_agent(
        model=ChatAnthropic(model=MODEL, thinking={"type": "adaptive"}, effort="medium", callbacks=[agent_usage]),
        backend=TimeLimitedSandbox(sandbox),
        tools=[list_tables, get_table_schema, dry_run_sql],
        # ProviderStrategy: the model's final message is a StructuredCodingAgentResponse, using the API's native structured output;
        # the parsed object is in the state as structured_response
        response_format=ProviderStrategy(StructuredCodingAgentResponse),
        # after_agent hooks run in reverse list order: patch_check (last) runs first, then the reviewer.
        middleware=[
            TodoListMiddleware(),
            RubricMiddleware(model=ChatAnthropic(model=MODEL, callbacks=[reviewer_usage]), system_prompt=GRADER_SYSTEM_PROMPT_WITH_TOOLS, tools=[show_changes, *read_only_tools], max_iterations=3, grader_middleware=[AnthropicPromptCachingMiddleware()],
                             prepare_messages_for_grader=compact_transcript, on_evaluation=rubric_evaluations.append),
            patch_check,
        ],
        system_prompt=SANDBOX_SYSTEM_PROMPT,
        skills=["/skills/"],  # folders of SKILL.md instructions the agent reads on demand
    )

    return agent, patch_check, rubric_evaluations, agent_usage, reviewer_usage
