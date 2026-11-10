# Coding agent: turning a finding into a pull request

**Status: designed 2026-11-03; the agent is built and live-tested in the notebook, the port is decided (2026-11-07) and not built.**
The agent, its tools and its skills are in `notebooks/phase6_coding_agent.ipynb` and `coding_agent_app/`; the job, the Firestore task, the
main-agent tools and the PR step do not exist yet ("The port", below). Built before the judge job
(Phase 6 item 2a, `docs/build-order.md`) because the goal is to learn the pattern.

**Terminology (2026-11-07):** the agent runs in a Cloud Run **service** started through Cloud Tasks, not a Cloud Run Job
("The port"). "The job" in this doc means the coding-agent process for one task, whichever way it is started.

## Goal

The main agent finds candidate ML features by working with the user. This lets it hand that work to a coding
agent that drafts the change (first the Dataform SQLX in `definitions/`, later the ML training and evaluation
Python) and leaves a branch for a human to review as a draft pull request.

**The target is the ML model repo** (`GITHUB_REPO_OWNER`/`GITHUB_REPO_NAME` in `app/config.py`, the same repo
the code-search tools read), not this one. It holds both the Dataform SQLX and the ML code. Agent branches start
from its `dev` branch and PRs target `dev`; a human merges into `dev` and later promotes `dev` to `main`. The point of the build is the
framework and pattern (a background coding agent started by a tool), reusable beyond this project.

## Decision

Build our own coding agent with **LangChain Deep Agents**, run as a private **Cloud Run service** that
Cloud Tasks calls once per task (decided 2026-11-07: a job's start latency is unmeasured and reported at minutes, a service's is seconds).
The main agent starts it with a tool and gets a handle back. The job writes its status to Firestore; the main agent reads it. **The
coding agent pushes a branch. The main agent opens the draft PR, after the user confirms.**

Options not taken:
- **One PR tool on the main agent** (`create_pull_request(files, ...)`): simplest and still the fallback, but
  more of what is already built.
- **Hand off to a hosted coding agent through a GitHub issue** (Copilot coding agent, Claude Code GitHub
  Action): quickest, and the handle comes free (the issue or PR number). Rejected for control: Actions logs
  instead of LangSmith traces, and no caps or guardrails of ours. Not a trust objection, since the Claude Code
  action runs in our own Actions and repo content already reaches LLM providers through code search.

## The flow

1. **Explore.** The main agent and the user find candidate features (existing behaviour).
2. **Gather.** The main agent pulls what it needs from `conversation_findings` (`get_conversation_findings`,
   `search_conversation_findings`) and reads the relevant repo files (`get_repo_contents`), and summarizes it.
3. **Draft the plan with the user.** The main agent saves the plan with `save_coding_plan`, shows it in chat, and
   the user edits it; each edit replaces the saved plan. A line in the system prompt and the tool's description
   steer this ("show the plan to the user before starting a task"), and a rule in code backs it (step 4). Showing the
   plan is still a conversation habit, not a guarantee.
4. **Start.** `start_coding_task()` starts exactly the conversation's active plan and returns the task id. The plan id is minted by
   code on the first `save_coding_plan` and kept as the session's `active_plan_id`; later saves replace that plan, and
   the model never types an id (decided 2026-11-07). So the main agent cannot start a task without having saved a plan first
   (decided 2026-11-04). The user knows a plan by its name. **Also refused if the
   plan was last saved in the current turn** (every plan, revisions included): at least one user message
   must come between the last save and the start, so the user gets a turn to see and answer it. The plan
   records the turn it was saved in; the refusal tells the agent to show the plan and wait for the user. This makes it likelier that
   the user sees the plan; it cannot prove they read it.
5. **Follow.** A running task is attached to the prompt each turn (the way notes and the task plan are). A task in status
   `completed` is attached once, with a note to finish the user's question first, then tell them and offer a review. `agent_node` does
   this before it builds each request, not only at the start of the turn, so a task that finishes mid-turn is caught on the next
   model call: it reads the conversation's running tasks (known from the start of the turn, so there is no query when none is
   running), sets any that finished to `notified`, and keeps the note in the turn's state for the rest of the turn (decided
   2026-11-07: the status is the marker, no timestamp or flag; a turn that then fails does not re-announce it, which
   `get_coding_task()` and the system prompt line cover); after that it falls out of the prompt, and `get_coding_task()` with no id lists the conversation's tasks (summary rows),
   so an id is never lost (decided 2026-11-07). The system prompt says to call it when the user refers to
   earlier coding work. The Power Apps client can poll `GET /tasks/{id}`. The chat can't push, so "complete" is seen on the next
   turn or by the badge; `completed` and `notified` both mean finished.
6. **Review.** `get_coding_task(task_id)` returns the result; with `diff=True` it also returns the branch's full diff from
   GitHub ("The port"). The main agent summarizes for the user and gives them the compare link to review the code.
7. **Revise or open the PR.** A change request becomes a new plan built on the last run (below), which starts a
   child task on the same branch; a yes opens the draft PR.

## Components

| Piece | What it is |
|---|---|
| `save_coding_plan(plan)` | Saves or replaces the draft plan (the plan below; its `base_branch` is `dev` for a new task, or the branch of an earlier task's result for a revision) in `coding_plans/{plan_id}`, replacing the whole plan each call, like `manage_tasks`; only the latest plan is kept (a `version` number and the turn it was saved in), and earlier versions can be recovered from the chat or from `get_conversation_findings` with `include_tool_calls` (the ledger keeps every successful `save_coding_plan` call with its full plan) |
| `start_coding_task()` | Takes no arguments; starts the active plan. Snapshots the plan into `background_tasks/{task_id}` (so later edits can't change a running task), creates a Cloud Tasks task for the coding-agent service with the task id, returns the id. Refused if the plan was last saved in the current turn A plan whose `base_branch` is not `dev` continues that branch: refused unless the branch is one a completed task of this user pushed, and while another task is running on it (the main agent types the branch, so a wrong or someone else's must be caught here; the conversation does not matter, a user can revise an earlier conversation's task; the service trusts the task document and does not check) |
| `get_coding_task(task_id=None, diff=False, include_plan=False)` | With no id, lists this conversation's tasks, most recent first (about 10), as summary rows (a dict built in the tool from each stored task, not a class): id, name, goal, status, outcome, parent task, PR link, completion time, the changed file paths, and the questions of a `needs_input` task, enough to match a task to what the user is asking about (the findings tools help with that match; they do not find tasks). With an id, returns the task's `status` and `progress` and, once completed, the `CodingAgentResult` below. With `diff=True`, also the branch's diff from GitHub ("The port"); with `include_plan=True`, the plan snapshot, which a revision needs |
| `coding_plans/{plan_id}` (Firestore) | `conversation_id`, `user_id`, the latest plan with its `version` and `saved_in_turn`, status (`draft` or `started`), with a TTL; the session document holds an `active_plan_id` |
| `open_pull_request(task_id)` | Takes only the id. Opens a **draft** PR using the stored title and body; returns the existing PR if one is already open for that task |
| `background_tasks/{task_id}` (Firestore) | `conversation_id`, `user_id`, the plan snapshot, `status` (`queued`, `running`, `completed`, `notified`), `progress` (a stage line the job overwrites), the `CodingAgentResult` (which holds the outcome), `cancel_requested`, timestamps, with a TTL |
| Coding-agent service (Cloud Run) | Private; `POST /run/{task_id}`, called by Cloud Tasks. Reads its task from Firestore, runs the deep agent, writes progress and the result back |
| `GET /tasks/{id}` | Ownership-checked read of the task document, for the client's poll |

Ownership works like conversations: every tool and endpoint checks the task belongs to the caller's
conversation and `oid`. 404, not 403.

## The plan (a Pydantic model)

The job knows only what the plan says; only the main agent has the conversation. A plan is a saved
object, not an argument passed once at start: it survives across turns (tool results are redacted from history,
a stored plan is not), and while it is a draft it is attached to the prompt each turn through the same
`attach_context` mechanism as notes and the task plan, read only when `active_plan_id` is set; once the task starts it is no longer
attached (the snapshot is in the task, and `get_coding_task(include_plan=True)` returns it). The models are in
`app/coding_task_models.py` (built 2026-11-07); the field descriptions there are the guidance the main agent reads. `CodingAgentPlan` is
Pydantic because the model writes it; the stored task (`CodingAgentTask`) and its result (`CodingAgentResult`, in the task's `coding_agent_result` field) are plain dataclasses, since only our own code reads and writes them
(`asdict()` to write, the stored dict read as is; a Pydantic model there would add validation with nothing to catch). Required:
`name`, `goal` (the result and why, in a sentence or two) and `requirements` (at least one; one deliverable per item, each naming its
table, file or model). Optional: `tests` (the behaviours the new pytest must pin down, few and lean; compile, the SQL dry runs, ruff
and the clean-sandbox checks are the coding agent's by design and need no repeating); `constraints`; `files_to_imitate` (each with a
`what`); `decisions` (what the user settled, including any logic they dictated); `suggested_code` (a purpose, an optional target
file and the code, always unverified: the repo wins on conflict, and code already in the repo is named, not pasted); and `review`,
whether the reviewer runs, false by default, whose field description lists the rubric's criteria so the main agent can tell the user
what is checked. There are no provenance statuses (dropped 2026-11-07: none of the four distinguished anything the coding agent
could rely on). `requirements` is what to build, `tests` how to tell it works, `constraints` what not to do.

Later option: reference a query by `ref_id` (as `generate_chart`'s `source_ref` does) so the SQL is never
retyped and its provenance is provable.

**Guidance for writing a plan** lives in `save_coding_plan`'s description, where `submit_answer`'s formatting
contract already lives. A first plan covers the fields above. A **revision plan** describes only the change from
the previous run, because the branch already holds that work: the user's requested changes, the parts of the
previous result to keep, and any assumption or conflict the previous run flagged that the user has now resolved.
Either way the plan is drafted in chat and shown to the user before it starts. A plan is guided to fit one answer (the
same limit as `submit_answer`, which already splits a long one); it is not enforced, and the draft plan is attached each turn only
until the task starts.

**Strict budget.** On Claude, strict tool schemas share one limit on grammar size (`.claude/rules/tools.md`), so
a structured `save_coding_plan` is likely non-strict, validated at dispatch; a bad call costs one retry. Which
tools need strict most is worth evaluating at some point; `combine_results`, almost never needed, is a candidate
to drop, which would reduce tool count and free a slot. Not decided.
## The result (`CodingAgentResult`, a Pydantic model)

Written once by the job, at the end. Two kinds of field: **facts our code produces** (outcome, files, checks, review, commits,
usage), which the main agent can state, and **the agent's own prose** (report, assumptions, blocking and follow-up questions), which it treats as data
from a model.

```
CodingAgentResult
  outcome: succeeded | failed | needs_input | stopped
  stop_reason                 why a stopped task stopped: recursion limit (the agent used all its steps: the task is probably too large), timeout, token budget, context too long, ...
  report                      the agent's final report (the patch check already enforces its length)
  files: [{path, added, removed}]     per-file line counts, not the diff
  checks: [FinalCheck {name, passed, detail}]   our automated checks on the patch (validation, apply, compile, ruff, pytest);
                                                detail (the tail of the output) only when it failed
  patch_problems: [str]       why a patch was rejected, from the patch check
  review: ReviewEvaluation {ran, verdict, rounds, explanation, criteria: [{criterion, passed, gap}], unverified}
  assumptions, blocking_questions, follow_up_questions   from the agent's structured final output (`StructuredCodingAgentResponse`)
  todos                       as the agent wrote them, always stored (the agent writes the list once and rarely updates it)
  token_usage                 agent and reviewer token counts; in the stored result for telemetry, left out of the main agent's view
  branch, commit, started_from_commit, dev_moved
```

`needs_input`: a background job can't ask the user anything, so it ends early with `blocking_questions`; the main agent relays them and
saves a revision plan with the answers. A finished task can also carry `follow_up_questions`: points that did not stop the build and
the user should settle. A failed outcome with a pushed branch carries the failed checks and their output, so a
revision can target them. There is no stored patch (see "The port").

**The main agent must be told what each field means (decided 2026-11-07).** The result reaches it as tool output, so nothing in the
dataclasses (comments, types) is visible to it, and a Pydantic `Field` description would not be either: those reach a model only as
tool *input* schema. So `get_coding_task`'s description defines every field of the task and the result, separates our facts (outcome,
files, checks, review verdict, commits) from the agent's prose (report, assumptions, questions, todos), and says what to do for each
`outcome`: relay the blocking questions of `needs_input` (and the follow-up questions of a finished task), show the failed checks of `failed` and offer a revision, explain the stop reason of
`stopped`. Written with the tool (step 7 of "The port").

**The review outcome travels with the result (decided 2026-11-06).** The reviewer is optional per plan, so the result
says whether it ran: `review.ran`; when it did, its last `verdict` (`satisfied`, `needs_revision` after the round limit,
`max_iterations_reached`), the number of rounds, whether the grading was `unverified`, its explanation, and every
criterion with `passed` and `gap` (the full final evaluation, not only the failures). The main agent tells the user "reviewed, nothing left", "not
reviewed" or "reviewed, N things left" and shows each unresolved finding with its reason, and the user is the final
review: a finding that does not matter to them is dismissed. An unresolved finding never fails the task by itself. The
gaps are text from a model that read the agent's diff, so the main agent treats them as data, like any tool result. The
same result carries the stop reason and todo list of a stopped task (item 6 of the remaining work) and the agent's full
report.

**Build order: the final result first, live progress later (decided 2026-11-06).** The job keeps every grading round in
memory (the full evaluations) and writes the result once at the end, from that memory, in one atomic update that also
sets `status`, including after a stop. Progress is a separate, later addition: a small overwritten `progress` field,
written by the same sequential writer (the job's streaming loop, which can consume the middleware's custom
`rubric_evaluation_*` events), never from the `on_evaluation` callback, which is synchronous and whose errors are
swallowed, so a write there could be lost or land after the final one. A task killed outright never writes its
result; `progress.updated_at` lets the main agent notice a task that has gone quiet.

The chat shows the summary, the assumptions, the file list and the compare link, not the diff (the main agent reads the diff, it does not paste it): a long diff would hit
the same display-height limit as a long answer (`.claude/rules/orchestrator.md`).

## Revisions

Each run starts fresh, with no replayed history (replaying would need a persistent LangGraph checkpointer, and
there is no free GCP-native one). What carries over: the branch (cloned; the previous commit's diff is
readable), the original plan, the previous result record (summary, assumptions, validation), and the revision
plan (the user's requested changes). LangSmith traces are tagged with the task id and its parent id, for humans following a task
through its revisions.

**Revision or new task.** Nothing in the data says whether a new request continues an earlier task, so the main agent decides when it
drafts the plan: it sets the plan's `base_branch` to the `branch` of the earlier task's result (the task attachment and
`get_coding_task()` show it), or leaves it at `dev`. Code derives the label the user sees: `save_coding_plan` returns "REVISION:
continues branch X, from task Y" or "NEW TASK: new branch from dev" as the first line of its result, which the agent relays; the agent
supplies no revision flag. The branch is the link: there is no parent task id, and the service reads the base branch from the plan. A new
task pushes `agent/<plan name as lowercase words joined by hyphens>-<first six characters of the task id>`; a revision adds its commit to
the branch it continues. `start_coding_task` validates the choice (the branch is one a completed task of this user pushed, and no other task is
running on it). The rule in the `base_branch` field description: continue an earlier task when the new work needs, fixes or extends its
changes; independent work stays on `dev`, because a new task does not see an unmerged branch; ask the user when unclear. Branches are not
deleted automatically (decided 2026-11-09; the user can see branch history), so nothing yet stops continuing a branch whose PR is merged:
open, refuse it once `open_pull_request` stores the PR and the PR's state can be checked.

**Base branch.** Each run fetches the latest `dev` first, and the result records the `dev` commit it started
from. A revision merges or rebases the agent's branch onto `dev` before changing anything and reports any
conflict it can't resolve under assumptions. Work in other open PRs isn't visible to it.

**Later, not decided:** the code-search tools read `GITHUB_REPO_BRANCH = "main"` (`app/config.py`) while the
coding agent works from `dev`. While the branches match it makes no difference; once they drift, a plan written
from `main`'s code could be wrong for `dev`. The intent is to point the main agent at `dev` too.

## Guardrails

- **Branch-only token for the job, PR-only token for the main agent**, each its own Secret Manager secret
  scoped to the ML model repo. The job's service account is separate from `agent-sa`. It has no write access anywhere and no access to `agent_safe`, so the
  `agent_safe` boundary is untouched; it can read the three ML pipeline datasets (the dry-run tool needs that, decided
  2026-11-06), and the tools return no rows.
- **Always a draft PR into `dev`; never merged by the agent.** Branch protection on `dev` and `main` (humans
  merge into `dev` and promote `dev` to `main`), and agent branches prefixed `agent/`. The chat "yes" is a courtesy; the real gate is review in GitHub. An in-app hard gate
  (generalizing the approval pause to a PR card) is possible later.
- **The PR title and body come from the stored result,** not free text from the main agent. The verification
  contract only covers `answer_markdown`, so a PR description is a route for ungrounded claims otherwise.
- **Path policy: a blocked list, not a `definitions/` allowlist (decided 2026-11-04).** The agent may change any
  code file in the repo, the SQLX and the Python in `src/`, `config/` and `scripts/`; a change to a blocked path
  fails the task. The default blocked list is `.git/`, `.github/` (CI workflows), `dashboard/` and `images/` (the
  Power BI report and binary assets), and secret-looking files (`.env`, `*.pem`, `*.key`,
  `service_account.json`). The policy is checked in the job on the change list, not baked into extraction, and the
  list is a constant to adjust. Deep Agents' `permissions` (`FilesystemPermission`: operations, paths, mode
  allow/deny/interrupt; first match wins, no match means allowed) apply to `FilesystemBackend` file tools only, not
  to a sandbox backend, so with the sandbox the job-side check is the enforcement. The restricted token and CI at
  the PR remain a second layer.
- **Bounded cost and time:** LangGraph recursion limit, a token budget, the run's timeout (30 minutes on Cloud Run and Cloud Tasks, an internal 25 so a result can still be written, one attempt; see "The port"), and one running task at a time across all users (the cap proposed under the parked entries,
  which also covers one per branch). Cancellation reuses the flag pattern (`cancel_requested`,
  with an execution cancel as the hard stop).
- **Never auto-merge.** The agent never sees the real data; it can't verify a feature's value, only the
  user can, in the training notebooks.

## Sandbox

The agent loop runs in the coding-agent Cloud Run service, held open by the Cloud Tasks request for the whole run (request-based
Cloud Run throttles CPU after the response, so the run must be an in-flight request). **What the agent's tools run in is a separate question, decided
2026-11-04: a hosted sandbox (LangSmith), not the job container** (Running tests and code). A provider sandbox
only moves the agent's file and command execution; the loop stays in our process. Isolation strength is comparable
to the container's; what matters is what the agent can reach from where its shell runs: credentials, the service
account token on the metadata server, the harness code and open network egress. Those are the reasons the shell
runs in the sandbox and not in the job container.

**The agent gets no shell in the job container.** A `FilesystemBackend` with the path rules lets it read and
edit files only. Our own job code
commits and pushes after the agent finishes, so the agent never holds the git token. The service account is
minimal; the subprocess environment is stripped. Running code is a separate question (Running tests and code).

## Checks and validation

**Status (2026-11-04):** with the sandbox as the agent's backend (Running tests and code, pattern A) the agent has
a shell in the sandbox and runs `dataform compile` itself, so `run_check` is not needed for that; it stays the
design for pattern B, a tighter agent with no shell. Our own checks (the independent compile check) are described there.

Instead of a shell, the agent gets one tool, `run_check(name)`, where `name` is a fixed list. Our code maps each
name to a fixed command, run in the clone with a timeout, a minimal environment and a cap on output size.
Checks: `dataform_compile` (needs the `@dataform/cli` package, a Node.js tool installed with `npm`, not
`uv`; locally `npm install -g @dataform/cli`, in the job image see Job image), `python_lint`, and `pytest`. The
checks to run for a change are chosen in code from a file-type map (`.sqlx` and `workflow_settings.yaml` to
compile; `.py` to lint and tests), not by the model. The compile check can also return the compiled SQL for a
named table (refs resolved to full table names), which the dry-run tool below needs.

- **Loop-back.** A failing check returns its output as the tool result, so the agent fixes and reruns, with a
  cap on failed attempts.
- **Final gate.** After the agent finishes, the job runs the checks itself and records the real outcome in the
  result's validation field. The agent's "it compiles" is never trusted. If a check still fails, the status is
  `failed` with the last output and the branch is left for inspection; it is not offered as a PR.
- **Dataform runs JavaScript at compile time** (`${...}`, `js {}`, the `config` block, `includes/`), so compiling
  the agent's SQLX executes code it wrote. The existing `definitions/` has none of `js {` or `require(`, and the
  job rejects any agent diff that adds them, as defence in depth; a blocklist is not a boundary, because a `${}`
  expression can reach the Node process without writing `require(`.
- **Static versus executing checks.** A linter and `py_compile` don't run the agent's code and are safe in the
  job container. `dataform compile`, `pytest` and any scratch script do (the compiler evaluates the SQLX's
  JavaScript, tests import the source the agent just edited), so they never run in the job container, only in a
  sandbox (Running tests and code).
- **Tests.** The ML repo has no test suite yet. Add one as a PR (a good first coding-agent task): pure
  functions with small synthetic DataFrames, and a **contract test** comparing the columns
  `final_ml_features_table.sqlx` produces with the feature lists in `config/config.py` and the training code, so
  a feature added to the SQL but not wired into training (or the reverse) fails. `tests/**` is read-only for the
  agent at first, so it can't weaken a test to pass it.
- **Schema (decided 2026-11-03: a live tool, not a snapshot).** Two tools, `list_tables(dataset)` and
  `get_table_schema(table)`, call the BigQuery client's table metadata endpoints (no query job, no free-form SQL,
  no cost) and accept only an allowlist of datasets held in code: **`instacart`** (raw sources plus the
  `cleansed_*` and `order_products_cleansed` staging tables), **`building_blocks`** and
  **`ml_model_feature_table`** (the schema names in the SQLX configs). The job's service account is new, with
  `roles/bigquery.metadataViewer` on those three datasets only: names, types and descriptions, never rows. IAM
  decides what exists for the job; the code decides what the tool accepts. **Not `agent_safe`:** that is the main
  agent's dataset and says nothing about the tables the coding agent edits. The agent also reads the repo's SQL
  for the tables the project defines; the live tool adds real types, the source tables and what is materialized.
- **No aggregate check by the main agent.** The main agent can't read these tables (`agent_safe` excludes the ML
  pipeline datasets), so it can't validate the feature's values.

**What compile can and can't catch.** Compile resolves `ref()` and templating but doesn't send SQL to BigQuery.
Tested 2026-11-04 on the real Dataform CLI in throwaway copies: a `ref()` to a table that doesn't exist fails
(exit 1, "Could not resolve" and "Missing dependency"); an undefined alias passes with 0 errors. The main agent's
draft had exactly that error (`hist.` used in the outer `SELECT`, where only `temp_ml` is in scope).

**Testing the SQL itself.** A plain dry run of a changed gold query fails, because the upstream silver table
hasn't been rebuilt with the new columns, so it can only test the silver layer. Decided 2026-11-04: a
`dry_run_sql(sql_file)` tool (changed from taking the SQL itself, 2026-11-06), with the technique in guidance and not in rewriting code:

- **Enforced in the tool (built 2026-11-06 in `coding_agent_app/tools.py`):** `dry_run=True` set unconditionally, no parameter
  to turn it off, and a Layer 1 test on the job configuration; it returns JSON with only validity, the error text, the
  statement type, the output columns and types, and the bytes estimate, never rows. Inputs are Pydantic schemas: the
  dataset is a `Literal` of `CODING_AGENT_DATASETS`, so the model sees the allowed datasets as an enum and anything else is
  rejected before the tool runs. Decided against a cap on query length, a bytes cap and a statement-type check: BigQuery
  limits query size itself, a forced dry run bills nothing, and the statement type is reported in the result so a
  multi-statement script shows as `SCRIPT` (BigQuery validates only its first statement) instead of being refused.
  Failures reach the agent as error messages and never stop the run: every tool raises `ToolException` and sets
  `handle_tool_error = True`, so the tool message has `status="error"` (an invalid query: BigQuery's message; a missing
  table: the tables that exist; a service, access or credentials problem: "retry once, or carry on"). Checked on the
  agent's own tool node, 2026-11-06: a bad argument is already turned into an error message by the framework, but a
  `ToolException` without the flag, or any other exception, propagates and stops the run, unlike the main agent, whose own
  loop catches `ToolError`. The dry run is never needed for a good result. The tools are module-level and imported, like
  the main agent's, with `get_bq_client()` patched by tests and by the notebook (which impersonates the account); the record of
  dry runs is read from the tool messages, so no tool keeps state.
- **Guidance:** to test a changed table whose upstream tables are also changed, replace each reference to an
  upstream table with that table's query as a subquery, then dry-run the result, with the skeleton using the new
  silver query as the worked example. The agent gets the compiled SQL from the compile check.
- **Advisory, not a gate.** The agent writes the test query, so the job can't re-run it to confirm the agent's
  claim. Each dry-run call and its result are recorded in the task record, and the result marks the dry-run
  outcome "composed by the agent". Compile stays the deterministic gate.
- **Confirmed 2026-11-06:** a dry run needs read permission (`bigquery.tables.getData`) on the tables a query references
  (access map below); the tool can't return rows, which is why it is accepted. It validates structure only.

**Access map for the dry run (2026-11-06; settle before building `dry_run_sql`).** Never any write permission: no table
create, update or delete, so Dataform's `CREATE TABLE ... AS SELECT` is never run by the job, and the tool dry-runs only the
compiled `SELECT` body (`dataform compile --json` gives it as the table's `query`). `list_tables` and `get_table_schema`
need `roles/bigquery.metadataViewer` on the three datasets. `dry_run_sql` needs `bigquery.jobs.create`
(`roles/bigquery.jobUser`, project level); BigQuery's documentation says running a query also needs
`bigquery.tables.getData` on every table and view it references (and on a view's underlying tables), and does not say whether
a dry run is exempt. **The open question: does a dry run succeed with metadata access only?** **Answered 2026-11-06: no (outcome B).** Run through
impersonation in `notebooks/phase6_dry_run_access.ipynb` with only `metadataViewer` and `jobUser`, every dry run was denied with
"User does not have permission to query table", while listing tables and reading schemas worked and `agent_safe`,
`telemetry` and `agent_chat_history` were denied. So a dry run needs `bigquery.tables.getData` on the referenced tables.
Decided: accept it, with the tool rules below (the tool never executes and returns no rows, and the account has no write
role), and grant `roles/bigquery.dataViewer` on the three datasets only. The replica-table and drop-the-tool options stay on
record. How the test was set up: test it from the notebook by
impersonating the new service account (the notebook identity needs `roles/iam.serviceAccountTokenCreator` on it), so the
IAM boundary is exercised and not the developer's own wide access. Outcomes: (A) it works: no row access and no write access.
(B) it needs `getData`: the service account could read rows (never write), though nothing returns them to the agent; then
accept with mitigations, dry-run against empty replica tables in a separate project (same dataset and table names, schemas
only, so `getData` exposes nothing; costs a project and a schema sync), or drop the tool, since criteria 2 to 4 of the
reviewer already catch the main bug class (an undefined column or alias). Tool rules either way: `dry_run=True` and a maximum
bytes billed forced in code and covered by a Layer 1 test on the job configuration; a single `SELECT` or `WITH` statement
only (a dry run of a script validates only its first DDL statement and skips the rest), checked again from the statement
type in the result. The agent's own Python cannot reach BigQuery: it runs in the sandbox, which holds no credentials, and
the job process never runs agent-written code; the only agent-controlled input to an identity-bearing call is the SQL
string.

**The job's service account (2026-11-06): `coding-agent-sa`** in `instacart-ml-model`, separate from `agent-sa` (the commands are in the
setup guide, Step 19). Created first
with only what the tool experiment needs: `roles/bigquery.metadataViewer` on `instacart`, `building_blocks` and
`ml_model_feature_table` (dataset-level `GRANT ... ON SCHEMA`), `roles/bigquery.jobUser` on the project, and
`roles/iam.serviceAccountTokenCreator` on the account for the developer, so the notebook can impersonate it. The IAM Credentials
API is enabled for that. Still to add for the running job: `roles/secretmanager.secretAccessor` on specific secrets
(the Anthropic and LangSmith keys and the new branch-only GitHub token), `roles/datastore.user` for the task document, and
for the gateway's service account, permission to create tasks in the queue (and to act as the invoker account below). `roles/bigquery.dataViewer` on the three
datasets is added only if the dry-run experiment shows a dry run needs it.

**Validation chain.** (1) Compile, automatic. (2) The patch validation (blocked paths, symlinks and
submodules, binary files, size limits). (3) The advisory dry run. (4) Human review of the draft PR. (5) A Dataform run of the `dev`
branch with its assertions (`rowConditions`) before anything reaches `main`; that is the only step that tests the
feature's values.

## Planning, deletes, scratch files and the final report (decided 2026-11-04)

- **Planning tool.** LangChain's `TodoListMiddleware` is added (a `write_todos` tool; not in the default tools of
  this Deep Agents version). It is soft guidance: a system prompt that tells the model to plan complex work, mark
  items done as it goes and revise, and skip it for simple tasks, with `write_todos` allowed at most once per
  model turn. One line in our prompt: for a change touching more than one file, write the todo list first.
- **Deletes stay available.** The `delete` tool is the only way to remove a file, and refactors and renames
  need it. A deterministic change summary is built by the job from `git diff --name-status -M` (added, modified,
  deleted, renamed with old and new names), not from the agent's report, and appears in the result, in the
  main agent's chat summary and in a generated section of the PR body. If the diff has a deletion or rename the
  plan never mentioned, the result is flagged `unplanned_deletions` and the main agent raises it with the user
  before offering the PR. With the sandbox backend the permission rules don't apply (Guardrails), so the
  blocked-path check on the change list is what stops a delete of a blocked path.
- **Scratch files** (not needed with the sandbox, 2026-11-04): the agent's notes and draft SQL can go anywhere
  outside the clone (for example `/tmp`), which the patch never includes. The in-memory `CompositeBackend` route
  is dropped.
- **Final report.** `response_format` with a small model (summary, conflicts, assumptions) gives the report a
  fixed shape. The validation and conflict fields of the result still come from the checks and the diff.
- **Considered, not adopted.** Checkpointers and `interrupt_on` (a persisted state has a cost we don't want, and
  an approval would have to travel back through the main agent). `AsyncSubAgent` (background tasks on a remote
  Agent Protocol server, LangGraph Platform or self-hosted, with start, check, update, cancel and list tools):
  our Cloud Run service plus Firestore is simpler to run, and the main agent is not a deep agent. Candidates for
  later: `RubricMiddleware` (beta; a grader model scores the work against criteria the plan supplies, advisory
  and costlier than the deterministic checks), `memory` and more skills (the Dataform conventions as `SKILL.md` or
  `AGENTS.md` files in the ML repo, which would have closed the assertion gap; a decision for the repo owner; a
  skill is now used for notebooks, see Notebooks),
  and a read-only reviewer subagent.
- **`needs_input`, final touch.** The agent gets a small tool that ends the run with a question (LangChain tools
  can return directly; unverified in this version). The job commits the work in progress to the agent branch
  and stores the question in the result. The main agent relays it, the user answers, and a revision plan carries
  the answer; the next run starts from the branch. No saved state, so no checkpointer.

## Running tests and code

Agent-written code runs only outside the job container: the compiler and `pytest` execute what the agent wrote,
and the job container holds the model keys, the git token and a service account token.

**Decided 2026-11-04: pattern A on the LangSmith sandbox.** A vendor sandbox is the agent's backend, so the agent
has a real shell there and runs `dataform compile` and scripts itself; the agent loop, the git token and the model
keys stay in our job. Deep Agents' `LangSmithSandbox` wraps a `langsmith.sandbox` sandbox; a custom provider is a
`BaseSandbox` subclass with four methods (`execute`, `upload_files`, `download_files`, `id`). Pattern B (the
sandbox only behind a fixed-command `run_check`, the agent with no shell) stays an option for a tighter agent.

**The LangSmith sandbox, as tested.**
- **Access** is switched on per organization (`SandboxAuthenticationError: Sandbox feature is not enabled for this
  organization` until the account joined the Developer pay-as-you-go plan). Billing is per second in LSUs, 1 LSU =
  $1: 0.0576 per vCPU-hour, 0.01845 per GiB-hour, 0.000123 per GiB-hour of storage, with 8 LSU of free sandbox use
  a month and a limit of 10 sandboxes. The pages don't say which plan includes it.
- **The default runtime** is a full Linux machine (kernel 6.1, x86_64), running as root, with 1 vCPU, about 2 GB
  of RAM and 16 GB of disk; git 2.53, Node 24, npm 11, curl and wget; Python 3.14.4 (very new, so some ML libraries
  may lack wheels and Python tasks want a 3.12 snapshot); and open internet access (pypi.org and the npm registry
  answer). About $0.09 an hour at that size. A snapshot is a saved environment a sandbox starts from, built from a
  Docker image or Dockerfile (`create_snapshot`, `create_snapshot_from_dockerfile`), the answer to "all the right
  installs".
- **Getting the repo in.** A git mount (`mount_config`; the path must be under `/mnt/mounts`) works and is
  writable, but it is a FUSE filesystem on which git cannot track the files (everything shows as untracked, and one
  `git status` timed out), so it is not used. The sandbox shallow-clones instead (`git clone --depth 1 --branch
  dev`, the repo is public, no token): 6 seconds, onto the sandbox's own disk, where ordinary git works.
- **Getting the changes out.** After the agent finishes, our code inside the sandbox restores line endings, stages
  every change (`git add -A`; files ignored by `.gitignore`, like `node_modules`, stay out), lists the changes
  (`git diff --cached -M --name-status <base commit>`: A, M, D and R with old and new names) and writes one patch
  (`git diff --cached --no-renames <base commit>`; no `--binary`, since every binary file is rejected and the flag would write its whole content into the patch; no rename detection, so a moved file is a delete plus an add and the blocked-path check sees both paths), read out with `sandbox.read`. Every comparison is against the
  commit the sandbox started from, recorded right after the clone, never against `HEAD`, so the result is the same
  whether or not the agent commits (a diff against `HEAD` would come out empty after a commit; tested with an agent
  that commits). Other ways out exist (a writable GCS mount, which would put a bucket key with the vendor;
  `generate_download_url` for one file; `capture_snapshot` to save a disk) and were not needed.
- **Measured** (step 1f, the aisle pacing task, two runs): 80 and 135 seconds in all, 23 for the clone and the CLI
  install together, 52 and 47 for the agent, 155,000 and 144,000 input tokens, 6,500 and 5,700 output. The in-sandbox
  checks took 5 seconds in one run and about 65 in the other, probably the first compile fetching the Dataform core
  from npm (unverified); a snapshot with the core pre-installed would remove that variance.

**Production flow.**
1. The coding-agent service creates the sandbox, which clones `dev` (or, for a revision, the agent branch), installs or
   already has the Dataform CLI, and runs the agent; the job records the commit it started from.
2. Inside the sandbox our code restores line endings, stages every change and writes the patch and the change
   list, all against the recorded base commit. The agent's own `dataform compile` calls are its feedback loop, not the recorded outcome.
3. The job reads the patch, the change list and the base commit out of the sandbox.
4. The job validates the patch, then shallow-clones the repo itself at the recorded commit, with its branch-only
   token (never inside the sandbox), creates the task's branch, applies the patch, commits and pushes, and records
   the branch, the commit and the base. No local clone is involved anywhere.
5. The **independent compile check** runs the recorded compile (below), and the main agent opens the draft PR after the user
   confirms.

**Trust boundary.** With the sandbox as the agent's environment, its tools cannot reach the job container: the job
holds the keys, the token and the harness code, and none of it is reachable from the sandbox. What crosses back is
data the agent controls: tool results (text the model reads) and the patch, change list and check output.
- **The patch is untrusted input to the job.** Before applying it the job checks: no changed path (a moved file
  counts as a delete and an add, so both paths) matches the blocked list, nothing outside the clone, no symlinks or unusual file modes, deletions
  and renames reported (the change summary), and a size cap. It applies the patch in a clean clone of
  its own.
- **Checks run in the agent's sandbox are advisory.** An agent that wanted to could replace the `dataform` command
  there with a script that prints a pass. The recorded outcome therefore comes from the **independent compile check**: the
  validated patch applied to a fresh clone and compiled in a **second sandbox started from the same snapshot**
  (about 25 seconds, a fraction of a cent), never in the job container, because compiling executes the SQLX's
  JavaScript next to the job's secrets. One snapshot serves both sandboxes, so Node and the CLI are pinned in one
  place, not in two.

**Privacy.** The sandbox vendor becomes another subprocessor of repo code, which already reaches LLM providers
through code search. Claims found, from vendor and competitor pages, to verify before relying on them: Modal
publishes a SOC 2 Type II report (Trust Center), says it never accesses customer source code, encrypts stored
data, isolates with gVisor, and offers a HIPAA BAA on Enterprise; Daytona offers hosted, self-hosted and hybrid
(sandboxes on machines you manage), with conflicting reports of its SOC 2 level and its open-source status; E2B's
enterprise tier offers BYOC in your own AWS or GCP account, with some telemetry still going to E2B; LangSmith
Sandbox documentation on retention and residency was not found. For a portfolio project the hosted version is an
accepted tradeoff; keep credentials and data out of it.

**Enterprise path (documented, optional).** An isolated test runner in GCP: a second Cloud Run Job whose service
account has no roles, with egress blocked (a VPC with no NAT), no secrets, a stripped environment, a non-root
user and a short timeout; the code goes in through a bucket path and the results come back the same way. It
needs no vendor review and keeps everything in the tenant. If the agent's shell ran in the job container itself
(no sandbox), the risks are the service account token on the metadata server (a service account with no roles
makes it nearly useless; egress rules don't stop it, as far as is known), the secrets the job process holds
(readable through `/proc` by a process of the same user, so the shell would need a separate unprivileged user),
the harness code and checks living where the agent can write, and egress that is hard to restrict because the job
itself needs the model API, LangSmith, GitHub and Google's APIs. E2B BYOC on GCP is the middle path.

**Option: GKE Agent Sandbox, and what a switch would take.** Google's managed GKE add-on for agent code (gVisor
isolation, default-deny network, pre-warmed pools, a Python SDK); no ADK or Gemini model is required. Listed as an
option, not planned; kept so a switch can be done from this note.
- **The adapter exists, so nothing needs writing.** Checked 2026-11-04: `langchain-k8s` 0.7.1 on PyPI (a single
  third-party author, uploaded 2026-10-06, 10 releases) implements Deep Agents' `BaseSandbox` contract on top of the
  official client `k8s-agent-sandbox` (1.0.9, from `kubernetes-sigs`), and needs `deepagents>=0.7.21` (installed:
  0.7.32) and Python 3.11 or newer; it is built with `KubernetesSandbox(warmpool_name=...)`. The upstream repo also
  has an official Deep Agents integration under `clients/integrations/deepagents` (not examined). `langchain-kubernetes`
  0.4.0 (bitkaio, April 2026) is an older alternative. The ecosystem is moving: the adapter's notes describe a
  breaking change when the project's CRDs graduated to `v1beta1`. A new dependency needs approval first.
- **Code side, small.** The notebook calls LangSmith's own methods (`sandbox.run`, `write`, `read`); the adapters
  offer Deep Agents' `execute`, `upload_files` and `download_files`. Writing the harness against those interface
  methods makes a swap a one-line change where the backend is created (not done yet).
- **Infrastructure side, the real work** (an estimate of a day or two the first time, unverified): a GKE cluster
  (Autopilot) with the Agent Sandbox controller 1.0.0 or newer; a `SandboxTemplate` with our image (Node, git, the
  Dataform CLI), built in Artifact Registry, which replaces the LangSmith snapshot; a `SandboxWarmPool`, which is
  mandatory (`replicas: 0` means on demand, nothing running while idle); IAM so the job can reach the cluster; and
  network rules, since the default is deny and the sandbox needs GitHub and npm.
- **Price.** The add-on has no extra charge; GKE pricing applies: a $0.10 per hour cluster fee, covered by the
  $74.40 a month free tier for one Autopilot or zonal cluster if the billing account isn't using it elsewhere, plus
  Autopilot's per-pod CPU and memory (about $0.0445 to $0.0585 per vCPU-hour and $0.0081 per GiB-hour, sources
  differ). A run costs about a cent, similar to LangSmith (memory is cheaper). The trap is a warm pool, which bills
  while idle (about $55 a month per always-on 1 vCPU, 2 GiB pod); a cold pool of suspended VMs is cheaper but
  unpriced in what was found.
- **Speed.** Not the same: LangSmith started in about a second. Google's quoted fast starts come from warm pools;
  with `replicas: 0` a sandbox waits for pod scheduling, which on Autopilot may take tens of seconds if a node must
  be provisioned (unverified).
- **Why switch:** the in-tenant story, not cost or code. A Cloud Run Job can't host it (a sandbox needs a live
  session that takes many commands).

**Considered and rejected: Vertex AI Agent Engine Code Execution.** A managed sandbox that works with any
framework and model, with no deployment needed (Vertex AI SDK, `google-cloud-aiplatform` 1.112 or newer). It runs
Python or JavaScript code only, you can't install your own libraries, the default is 2 vCPU and 1.5 GB, and it is
not a shell, so `git clone`, `npm install` and `dataform compile` are not what it is for.

**ML test image.** The ML repo's `requirements.txt` has 17 entries, mostly heavy (`xgboost`, `lightgbm`,
`scikit-learn`, `shap`, `mlflow`, `optuna`, `statsmodels`, `bigframes`, `pandas-gbq`, `pyarrow`) and mostly
unpinned, so a fresh install may differ from the versions the project was built with; `pydantic~=1.10.0`
conflicts with the agent project's Pydantic 2, so it is its own environment: a sandbox snapshot with Python 3.12,
the pinned libraries, `pytest`, Node and the Dataform CLI. The default sandbox has internet access, so installing at
run time is possible, but a built snapshot is faster and pinned; with the isolated Cloud Run runner there is no
network, so everything must be in the image. Dependencies (decided 2026-11-04): the agent is steered away from adding one unless the change truly needs it and the value
justifies it, never for something it can do without. If it must, it declares the package in `requirements.txt` in the
patch, and says why in its report. The second sandbox installs from the patched `requirements.txt` (versions held to
the repo's lock, plus `gcsfs`) before the tests, so a package installed only in the agent's sandbox fails the check,
and a declared one is verified and is visible in the PR for review. After the merge the snapshot cell sees the
changed hash and rebuilds. A new package has no pin in the lock until it is regenerated on the VM. Tests
run offline on small synthetic DataFrames.

## Notebooks (decided 2026-11-04)

**Scope.** In the ML repo the scripts in `src/` are authoritative and the notebooks under `Notebooks/` are for
testing on samples, then porting to the scripts (the same way this project uses `phase3_graph.ipynb`). A feature
change goes in the scripts, which are plain Python the agent already edits well. The agent reads notebooks, creates
new ones (for example a with and without the new feature comparison on a sample of the data, or a fuller analysis)
and, when a task asks, edits existing ones by cell id. It never runs a notebook: it tests and compiles the percent-format
`.py` it wrote instead.

**What the notebooks are** (checked 2026-11-04, the nine in the clone): every one is stored with CRLF line endings,
is nbformat 4.5 with a cell id on every cell, and round-trips byte for byte through `nbformat` once CRLF is
accounted for, so a structured write followed by the per-file line-ending restore changes only what was edited.
Two are 2 to 3.5 MB, almost all saved outputs.

**The agent's `read_file` works on a notebook but returns raw JSON**, in pages of 100 lines. Reading a whole
notebook that way costs about 18,000 to 25,000 tokens for each training notebook (their cell sources are 2,100 to
2,900), 78,000 for `preliminary_eda`, 41,000 for `financial_impact`, 540,000 for `feature_eda_screening` and
900,000 for `model_evaluation` (sources 10,100), and a saved image is one line of up to 589 KB. Editing raw JSON
with `edit_file` is error-prone (every code line is an escaped string).

**Approach: scripts the agent runs itself, packaged as a skill.** The agent has a shell in the sandbox, so no typed
notebook tools or middleware are needed.
- **`read_notebook.py <path>`** prints each cell's id, type and source with the outputs stripped: the read tool.
- **`write_notebook.py cells.py new.ipynb`** creates a notebook (built and tested 2026-11-05). The agent writes a plain
  Python file in percent format (cells separated by `# %%` lines, markdown cells by `# %% [markdown]` with the text in
  comments; VS Code runs such a file cell by cell as it is). The script parses the markers into cells, builds valid
  nbformat 4.5 with generated cell ids and the kernel metadata of a sibling notebook in the same folder, empty outputs,
  writes it in the repo's format (one-space indent, sorted keys, CRLF including the last line), and prints every
  problem it finds so the agent can fix them at once. It never overwrites an existing notebook (which protects the
  existing ones and their saved outputs), compiles every code cell as Python without running it (line magics blanked,
  top-level `await` allowed, a `%%` cell magic skipped), and validates what it wrote with `nbformat`, deleting the
  file if invalid. Checks that construction already guarantees (unique ids, empty outputs, canonical formatting) are
  tests, not runtime asserts. It does not delete the input `.py`: `SKILL.md` tells the agent to write that file in
  `/tmp`, outside the repo, so a mistaken path cannot cost a real file.
- **`edit_notebook.py`** edits an existing notebook by cell id (built and tested 2026-11-05): `replace-text` (the old
  text must occur exactly once in the cell), `replace-cell`, `insert-after`, `insert-before` and `delete-cell`. Text arguments are
  files under `/tmp` (multi-line code does not survive shell quoting). It refuses a notebook that does not round-trip
  byte for byte through the repo's format (so saving can never change lines the agent did not edit; all nine real
  notebooks pass), requires new code to compile, clears the outputs and execution count of an edited code cell (they
  would be stale), validates with `nbformat` before saving, and leaves every other cell and its outputs
  byte-identical (checked on a real notebook: 4 of 1,646 lines changed). It reuses `check_python` from
  `write_notebook.py`.
- **A `read_file` description nudge** pointing to `read_notebook.py` for notebooks, appended the same way as the
  parallel-edit rule. It is soft: a wasteful read does no harm, so there is no guard on reads.
- **The job-side check is the safety net, because the agent has a shell and could skip the script.** Before
  applying a patch the job validates every notebook in it with
  `nbformat` (valid, a new notebook has empty outputs, outputs of untouched cells unchanged), so malformed JSON
  fails the task whatever tool produced it. The per-notebook change summary (cells added, edited, deleted by id)
  is a later addition for review.

**The skill.** A Deep Agents skill is a directory with a `SKILL.md` and optional supporting files:
```
/skills/notebooks/
    SKILL.md        # YAML frontmatter (name, description; optional license, compatibility, allowed_tools),
                    # then the instructions
    read_notebook.py
    write_notebook.py
```
`name` is at most 64 characters, lowercase letters, digits and hyphens; `description` at most 1024. The agent
always sees only the name and description (in the system prompt); it reads the full `SKILL.md` with `read_file`
(`limit=1000`, since the default 100 lines is too small) when the task matches, and runs the scripts by absolute
path. That keeps the instructions out of the context on every task that doesn't involve a notebook. Sources are
paths in the **backend** (in order, later ones overriding earlier ones), passed as `create_deep_agent(skills=[...])`;
with a sandbox backend those paths are inside the sandbox. `SKILL.md` for notebooks says: use `read_notebook.py` to read
a notebook, never `edit_file` or `write_file` on a `.ipynb`; write new notebooks as a percent-format file and run
`write_notebook.py`; keep outputs empty; the repo's notebooks are testing tools and the scripts are authoritative.
- **Where it lives and how it gets there.** In `coding_agent_app/skills/notebooks/` (a new top-level directory, decided
  2026-11-05, for the job's code, Dockerfile and skills; outside `app/` so the gateway image stays unchanged, and the
  job's Dockerfile copies what it needs from `app/`), version-controlled with the job code and reviewed with it,
  and uploaded into the sandbox at start with `upload_files`, to `/skills/notebooks/` (the folder name matches the
  skill's `name`): outside the cloned repo, so
  it never appears in the patch and the agent cannot leave a modified copy behind. The alternative, baking it into
  the snapshot, needs a rebuild per change. `nbformat` goes in the snapshot (pure Python, a quick install).
- **What belongs in the ML repo instead:** how its training notebooks are structured (the conventions), as a second
  skill or an `AGENTS.md`, reviewed with that repo. A decision for the repo owner, later.

**Testing the code, not the notebook.** The percent-format `.py` is a plain script, so the agent checks it with
`python /tmp/cells.py` before converting, when it needs only local files (CSVs in the repo). `SKILL.md` tells it not
to run the notebook itself (saved outputs would make large, noisy diffs). The sandbox has no BigQuery access by
design, so code that needs it cannot run; the report says what was not run.

**Notebook-writing conventions live in two places, so neither depends on the other.** `SKILL.md` carries them for the
coding agent, because a plan written in a hurry may omit them: follow the neighbouring notebooks, sample by user and not
by row (these features depend on a user's history, so a row-level sample leaks across the train and validation split),
import the training functions from `src/` so the test reflects production code, assert the assumptions at the top so
a mistake fails loudly, keep it concise, and say where it lives. The main agent's prompt should carry the
same list (when that is built) so the plans it writes start out good, plus where new notebooks live (for example a
`Notebooks/experiments/` folder), which only the plan can say.

**Considered, not chosen.** Typed notebook tools plus a middleware guard on `.ipynb` writes (about twice the work,
and only worth it if the agent must edit existing notebooks); converting notebooks to text with `jupytext` and back
(simpler for the model, but not confirmed to preserve cell ids and formatting, where `nbformat` is verified
byte-identical on all nine). Effort for the chosen route: about two to four hours by estimate (the two scripts and
their tests against the nine real notebooks, the skill file, the job-side check).

## Output filter and line endings

Plain code that runs after the agent and before anything is committed. The in-sandbox parts (staging, line
endings, the change list) are advisory because the agent shares that machine; the job re-checks the patch it
reads out (Running tests and code, Trust boundary):

- **Export every change, check the policy in the job.** The sandbox stages everything not ignored by git and
  exports one patch against the base commit; the job then checks every changed path against the blocked list and
  fails the task on a match (tested: a committed `.github/workflows/ci.yml` is caught, a rename is reported as a
  delete plus an add, a new untracked file in the repo root is exported and shows in the change list for review).
  A silently dropped file would be worse than a failed task.
- **Validate the patch in the job** before applying it: no blocked path, nothing outside the clone, no symlinks or
  unusual modes, a size cap, every `.ipynb` valid (Notebooks), then `git apply --check` and apply in a clean
  clone.
- **Restore each changed file's original line endings.** The edit tool reads files in text mode (CRLF becomes
  LF) and writes LF, so a CRLF file shows every line as changed. In the ML repo the 19 SQLX files are committed
  with CRLF and the Python and the rest are LF, and the same happens on Linux (git there leaves the files as
  committed). The step checks the last committed version of each changed file for `\r\n`, normalizes the working
  file to LF, and converts back to CRLF if the original used it. Per file, never a global rule, because the repo
  mixes both. Git is called with `-z` (null-separated paths, so no file name can break the split), output decoded
  as UTF-8 (not the Windows locale encoding) and `check=True` (a failed command raises). Prompt guidance can't fix
  this: the tool normalizes whatever the model writes. Open: a file with mixed endings (converted wholly to CRLF
  if any CRLF is present) and a new file (no original; follow its neighbours). A `.gitattributes` for `*.sqlx` in
  the ML repo is a cleaner long-term fix and is a human decision.

## Job image

The container of the coding-agent service: `Dockerfile.coding-agent` at the repo root, built by `cloudbuild.coding-agent.yaml` (the gateway's `gcloud run deploy --source .` only finds a file named `Dockerfile`, and that one is the gateway's). With the sandbox as the environment it needs less than first thought:

- **Python 3.12 and `uv`**, as the gateway image, for the agent loop and the job code. It installs only the `coding-agent` dependency
  group (`uv sync --only-group`), not the gateway's heavier packages.
- **Git**, which `python:3.12-slim` doesn't include, for the fresh clone, `git apply`, commit and push.
- **No Node, no Dataform CLI, no ML libraries.** Those live in the sandbox snapshot, which the agent's sandbox and
  the independent compile check both start from, pinned in one place (Dataform CLI 3.1.3 today; the core is pinned in
  `workflow_settings.yaml`, 3.0.52, and is fetched at the first compile). The job never compiles or runs
  agent-authored code.
- The service account is new and minimal (setup guide, Step 19). The git subprocesses inherit the process environment, which holds the API
  keys and the push token's `GIT_CONFIG_*` header (the push needs the header). Git does not run agent code, but the environment is not stripped.

If the gate ran in the job container instead, the image would need Node, the CLI and a pre-installed core, plus a
strict allowlist of the `${}` forms in the SQLX and a separate unprivileged user for the compile; that was
rejected in favour of the second sandbox.

## Findings from the local runs (step 1, 2026-11-03/04)

- **Paths.** With `virtual_mode`, the agent addresses files as `/definitions/...`, so the permission rules are
  rooted at the clone.
- **Parallel edits.** `edit_file` rejects several concurrent edits to one path ("parallel file mutations to the
  same path are not allowed"), which cost a handful of retried calls per run. A line in the system prompt did not
  stop it (see the sandbox findings for the cause and the current fix).
- **The report varies, the diff doesn't.** Two runs produced the same code; one report called the draft's scope
  error a bug and the other described it only as a style match, and one misdescribed the draft (a `SUM` window it
  did not have). The result's validation and conflict fields therefore come from the checks and the diff, never
  from the agent's narrative.
- **Cost baseline.** A read-only run used about 34,000 input and 2,000 output tokens; the editing task about
  108,000 and 5,800 (input is cumulative across rounds).
- **Assertions.** The agent added a null check for the new ratio but not for the two helper columns, although the
  product helpers have them. The task text asked only for the feature, so the plan should say to match the
  product's assertions for every new column (decision pending).
- **Context compaction.** Built in and on by default; it writes evicted history through the backend. Whether the
  path rules apply to that write is unverified; prefer routing it to an in-memory backend (`CompositeBackend`)
  over allowing writes on the clone's disk.

## Findings from the sandbox runs (steps 1b to 1f, 2026-11-04)

- **Pattern A works end to end** on the aisle pacing task: 80 seconds, the same code quality as the local runs, a
  final compile of 13 tables with 0 errors, a change list of three modified files, nothing left out of the patch (no
  stray `package.json`), and a patch that applied cleanly to a fresh clone at the same base commit.
- **The planner is used some of the time.** In the first sandbox run it was not (the model judged a three-file
  change simple); in the second it made a `write_todos` call with a list per layer before editing. It is soft
  guidance; no fix needed.
- **The parallel-edit guard is positional.** Deep Agents rejects any later `edit_file`, `write_file` or `delete` on
  a path already mutated earlier in the same model response, whatever the timing, so a lock or queue would not
  help. The system prompt line failed (five of 16 `edit_file` calls were repeats in step 1f). The current fix
  appends one rule to the `edit_file` and `write_file` descriptions through a `HarnessProfile(tool_description_overrides=...)`
  registered under `anthropic:<model>`: `create_deep_agent` looks up the registry by the model's provider and name
  and hands the overrides to the file tools. A tool's own description is where the model weighs a rule when it
  picks the call, as with `submit_answer`'s contract. Result, one run: failed file-mutation calls fell from 5 to 3,
  each a harmless rejection the agent redid one call later; accepted as good enough. Fallbacks, not pursued: a model
  subclass with `parallel_tool_calls=False` (it also stops useful batched reads) or custom middleware (fiddly). The
  cell prints the count of failed file-mutation calls.
- **The independent compile check works** (step 1f, phase 5): a second sandbox clones `dev` (at the recorded start
  commit if `dev` moved), installs the pinned CLI, applies the patch from bytes (`Sandbox.write` takes bytes, so no
  encoding step) and compiles; the compile itself took 4 seconds. When Python files changed it also runs `ruff`
  (syntax errors and undefined names only, so older style problems do not fail the task) on the changed files and
  `pytest` (no tests collected is not a failure while the repo has no suite), because pytest imports the agent's code. Its result, not the agent's own compile, feeds the
  checks. Decided 2026-11-04: the compile runs once, here, and not again in the loop-back middleware or after the
  agent finishes (the agent still compiles during its work, as its prompt says). The clean sandbox is what shows the
  patch works on its own: it catches a package the agent installed to make its test pass, a file git ignores that
  the compile quietly needed, and tampered tools, none of which a compile in the agent's sandbox could reveal. `task_passed` is false if any check fails, and that is what the task status records.
- **The snapshot** (`scripts/coding_agent_snapshot/Dockerfile`, built by the notebook's snapshot section):
  `node:24-slim` with git, `uv`, the Dataform CLI 3.1.3 with its core 3.0.52 warmed in npm's cache, and Python 3.12.13
  in `/opt/ml-venv` holding the ML repo's `requirements.txt` plus `gcsfs` (the code imports it, the file omits it),
  every version held to `requirements.lock` by constraints (139 packages), plus `pytest` and `ruff`. The lock is a
  full freeze of the Vertex Workbench `jupyterlab` environment (253 packages, with JupyterLab and Workbench plugins,
  six installable only on that VM), so it is used as constraints and not installed whole; freeze with the
  interpreter `PY_ENV` names (`/opt/micromamba/envs/jupyterlab/bin/python3`), not the terminal's default `python`.
  The cell rebuilds only when the hash of the two requirements files differs from `/opt/inputs-sha256` in the
  image; a change to the Dockerfile or a pin needs the old snapshot deleted by hand. The build took 261 seconds once;
  the PyPI step alone varied from 42 to 662 seconds between runs (the sandbox's network), so the build's `timeout`
  is 3600: the SDK uses it as the time limit for the whole build command, and 900 was exceeded once. The save of the
  built disk uses the client's own HTTP timeout (10 seconds by default), so the snapshot client is created with
  `timeout=900`. A Dockerfile-built snapshot has no tag, so sandboxes start from its id, looked up by name. With
  it: sandbox ready in 2 seconds, the repo cloned at 9, the agent's checks 4, the second sandbox's apply and compile
  about 9, 124 seconds end to end, `task_passed`. The training scripts set `device='cuda'` (XGBoost) and
  `device='gpu'` (LightGBM ranker) and the sandbox has no GPU, so the coding agent's system prompt says so: a model fit
  run as a test uses `device='cpu'` in the test only, on small synthetic data, and the repo's device settings are
  never changed. The full `xgboost` wheel (with `nvidia-nccl-cu12`) is kept to match the repo.
- **If `dev` moved** (built in the notebook, not yet tested): the job tries `git apply --3way` onto the new tip. Success means the
  branch is built on the new tip; a conflict ends the task as `needs_rebase` with git's error. No automatic retry
  (confirmed 2026-11-04: a conflict needs `dev` to change the same lines within one run, so it is rare; a failure and
  a retry the user asks for through the main agent is enough, and a loop-back message cannot fix it because the
  sandbox holds the old base).
- **Assertions.** The agent again added a null check for the ratio but not the helper columns. Putting that in the
  plan is the main agent's job, not the coding agent's.
- **The report varies, the diff doesn't** (again): this run described the draft's scope error only as not fitting
  the repo's structure. It also noted a real quirk of the 999 fallback: for an aisle never bought, both inputs fall
  to 999 and the ratio is exactly 1.

## Unverified, confirm before building

- **Found and fixed 2026-11-08:** `git apply --numstat -z` lists a renamed file by its new name only, so `validate_patch` never
  saw the old name, and a rename out of a blocked path (`.github/ci.yml` to `docs/ci.yml`) passed. Fixed by building the patch with
  `--no-renames`: a move is then a delete plus an add, and both paths are checked. Still to add: a Layer 1 case for a move out of a
  blocked path, with the patch code in the port.
- The exact IAM role for running a job with overrides (believed to be `run.jobs.runWithOverrides`).
- The Python `deepagents` package's sandbox and permission behaviour (the docs read were the JavaScript package).
- Whether `get_conversation_findings` results include the decoded tool args (the SQL); see
  `docs/conversation-memory.md`.
- Whether `dataform compile` needs credentials or network (it fetches the core version pinned in
  `workflow_settings.yaml` at the first compile; it worked in the sandbox, which has internet), and which linter
  the job image uses for Python.
- LangSmith Sandbox: which plan includes it, its retention and residency, and whether the default runtime or its
  Python version will change; whether the proxy configuration can restrict egress.
- Skills with a sandbox backend: that `skills=[...]` finds skill files uploaded into the sandbox, that the agent
  reads `SKILL.md` and runs the scripts by absolute path, and that `nbformat` installs and runs under the
  sandbox's Python 3.14.
- The GKE Agent Sandbox figures above (rates, scheduling delay on Autopilot, warm-pool and snapshot billing).

## The port (decided 2026-11-07)

How the notebook becomes the job. **Exists** means in the notebook or `coding_agent_app/` today; **new** means not written.

| Module in `coding_agent_app/` | Holds |
|---|---|
| `coding_agent.py` | exists in the notebook: `build_coding_agent`, `SANDBOX_SYSTEM_PROMPT`, the grader prompt (`CODING_RUBRIC` stays in `app/coding_task_models.py`, where the plan model embeds it), `compact_transcript`, `PatchCheckMiddleware`. The middleware lives here because it is agent-loop glue, which keeps `patch.py` free of langchain imports |
| `patch.py` | exists: `read_patch`, `validate_patch`, `validate_notebooks`, `build_patch`, the blocked-path and size constants, `RESTORE_ENDINGS` |
| `apply_and_verify.py` | exists: `apply_patch_and_push`, `verify_patch_in_clean_sandbox`, `build_final_checks` and their dataclasses. The push is part of `apply_patch_and_push` (built 2026-11-08): given a `new_branch` (a first task) or `is_revision` (the base branch is the agent's own), a patch that applies is committed (author `coding-agent`, message = `agent: <plan name> (task <task id>)` plus the plan's goal) and pushed; a new task creates `agent/<plan name>-<short task id>`, a revision adds a commit to the branch it continues. Tested against a local bare repo; the real push with the branch-only token (from the job's environment) is Layer 2 |
| `orchestration.py` | `execute_coding_run` (built: the sandbox, the agent, apply and verify, the result) and `run_coding_task(task_id)` (built: claim the task, read the parent for a revision, send the preamble and the plan as JSON, call `execute_coding_run`, write the result in a `finally`; an error becomes a stopped result with `model_unavailable`, `sandbox_error` or `internal_error`; a revision's parent is trusted to have pushed a branch, which `start_coding_task` checks) |
| `main.py` | built: the FastAPI app and `POST /run/{task_id}` (not found 404, not queued 409, otherwise 200 after the run); a lazily cached `load_environment` puts the Anthropic and LangSmith keys in the environment and hands the push token to git as a `GIT_CONFIG_*` authorization header; no logic |
| `tools.py`, `skills/` | exist |

The plan and result models are used by the job and the gateway, so they go in a top-level `app/` module (`app/coding_task_models.py`), and so does the task store, `app/coding_task_store.py` (decided 2026-11-08): `write_coding_agent_task`, `get_coding_agent_task`, `mark_coding_task_running` (queued to running in a transaction, `TaskNotFound` and `TaskNotQueued` otherwise), `update_progress`, `complete_coding_task` (the result and `completed` in one update) and `mark_coding_task_notified`. Its documents carry `expires_at`, set at creation and again at completion (30 days), for a Firestore TTL policy: `gcloud firestore fields ttls update expires_at --collection-group=background_tasks --enable-ttl`.
`TASKS`, `section`, `progress`, `describe_tokens` and the stats and report cells stay in the notebook. The snapshot-building cell becomes
`scripts/build_snapshot.py`, last, with the GitHub Action in the CI/CD step.

**What changes from the notebook.** Impersonation goes (the tools default to the job's own credentials). The task text is built from
the stored plan, and the reviewer runs only when the plan's `review` is true. The job pushes the branch. Both sandboxes are always
deleted in a `finally`. Logging is light, configured at the entrypoint: task start, end, and errors with tracebacks. Skill paths are
package-relative.

**The task document** (`CodingAgentTask`; the result is nested in its `coding_agent_result` field, `None` until the end, so it is clear which fields are the result: decided 2026-11-07). `status` is a lifecycle only: `queued`, `running`, `completed` (all written by the service), then `notified` (written by
`agent_node` when it first shows the task to the main agent). `progress` is a separate field the job
overwrites with a stage line (richer later, from the streamed state), so adding stages never changes the values the main agent
switches on. The outcome lives in the result. The result is written once, atomically with `status="completed"`, in a `finally`, so a
timeout, a step limit or a sandbox error still produces one.

**Asking the user.** The prompt's `BEFORE YOU BUILD` section tells the agent to check up front, and again as soon as it notices a gap
mid-work, that the plan and the repo settle everything essential, and to finish with `blocking_questions` rather than guess; smaller
choices go under `assumptions`. It signals this in its structured final answer, `StructuredCodingAgentResponse`, set as
`response_format=ProviderStrategy(...)` (native structured output, not a tool or a forced `tool_choice`, which Anthropic does not
support with thinking on; a probe confirmed adaptive thinking works with it). Any `blocking_questions` make the task `needs_input`; the report is kept in the result as the agent's explanation but does not decide
the outcome, with no validator on the model (a failed validation would crash the run, and the questions are the safer
reading). `PatchCheckMiddleware` stops before building a patch and clears the rubric (the reviewer skips itself when it is empty; a
`jump_to: "end"` from an `after_agent` hook re-enters the chain and loops until the recursion limit): no patch checks, no review, no apply or clean-sandbox
verification, and empty `checks` and `patch_problems` in the result. The orchestration branches once on
`agent_run.answer.blocking_questions` and passes `final_checks=None` to `build_coding_result`. An empty report with no blocking questions is a fixable
violation. `follow_up_questions` ride along on a finished task and do not stop the build. Live-verified 2026-11-08: a task that
needs promotion data the pipeline lacks stopped after 7 tool calls with two blocking questions, and the clear `rapidfuzz_dependency`
task still ran the normal path.

**Push rule.** The branch is pushed whenever the patch is valid and applies, even if the clean-sandbox checks fail; the failed checks
and their output are in the result so a revision can fix them. A rejected or empty patch has no branch. Proposed, not confirmed:
`open_pull_request` refuses a `failed` outcome unless the user explicitly says to open it anyway.

**Why a service, and how it is started (decided 2026-11-07).** A Cloud Run Job's start latency is not documented; one forum report
says 90 to 120 seconds in us-central1 even with a minimal image, against 2 to 3 seconds for a service, and tasks run 3 to 5 minutes.
The task has to be an in-flight request so CPU stays on and the instance is not scaled in, so Cloud Tasks makes the request and holds it
open until the run returns. The gateway's `start_coding_task` creates the task; nothing else runs in the gateway. Adding
`google-cloud-tasks` is approved (2026-11-07); add it in the step that uses it.

**Securing the service.** It is not public (unlike the gateway, which Power Platform must reach): deployed with
`--no-allow-unauthenticated`, so Cloud Run rejects any call without a valid identity token before it reaches the container. Only a
dedicated invoker service account holds `roles/run.invoker` on it; Cloud Tasks signs its request as that account (OIDC token, audience
the service URL), and the gateway's account may create tasks in the queue and act as the invoker account. IAM is the only access control (decided
2026-11-07): no API key and no in-code token check, because Cloud Run's check runs before the container and our own would add a
maintenance cost and a way to get it wrong. Ingress is left open; `--ingress internal` would hide the public URL as a second layer,
but it blocks calls from a laptop, so it can be turned on later, once the deployed path has been tested end to end. The handler takes only the task id; everything else comes from
the Firestore document the gateway wrote, and it refuses to start unless the task is `queued`, so a replayed or forged call does nothing.
`coding-agent-sa` is the service's own identity, with the secrets and permissions listed under its entry.

**Limits.** Service request timeout and Cloud Tasks dispatch deadline both 30 minutes (Cloud Tasks accepts 15 seconds to 30 minutes for
an HTTP target); an internal timeout of 25 minutes so the run can still write a result before the request is cut; one attempt (a retry
would push the branch twice); the task name is the task id, which makes a duplicate create fail; concurrency 1, max instances 1 and
the queue's max concurrent dispatches 1, which is the one-running-task cap; min instances 0. Every shell command in the agent's sandbox is stopped after `EXECUTE_TIMEOUT_SECONDS` (120, `coding_agent.py`): `TimeLimitedSandbox` cuts any timeout the agent sets to that limit and returns a timed-out command as its output, with guidance, instead of raising (the library's default is 30 minutes, which let a whole-suite `pytest` run for four minutes in the 2026-11-09 revision run); the `execute` tool's description says so. To confirm: the service's maximum
request timeout (believed 60 minutes), CPU being allocated for the whole request, and a cold start of the real image.

**Telemetry and long-term storage.** One row per task in a new BigQuery table in the `telemetry` dataset (name not chosen): the flattened
metrics from `CodingAgentResult`, plus the plan, the result and the task document as JSON columns, so BigQuery is the long-term record of
everything (decided 2026-11-07) and Firestore holds only the live state under its TTL. `get_coding_task` does not read BigQuery for now.
written by impersonating `turn-writer-sa` as the gateway does (`coding-agent-sa` needs Token Creator on it, `turn-writer-sa` write access
to the table). Our job code holds the identity, never the sandbox or the agent. Read `.claude/rules/telemetry.md` first.

**Seeing the code.** `get_coding_task` builds the compare link from the repo constants and the branch
(`https://github.com/{owner}/{repo}/compare/dev...<the result's branch>`), not stored in the result, since nothing returns it and it is derived,
and the main agent relays it. `get_coding_task(diff=True)` fetches the diff in one request from the compare endpoint with
`Accept: application/vnd.github.diff`, using the read token, capped at `MAX_REPO_FILE_CONTENT_CHARS`; over the cap an error tells the main
agent to read the files instead. Checked on the public repo, 2026-11-07: HTTP 200, a plain unified diff (`main...dev`, 42 KB, 21 files);
the three-dot form diffs against the merge base, so only the branch's changes show. Notebook diffs are JSON hunks, noisy but complete.
`get_repo_contents` gets an optional `ref` (branch), defaulting to the configured branch, to read a whole file on the agent branch; its
description filter applies only to the no-path listing, so new files are readable by path. Unverified: whether a just-pushed branch is
visible to the API at once. The branch is public once pushed, since the repo is public. No patch is stored in the result.

**Numbers.** A coding task's numbers (counts, tokens) are context, not data, like notes and the task plan (`.claude/rules/tools.md`):
stated in prose, never in a markdown table (table cells are extracted and checked), never in `all_prose_numeric_claims`, and a number
about the data inside a coding report is never repeated as data. Goes into the `get_coding_task` description and that bullet when the
tool is built.

**Tests, kept lean.** Two new files: the patch trust boundary (a handful of crafted patches: blocked path, symlink, rename, binary,
oversized) and `apply_patch_and_push` against a local git repo (applied, conflict). Not tested: the middleware wiring, Firestore writes (the
mock would be the whole tool) and result assembly; a live run covers them. The existing tools, notebook-skill and sqlx tests stay. The 21
scratch harnesses used while building are not ported.

**Order.**
1. The plan and result models.
2. Notebook behaviours: the `needs_input` path (the agent finishes with a structured output carrying `report`, `assumptions`,
   `blocking_questions` and `follow_up_questions`; wired in the notebook and live-verified 2026-11-08), stop reasons. The agent-phase timeout (`asyncio.timeout`, about 20 minutes, with `stop_reason` becoming a code: `recursion_limit` or
   `timeout`) is built in the port, in `run_coding_task`.
   Done in the notebook 2026-11-08, checked offline, live run pending: check output on failures (`FinalCheck.detail`), and assembling a
   real `CodingAgentResult` (`build_coding_result`, printed as section 7 of the report). Decided 2026-11-08: no outer
   LangGraph graph. The flow is a straight line with one skip, so it needs none of what a graph is for (loops, many branches,
   checkpoints), and an exception skips a graph's `finalize` node anyway. `run_coding_task` is a plain async function:
   try/except for the stopped and failed cases, a `finally` that deletes the sandboxes and builds and writes the result, and a
   `progress(...)` call between steps. `build_coding_result` takes an `AgentRun` (what the agent run produced) instead of ten
   arguments.
3. Split into the modules with the two test files. Decided 2026-11-08: the package is `coding_agent_app/` (`main.py` holds the
   HTTP layer and startup, `orchestration.py` the run), and the notebook is not updated to match; after the port, work is in the `.py` files
   with a new notebook that imports them. Done: `patch.py` with `tests/test_patch.py`, and `apply_and_verify.py` with `tests/test_apply_and_verify.py` (no progress calls; `build_final_checks`
   takes the validation problems as a list, so the module does not import the agent; `REPO_URL` is in `app/config.py`), and `coding_agent.py` (no test file: the middleware wiring is covered by live runs; the plan reaches the agent as `TASK_PREAMBLE` plus the plan as JSON, built in `run_coding_task`), and `orchestration.py` (`execute_coding_run` with the agent timeout, `TASK_PREAMBLE`, `build_coding_result`; each step writes its stage with `update_progress`; `AgentRun` carries a `stop_reason` code). `run_coding_task`, the store and the route are steps 6 and 7. The `coding-agent` dependency group is in `pyproject.toml` (approved and added 2026-11-08; `dev` includes it, the service image installs only it with `uv sync --only-group`, and the gateway image's `--no-dev` leaves it out, checked with `uv export`). The snapshot Dockerfile moved to `scripts/coding_agent_snapshot/`. The test tasks live in `scripts/coding_agent_tasks.py`, each a `CodingAgentPlan` named after its task, built from module-level variables for its pieces (the free-text originals stay in the parked notebook; their report lists are left out, since the agent's report field says what a report covers). The e2e notebook, `notebooks/coding_agent_e2e.ipynb`, imports them by name, writes a task with `write_coding_agent_task`, posts to `/run/{task_id}` in-process and polls `get_coding_agent_task`; it runs with the developer's own credentials, so the service account's IAM boundary is not exercised there. `revision_for_aisle_pacing` is a revision of an `aisle_pacing` run (set `PARENT_TASK_ID` to that task's id): the rest of `aisle_pacing_all_models_edit_notebook`, after the SQL.
4. The task store and the push.
5. The coding-agent service, the Cloud Tasks queue and the IAM (`google-cloud-tasks` approved), after a cold-start check of the real image.
6. The telemetry table.
7. The main-agent tools, `GET /tasks/{id}`, the `ref` change, the prompt attachment and the numbers rule; then the cost-gate extraction
   (`docs/build-order.md`).
8. Later: the snapshot GitHub Action, live progress, cancellation, the one-task cap.

**Revisit later: run memory for very large tasks (noted 2026-11-09, after the coding agent works end to end).** Deep Agents'
`MemoryMiddleware` loads files into the system prompt on every model call, so their content survives the library's summarization (it
compacts at 85% of the context window, 170,000 tokens when the window is unknown, and keeps only the last 10% verbatim). It takes its own
`system_prompt` (the default is written for chat assistants and tells the agent to save user feedback), and has to be added to our
middleware list because `create_deep_agent(memory=...)` does not expose that argument. The idea is a per-run notes file in the sandbox,
outside the clone (so it never reaches the patch) and gone with the sandbox. It is worth more than a to-do list (`write_todos` already is
one) only if it holds what is expensive to rediscover and that summarization drops: where things are in the repo, grep results, commands
that hung or timed out (the grep and glob timeouts seen live), and decisions made. Not for memory across runs or users: a store the agent
writes and later runs read silently bypasses the plan the user confirmed, lets injected text persist, and makes runs depend on hidden
state; cross-run knowledge goes through the plan, the parent task's result, or reviewed files in the repo. Evidence first: runs use about
27,000 tokens of context per call, so summarization needs roughly six times that, and a task that large would probably hit the 20-minute
cap or the 200-step limit first, which stops the run with nothing pushed. Experiment with truly large tasks (`column_descriptions`, 19
files, is the largest test task) and watch context per call, tool calls, wall-clock time, repeated greps, and whether summarization
fires. Related options for big work: split it into a first task plus revisions (the branch carries the work), and salvage a stopped run's
partial work to a WIP branch.

## Build steps

Notebook-first, one step at a time, reviewed after each.

1. **Local** (`notebooks/phase6_coding_agent.ipynb`; done). A deep agent against a throwaway local clone of the ML
   model repo: the read-only run, the first editing task (twice), the line-ending restore and a real
   `dataform compile`. `deepagents` is installed as a dev dependency.
1b. **Hosted sandbox** (same notebook; done). LangSmith sandbox access, the default runtime, the git mount (rejected),
   a shallow clone and patch export, and the agent working in the sandbox (pattern A), 80 seconds end to end. Still
   open: testing the `--3way` apply when `dev` moved. The custom snapshot (Node, the Dataform CLI, Python 3.12 and the
   ML libraries) and the independent compile check in a second sandbox are built.
1c. **Notebook skill** (in progress). The three scripts (`read_notebook.py`, `write_notebook.py`, `edit_notebook.py`)
   with tests against the nine real notebooks are built; `SKILL.md`, the upload into the sandbox and the `skills`
   setting, the `read_file` nudge and the job-side notebook validation remain, proven on a with and without
   comparison notebook for a feature change.
2. **Containerize.** The same agent as a private Cloud Run service (Cloud Tasks calls it), with its own service account. The modules
   are decided ("The port"); the shared plan and result models in `app/` need approval first.
3. **Tools and state.** The Firestore task document, the three task tools, `GET /tasks/{id}`, the prompt
   attachment, and the schema, `run_check` and `dry_run_sql` tools. Needs the Cloud Tasks client library (`google-cloud-tasks`, approved 2026-11-07).
4. **The PR step.** The service pushes the task's branch; the main agent's `open_pull_request` opens the draft PR
   after the user confirms.
5. **Later:** ML Python changes and the CI check on the PR.

**Remaining work, in order (agreed 2026-11-04, revised).** Notebook-first until the port; the numbered build steps above
are the long form of items 7 and 8.
1. **Patch validation and the loop-back middleware** (built; ran live 2026-11-05 and passed after one fix: `git apply`
   run inside a subfolder of a repo skips patch paths outside that subfolder, so the parse-only calls now run from the
   temp folder, and `read_patch` raises if a non-empty patch lists no files). Two parts.
   - Part 1 (built in the notebook and tested offline on real and crafted patches, 2026-11-04):
     `validate_patch(patch_bytes)`, a function that reads the patch with git itself (`git apply --numstat` and
     `--summary`, so no second parser can disagree with the one that applies it) and never uses the sandbox's change
     list. It returns two lists and runs every rule, so the loop-back message reports each fixable problem at once.
     **Fixable** (the agent can correct them, so the middleware loops back): a blocked path (a moved file counts as a delete plus an add, so both paths are checked),
     a symlink or submodule, a binary file (a reviewer cannot read it in a PR, and a pickle runs code when loaded), and a notebook that is
     not valid JSON or fails the `nbformat` schema (`validate_notebooks`: the middleware reads each `.ipynb` the patch
     adds or changes from the sandbox, since a patch holds only hunks, and names the cell, for example
     `$.cells[1]: 'source' is a required property`). Not checked: empty outputs on a new notebook and unchanged outputs
     on untouched cells, which the scripts already guarantee; add them only if a run shows the scripts bypassed.
     **Unfixable** (the task fails, no loop): a patch git cannot read, and a patch over the size limit (5 MB, a constant to
     adjust, set to catch a runaway agent; the file-count and per-file line limits were dropped 2026-11-08, since the byte limit
     bounds the job and a human reviews the PR). An empty patch is fixable: the agent is sent back to make the changes. Line endings are
     not a check: the restore step is a transformation, and if it ever failed the PR would show a large diff, which a
     reviewer can reject. No content scanning (secrets, `js {`, `require(`): the sandbox holds no secrets and a human
     reviews the PR. The job stage applies a patch that passed to a fresh clone of the current `dev` with `--3way`
     (`needs_rebase` on a conflict, no automatic retry), and Phase 5 runs only for such a patch.
   - Part 2: a small middleware that, when the agent says it is done, restores line endings, runs `git add -A`, builds
     the patch, reads out the bytes and validates them, sending the agent back with the violations (capped at two
     rounds). It keeps the last bytes and violations; those exact bytes are the ones shipped, so nothing is validated
     or rebuilt a second time. The agent's post-run advisory compile and its report line are removed, along with the
     sandbox's own `changed_files`. The report prints the change list derived from the patch. Built in the notebook
     as `PatchCheckMiddleware` (2026-11-04) and tested offline with a fake sandbox over a local git repo: a good change,
     a fixable problem fixed on the retry, the retry cap (`max_retries`, two), and an unfixable problem ending the run.
     `after_agent` hooks run in reverse list order, so it is listed last and runs before the reviewer; an unfixable
     problem clears the rubric and skips the reviewer's cost. Not yet run end to end. Phases 4 to 6 are now functions
     (`apply_patch_and_push`, `verify_patch_in_clean_sandbox`, `build_final_checks`) whose results are dataclasses, not Pydantic models:
     nothing crosses a boundary, so there is nothing to validate, and the stored task result is built from them at the
     edge. `build_final_checks` holds only checks every task gets; checks for one test task live in a notebook-only cell.
     `apply_patch_and_push` takes the repo URL as a parameter so a test can use a local repo, which also gives the
     `--3way` and dev-moved cases a simple test.
2. **Test the dependency path** (done). The `rapidfuzz_dependency` task, which needs a package the snapshot lacks, passed:
   the agent declared it in `requirements.txt` and the clean sandbox installed it from there.
3. **Judge the reviewer on every future test.** Four graded turns all passed with good reviews (2026-11-04). No
   separate trial: keep watching section 2b of the report in each test for anything it misses or flags wrongly; it
   stays off by default in the port.
4. **Notebook skill** (step 1c), including notebook validity (JSON parses and passes `nbformat`) added to
   `validate_patch`, so a broken notebook loops back with the real error. Pieces, in order: A `read_notebook.py` (done
   2026-11-05, tests in `tests/test_notebook_skill.py`; run on all nine real notebooks, output about 8% larger than the
   cell sources alone, and it must set stdout to UTF-8 because the notebooks contain emoji), B `write_notebook.py` and `edit_notebook.py` (done 2026-11-05), C `SKILL.md` + upload + `skills=` + a sentence on `read_file`, `edit_file` and `write_file` + `nbformat` in the
   snapshot (written and wired 2026-11-05; needs the snapshot deleted by hand once, because the hash does not cover the
   Dockerfile, and a live run), D notebook validity (done 2026-11-05,
   `validate_notebooks`), E the task `aisle_pacing_lgbm_ranker_w_test_notebook` run live (also tests verify-as-you-go).
5. **Tools that run in the job process** (built 2026-11-06 in `coding_agent_app/tools.py`, with the reviewer's `show_changes`, and
   imported by the notebook; only `show_changes` has a test (`tests/test_coding_agent_tools.py`); the BigQuery tools rest on IAM and the live runs; first live run 2026-11-06, task `aisle_pacing_lgbm_ranker_w_test_notebook`: the
   agent read the sqlx skill first, composed and dry-ran all three affected tables, 3 of 3 valid; the reviewer's round 1 flagged a real
   gap (the test could not tell which model gets the feature), round 2 satisfied; the task passed in 496s):
   `list_tables`, `get_table_schema` and `dry_run_sql`.
   The first live run (2026-11-06) dry-ran only a trimmed query, not the full compiled tables, so a `sqlx` skill was built
   (`coding_agent_app/skills/sqlx/`, `compose_query.py` with tests in `tests/test_sqlx_skill.py`): it replaces each built table's
   reference with that table's compiled SQL, down to the raw tables, for the changed `.sqlx` files and every table downstream.
   `dry_run_sql` takes `sql_file`, a sandbox path the skill printed, and is built per run like `show_changes`
   (`build_dry_run_sql(sandbox)`); the notebook uploads both skills. The prompt points at the skill, `compact_transcript`
   gives the reviewer each dry run (the tool's result, or the error), and rubric criterion 11 requires a valid dry run of the
   composed file for every changed `.sqlx` file. Confirmed in the second live run (above). Not enforced beyond the reviewer, and
   a later `.sqlx` edit after the last dry run is not detected. Parked: a `PatchCheckMiddleware` check on file modification
   times (`dry_run_sql` returns the composed file's mtime; require `.sqlx` <= `/tmp/compile.json` <= composed file, read
   before the line-ending restore), which would replace criterion 11. Build it only if criterion 11 passes loosely in live runs.
6. **Agent behaviour:** `needs_input` and the structured final answer (`response_format`, wired in the notebook and live-verified 2026-11-08); bounds (a token budget, a
   wall-clock timeout, cancellation, where deleting the sandbox is the hard stop). The agent runs through `astream` with
   `stream_mode="values"` so the latest state survives a stop (built in the notebook 2026-11-06; the step limit is caught and
   the notebook prints the todo list). A stopped task returns a partial result so the main agent can help the user change
   the task: a stop reason (recursion limit, timeout, token budget, context too long, model unavailable, sandbox error, internal error,
   cancelled), the todo list with each item's status, the tool-call count and last calls, and optionally the change list
   so far (diagnosis only, never shipped). The stop reasons tell the main agent what to do: split the task for the
   limits, retry as is for the unavailable ones.
7. **Optional tests when convenient:** the `--3way` apply with a moved `dev` (needs a way to fake it); the ruff
   cleanup task below.
8. **Port and integrate** (each needing approval first): the modules ("The port"), the coding-agent service and its Cloud Tasks queue,
   the Firestore task document and tools, the plan model (with `review`, false by default) and result model, the
   `start_coding_task` rules (needs a saved plan, refused if saved in the current turn), the PR step, and the snapshot
   build script.
9. **Not ours to decide** (`pytest` is now in the ML repo's `requirements.txt`, added 2026-11-05, so the tests the agent
   writes run from the repo's own requirements and the agent never needs to touch that file): `gcsfs` in it; its conventions as `AGENTS.md` or skills; its
   test suite; pointing the main agent's code search at `dev`; whether a proxy can restrict the sandbox's egress;
   LangSmith's plan, retention and residency terms.
10. **Snapshot lifecycle** (decided 2026-11-05). The snapshot holds the environment (Node, the Dataform CLI, Python and
   the pinned libraries), not the repo: every run starts a sandbox from it in about 2 seconds and clones `dev` fresh, so
   an agent's change or a PR revision never touches it. It changes only when `requirements.txt`, the lock file or the
   Dockerfile change on `dev`. While building, the notebook's snapshot cell rebuilds inline on a mismatch (5 to 10
   minutes). In production a task only uses the latest ready snapshot and never rebuilds: a GitHub Action that
   triggers on a change to those files builds the next one. A stale snapshot still works, because verification
   installs what the patch declares. Because deleting a snapshot that a sandbox still uses fails with a 409
   (seen 2026-11-05), the build script builds the new snapshot first, switches to it, and deletes the old one
   best-effort later (a versioned name, such as one carrying the input hash, avoids the delete-first dependency). The
   hash does not cover the Dockerfile today, so a Dockerfile change needs a manual delete until it does.
   `libgomp1` (a system library LightGBM needs) was missing; the agent reported it on 2026-11-06 and it is added to the
   Dockerfile, pending a rebuild, so model code can run in the sandbox on synthetic data on CPU.

Dropped: the `/scratch` backend and the `delete` permission test (the sandbox makes them moot), the standalone
extraction test script, a todo-list criterion in the rubric, and any compile in the loop or after the agent ends.

**Parked (a performance bonus, not needed for the main value; revisit after the notebook skill, the dry-run tool and the
main-agent wiring).** A dry-run check in the loop-back middleware, as a fixable violation. Programmatic version, which needs
no trust in the agent: run `dataform compile --json` in the agent's sandbox (advisory), take the tables defined by the
patch's `.sqlx` files, and dry-run each with `dry_run=True` in the job process. A changed table that reads another changed
table (gold reading a new silver column) has that reference replaced by the upstream's compiled query, recursively, and so
does an unchanged table that reads a changed one (so pass-through columns work). Compile already resolves every `ref()` to
a backticked full table name and lists each table's `dependencyTargets` and `fileName` (field names unconfirmed; check one
real compile output first). Open: incremental tables, operations and assertions (skip at first); repeated references
paste the subquery each time (a `WITH` would be tidier); BigQuery's error positions point into the inlined text; a dry run
may need read access to the referenced tables, not metadata only, which touches the `agent_safe` conversation. The weaker
alternative, checking that the agent called `dry_run_sql` after its last `.sqlx` edit, is redundant if this works.

**Reviewer settings (2026-11-06).** Three grading rounds (`max_iterations=3`: two chances for the agent to fix what is flagged; it was 2 until the 2026-11-06 run, where the agent fixed a flagged test helper but sent a delta report that round 2 flagged with no round left), a progress line per round, prompt caching on the reviewer (`grader_middleware=[AnthropicPromptCachingMiddleware()]`: the nested grader agent gets none by default, and the first reviewed run showed 190,557 input tokens with 0% from cache against 84% for the agent), and the reviewer's token use captured with `UsageMetadataCallbackHandler` on its model (it is in no message, so it was invisible before). `on_evaluation` is a one-way notification: its return value is ignored and exceptions in it are swallowed, so it stores results and cannot steer the loop. A criterion earns its place only if its failure changes the outcome (missing, wrong or out-of-scope work); narration style does not, so the "report refers to an earlier report" half of criterion 8 was dropped (a delta report is still caught by "covers every item"), and an edited notebook cell's cleared execution count is exempt. A run that ends at the round limit with a criterion still failing is shown in the summary (each failing criterion with its gap) and does not fail the task; see "The result" for how the outcome reaches the main agent.

**Proposed: one running task at a time, across all users (noted 2026-11-06; to work through when building
`start_coding_task`).** Parallel tasks conflict in four ways: a revision starts a child task on the same branch, two tasks
touching the same files give conflicting PRs, each run is about a million input tokens against rate and concurrency
limits shared by every user, and "what is it doing?" should have one answer. Those reasons do not stop at the user
boundary, so the cap is global and not per user (first thought per user, then widened the same day); it can be loosened
later to a small number, or per user, if one task at a time proves too tight. Enforce it in `start_coding_task` in code,
like the plan-before-start rule, not in a prompt: refuse with an actionable message that names the running task (and, for
another user's task, says only that one is running, not whose or what), and make the check atomic (a Firestore
transaction that creates the task only if no task is running), since check-then-create can race. A task stuck at
`running` after a crash must not block everyone forever, so it expires (a start time older than the maximum run, or a
stale `progress.updated_at`) and is treated as dead. Open: whether a refused user is queued or just told to wait, and
how a waiting user finds out the slot is free. A revision also needs its parent task finished. Later, a "running task and
its status" control in the Power Apps panel can poll `GET /tasks/{id}`.

**Parked: letting the user cancel a running task (noted 2026-11-06; not built, revisit with the bounds in item 6).** Today
nothing but an admin cancelling the Cloud Run execution, Cloud Run's own task timeout (a termination signal with a short
grace period) or a machine stop can end a task early, and none of those is the user's choice. Options, cheapest first:
(1) a cooperative flag: a main agent tool, `cancel_coding_task(task_id)`, sets `cancel_requested` on the Firestore task
document, and the job's streaming loop checks it after each step, stops, saves the partial result with the stop reason
`cancelled` and deletes the sandbox; graceful, and cheap because the loop already runs on `astream`. (2) a hard stop:
deleting the sandbox makes every tool call fail so the run ends, which is blunt but works when the job is stuck inside one
long call that never returns to the loop. (3) cancelling the execution through the Cloud Run Admin API, which needs the
Cloud Run client library (ask first) and a termination-signal handler that saves state inside the grace period. The
automatic cases need the same handling: a wall-clock timeout set below the job's own task timeout, so the job ends itself
with a partial result and not by being killed. Open: where the user triggers it (a chat message that the main agent turns
into the tool call, or a control in the Power Apps panel calling a gateway endpoint such as `POST /tasks/{id}/cancel`,
see `docs/frontend.md`), and what a cancelled task leaves behind (a `cancelled` status and the todo list; no patch and no PR).

**Rubric (built in the notebook 2026-11-04; first graded runs worked).** `RubricMiddleware` with one fixed rubric of ours for every task, so
quality does not depend on the main agent or the user writing a good task. Scope: only the lines the
diff adds or changes and what they refer to; problems in untouched code are not failures, and a criterion that does not
apply passes. Criteria: every deliverable the task names is in the diff; every assertion column the diff adds is
produced by its own query; every column an added SQL line reads is produced upstream and in scope; every feature name
the diff adds to a Python feature list is a column the final table produces; every function, import or constant an
added Python line uses exists and callers of a changed signature still work; what the report claims about the diff is true (claims about runs are not judged; the clean sandbox checks those) and it lists every changed file and what changed in each;
nothing out of scope or left behind; the added tests do not parse source code, copy a production list, run a training script or read real data or a saved model, and no helper's only job is calling another function; every changed `.sqlx` file has a valid dry run of its composed file (from the dry-run lines `compact_transcript` adds to what the grader reads) (2026-11-06). It does not compare the returned columns with the `columns:` block: the composed file is generated from the compile output, so it cannot be a trimmed copy, and skill step 4 still asks the agent to check the columns itself. The same readability guidance is in the agent's system prompt (2026-11-06): plain, idiomatic code in the style of the neighbouring files, related logic kept in one function, a helper only when reused or needed for clarity, and the smallest test that fails when the behavior breaks. The reviewer criterion lists only concrete patterns, with no reuse counting and no "clever construct" (taste): whether a fixture is worth it is a judgment the reviewer cannot check from one diff. A todo-list criterion was tried and dropped: in the first graded run the agent
wrote its list once and never updated it, and the grader passed the criterion from the diff alone, so it checked nothing. The list grows as new failure types appear.
The grader's system prompt is the middleware's own plus a paragraph telling it to start with `show_changes` and to
use the file tools only to confirm what the changed lines refer to (how to review lives there; what counts as done
lives in the rubric). The grader gets `show_changes` (stages and diffs against the start commit) and read-only `ls`, `read_file`, `glob` and
`grep`, because by default it reads only a compact transcript (the task, the latest todo list, the final report) which
holds the agent's claims and not the code. Its input is limited by the middleware to the first message and the last 30,
each cut at 4,000 characters, so a compact transcript is passed instead: the task and the report as plain text (the thinking block is
left out), each split into messages of up to 3,900 characters so a long one is read whole. The task needed this too: the
reviewer's note on the weekday fix run said "the task text was truncated", and a 5,580-character task had lost its
constraints and report items past the 4,000-character cut, so criteria 1, 8 and 9 had been judged on part of it (found 2026-11-06). The main agent should get a
thorough report, so the cap is 8,000 characters, enforced by the patch-check middleware (a longer report goes back as a
fixable problem), not only asked for in the prompt (decided 2026-11-06). Only the last message is kept, so the agent is told to write the complete report again after a
send-back (a live run 2026-11-05 produced a delta report saying "the rest is unchanged"; criterion 8 now fails that), and
to verify as it goes (compile after each SQL change, run a changed Python function on a tiny DataFrame, scratch files in
`/tmp`). The diff is never truncated, since a partial review is worse than
none: one too big for the grader's context ends as `grader_error`, visible in the report. Motivation: task 2 reported adding `user_aisle_pacing_ratio` to the
final table, but the diff never defines it, so the `rowConditions` assertion and the ranker's feature list pointed at
a column that did not exist, and compile, ruff and the agent's pytest all passed. Advisory, not a gate; judged over
repeated runs. **Optional per task (decided 2026-11-04):** the reviewer costs tokens and time, so it is off by
default; it is the plan's `review` field, which the main agent sets only when the user asks for it, and it is shown
with the rest of the plan before the task starts. Alongside it, stronger todo guidance in the system prompt. Later ideas: the main agent offers the user
the rubric (with a default) when drafting the plan, and a `rubric_additions` field for criteria the user asks for,
appended to the fixed base and shown to the user before the plan starts, never taken from tool results or repo content.
A deterministic check that every feature and `rowConditions` name is an alias in the compiled final table may be added
for this repo, but it is finicky and does not generalise.

**Idea: todo list as progress report.** The agent's todo list could feed the task's progress log, so the main agent can tell the
user how far a long task is. It only works if the agent keeps the list current, which it has not done so far (one
`write_todos` call in 36 tool calls, with every status still pending or in progress at the end). Telling it to update the list did not
change that (still one call), and the middleware's own tool description and prompt already say to mark steps
completed immediately, so the extra sentence was removed. If this is ever wanted, the options are a line appended to
the `write_todos` description or a small middleware that checks the list; read the final list from the agent's state
in the job, not from its report.

**Task ideas for later.** Clean up the 55 findings ruff's default rules report on the ML repo (measured 2026-11-04:
36 unused imports, 17 unsorted imports, 2 others; 53 auto-fixable). The agent runs ruff itself and loops until clean.
It runs under the usual gate; to confirm it worked, ruff's default rules can be run on the changed files and a diff
check can confirm that only import lines changed. The risk is an import that looks unused but has a side effect (`gcsfs` is flagged and
may be needed for `gs://` paths); training can't run in the sandbox (GPU, BigQuery), so review of the draft PR is
the behaviour check.
