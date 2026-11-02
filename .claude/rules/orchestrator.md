---
paths:
  - 'app/orchestrator/**'
  - 'app/shared_helpers.py'
  - 'app/model_schema.py'
  - 'app/exceptions.py'
  - 'tests/test_guardrails.py'
  - 'tests/test_orchestrator.py'
  - 'tests/test_submit_answer.py'
  - 'tests/test_e2e.py'
  - 'tests/test_static_context.py'
  - 'tests/test_conversation_memory.py'
  - 'tests/test_compaction.py'
---

# Orchestrator: verification contract + guardrails

> **Deep dives:** `docs/testing.md` for how these checks get tested (the
> composition test is the important one). Read `docs/approval-workflow.md`
> before building the cost tiers or `/ask/respond`.

**New ideas get built in `notebooks/phase3_graph.ipynb` first, then ported
here once proven** — never the reverse. The notebook keeps its own copy of
this module's logic rather than importing it, specifically so an in-progress
experiment can't leak into the real module through shared mutable state
(`ALL_TOOLS`, `FILE_DESCRIPTIONS`, etc.); resyncing is a manual copy-paste
right after a real change lands here, not an automated step.

## The verification contract — implement exactly

**The model declares its final answer through a forced-shape tool,
`submit_answer` (`.claude/rules/tools.md`), not plain text and not a
`Claim` list.** `agent_node` binds it alongside every real tool; `call_tool_node`
dispatches it exactly like `run_bigquery_sql` — same `ToolMessage` machinery,
same telemetry logging via its args. This replaces the earlier citation-based
`Claim` design entirely: no `Claim` type, no per-claim `source_tool_call_id`.
Reasoning below, under "Not citation, not regex-scanned prose."

```python
class SubmitAnswerArgs(BaseModel):
    answer_markdown: str = ""  # empty only when clarifying_question is set
    all_prose_numeric_claims: list[float]  # every number stated in PROSE as
                                            # fact -- table cells are checked
                                            # separately by extract_table_values,
                                            # not re-declared here. Empty is
                                            # legitimate for a purely
                                            # qualitative answer
    suggested_follow_ups: list[str] = []   # cleared by call_tool_node on question turns
    clarifying_question: str = ""  # set INSTEAD of answer_markdown; exactly one
                                   # of the two is set (model validator)
    title: str = ""                # per-turn tags for conversation_findings
    topics: list[str] = []         # (docs/conversation-memory.md); a validator
    entities: list[str] = []       # strips, dedupes and caps each list at 6

class AgentResponse(BaseModel):
    answer_markdown: str
    sources: list[str]
    needs_approval: bool = False
    chart_urls: list[str] = []
    suggested_follow_ups: list[str] = []
    iteration_cap_hit: bool = False   # see "Hitting max_iterations" below
    pending_query: str | None = None   # set ONLY when needs_approval is True.
                                       # SINGULAR: the one query shown on the
                                       # card. The cache holds pending_queries
                                       # (plural) — every query to execute.
    estimated_cost: str | None = None  # display dollars; set with pending_query
                                       # or cost_cap_exceeded
    cost_cap_exceeded: bool = False    # hard decline — mutually exclusive with
                                       # needs_approval; no approval offered
```

## Graph state — what flows between nodes

`AgentResponse` is the wire format the gateway returns. `AgentState` is the
superset carried *through* the graph — scratchpad included.

```python
from typing import Annotated, Any, TypedDict
from datetime import datetime
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class ToolCallRecord(TypedDict):
    id: str              # matches the AIMessage.tool_calls id
    name: str
    args: dict            # the query lives HERE — {"query": "SELECT ..."} or
                          # {"dax": "EVALUATE ..."}.
    query_text: str | None  # args["query"] or args["dax"], SQL/DAX only, else
                            # None. Derived once at construction, not a second
                            # independently-set field, so it can't drift from
                            # args the way a hand-maintained copy could — it
                            # exists so agent_telemetry's tool_calls RECORD can
                            # be queried directly (`WHERE name = 'run_bigquery_sql'`)
                            # without parsing JSON out of args.
    result: Any  # shape depends on the tool
    success: bool
    error: str | None    # WHICH failure — the message text (always ToolError,
                         # no separate subclasses) is what agent retry behavior
                         # actually reads; `success` alone loses that
    ref_id: str | None    # set only for a successful run_bigquery_sql/run_dax_query/
                          # combine_results call -- "ref_1", "ref_2", ... -- what
                          # generate_chart's source_ref and combine_results'
                          # source_refs resolve against ("Batch ordering" below)
    bytes_billed: int | None  # from the tool's artifact; None for non-BigQuery
                              # or failed calls
    started_at: datetime
    completed_at: datetime

class TurnError(TypedDict):
    stage: str                # a node name: "agent" | "call_tool" |
                              # "check_length" | "verify"
    error_type: str
    message: str
    occurred_at: datetime
    tool_call_id: str | None  # matches ToolCallRecord.id -- direct join, no
                              # index-based matching. None reserved for a
                              # failure with no single originating call (an
                              # LLM call failure in `agent`, not built yet)

def append_list(existing: list, new: list) -> list:
    """Accumulates a list across graph supersteps."""
    return existing + new

class AgentState(TypedDict):
    # Set once, at invocation
    question: str
    conversation_id: str
    user_id: str                 # claims["oid"] — same source as sessions'
                                 # own user_id (`.claude/rules/gateway.md`).
                                 # finalize needs it directly to call
                                 # write_telemetry_row itself
    model: str                   # AskRequest.model (`.claude/rules/gateway.md`) --
                                 # per-request now, not a fixed app/config.py value.
                                 # Carried on PendingApproval too, so a resumed
                                 # turn continues on the same model it paused on
    model_provider: str          # AVAILABLE_MODELS[model], derived once in
                                 # run_agent_turn (`.claude/rules/gateway.md`)
                                 # before build_initial_state -- every branch
                                 # that used to read a module-level MODEL_PROVIDER
                                 # now reads this instead (build_model,
                                 # build_tools_list, build_static_context, below)
    turn_started_at: datetime    # set at the top of the gateway's
                                 # run_agent_turn, before graph.astream(...)
                                 # — finalize's other write_telemetry_row arg
    filter_context: list[dict]
    active_page: str | None      # from report.getActivePage().displayName;
                                 # NOT a filter — folded into the HumanMessage
                                 # each turn (general grounding) AND injected
                                 # into get_page_info directly (`.claude/rules/tools.md`)
    image_base64: str | None

    # Reconstructed history (`build_history_messages()`, `.claude/rules/gateway.md`),
    # set once at invocation. Kept OUT of `messages` deliberately: `messages`
    # gets written back to Firestore each turn as messages_to_dict(state["messages"])
    # (one new sessions.history_messages entry per turn) — if history were seeded
    # into `messages` too, every stored turn would recursively contain every
    # prior one, growing unbounded instead of staying FIFO-trimmed. Prepended
    # at LLM-call time only, same as the static system prompt:
    # model.ainvoke([system_message, *state["history_messages"], *state["messages"]]).
    history_messages: list[BaseMessage]
    history_verified_numbers: list[float]  # verified_numbers of the stored turns still in the
                                           # history (StoredTurn); part of the numeric pool
    notes: list[Note]            # standing notes ({id, kind, text}), read with the history;
                                 # attached with the task plan to the turn's first message at
                                 # request time, never in `messages`
    task_plan: TaskPlan          # {"scope", "tasks"}, read with the history; attached the same way

    # Seeded at invocation with just the question, then accumulated during
    # the loop. Plain BaseMessage, not a wrapper — passes straight to
    # model.ainvoke()/bind_tools() calls with no unwrap step.
    # LangGraph's own add_messages, not append_list: nothing here ever
    # re-emits a message sharing an id with an earlier one, so its
    # dedup-by-id/update-in-place behavior never triggers — it just appends,
    # same as append_list would, without a hand-rolled reducer to maintain.
    # A per-message timestamp was tried and dropped — never read anywhere,
    # and it forced an unwrap at every single site touching this field
    # (which is also what made add_messages unusable in the first place).
    # Per-tool-call timing already exists on ToolCallRecord; per-LLM-call
    # timing is already in LangSmith.
    messages: Annotated[list[BaseMessage], add_messages]
    tool_calls: Annotated[list[ToolCallRecord], append_list]
    iteration_count: int

    # Separate retry budgets — verification and length fail for different
    # reasons; one shared counter lets length failures burn verification's budget
    verification_retry_count: int
    length_retry_count: int

    verified: bool               # set by verify_node; the routing condition
                                 # out of it. Not on AgentResponse — callers
                                 # get a verified answer or an honest decline,
                                 # never a flag to interpret.
    verified_numbers: list[float]  # set by finalize: this turn's claims plus table values when
                                   # verified and not cancelled, else empty. Stored on the turn's
                                   # history entry and in its ledger row

    # Resource accumulators
    bytes_consumed: int          # Cumulative across EVERY tool loop this turn,
                                 # and carried through an approval pause. The
                                 # absolute cap compares against this; the
                                 # per-batch PENDING_APPROVAL_THRESHOLD does not.
                                 # Must be summed live -- per-call figures are
                                 # gone by the time telemetry writes.
    bytes_consumed_baseline: int  # bytes_consumed's value at this phase's start --
                                  # 0 for a fresh turn, seeded from PendingApproval
                                  # on resume. finalize subtracts it so telemetry
                                  # reports only this phase's bytes, never the
                                  # whole cumulative total a pause row already
                                  # reported (`.claude/rules/telemetry.md`).
    prompt_tokens: int           # Set once, in finalize -- sum_token_usage()
    completion_tokens: int       # walks state["messages"] for every AIMessage's
                                 # usage_metadata and totals input/output tokens.
                                 # Deliberately NOT accumulated incrementally
                                 # per LLM call: summing at the end is
                                 # agent-count-agnostic, so a future
                                 # LLM-calling node besides `agent` is picked
                                 # up with no extra code to remember.
    llm_calls: int

    # Turn-level failures. A tool-call-scoped failure carries the matching
    # tool_call_id (direct join to ToolCallRecord.id, one entry per failed
    # call -- not deduped per batch); a future failure with no single
    # originating call (LLM call failures, verification exhaustion, chart
    # failures) would carry tool_call_id=None. A LIST: a turn can survive
    # one failure and hit another; the first is often more diagnostic.
    errors: Annotated[list[TurnError], append_list]

    # Set False at invocation (build_initial_state) -- a turn can't start
    # already cancelled. finalize_node overwrites it with its own live
    # Firestore read; call_tool_node also writes its own live read on every
    # round, purely informational (nothing routes on either write -- routing
    # functions check the flag directly via their own live is_cancelled call,
    # never state["cancelled"], since a routing function can't write state).
    # See "Cancellation".
    cancelled: bool

    # Guardrail outcomes
    needs_approval: bool
    approved_batch: bool            # True on a resumed turn's first batch only --
                                    # lets it skip the pause gate once; reset to
                                    # False in the same call_tool_node return
    approval_decision: str | None   # "approved" | "rejected"; None until resumed
    pending_queries: list[dict]     # {id, query, estimated_bytes} -- every
                                    # BigQuery call in a paused batch. Display/
                                    # telemetry only; resume doesn't dispatch
                                    # from this (`docs/approval-workflow.md`)
    largest_pending_query: str | None  # the query the approval card shows --
                                       # largest estimated_bytes, re-derived by
                                       # index, not a lambda
    paused_at: datetime | None      # set by call_tool_node's pause branch
    deferred_dax: list[dict]        # vestigial -- carried on AgentState/
                                    # PendingApproval but never populated
                                    # (`docs/approval-workflow.md`)
    estimated_cost: str | None
    cost_cap_exceeded: bool
    iteration_cap_hit: bool
    answer_submitted: bool         # True exactly when submit_answer was the
                                   # ONLY tool call_tool_node dispatched this
                                   # round -- recomputed fresh every call_tool_node
                                   # invocation, not accumulated, same pattern as
                                   # cost_cap_exceeded. Batched with a real data
                                   # tool -> stays False, submission ignored;
                                   # route_after_call_tool reads this directly.

    # Building toward AgentResponse
    answer_markdown: str
    clarifying_question: str       # "" unless the turn ended on a clarifying question;
                                   # answer_markdown then holds the same text
    all_prose_numeric_claims: list[float]  # set by call_tool_node from
                                            # submit_answer's args when
                                            # answer_submitted -- verify_node
                                            # checks these plus
                                            # extract_table_values(answer_markdown)
                                            # against build_numeric_pool()
    suggested_follow_ups: list[str]        # same source; flows straight to
                                            # AgentResponse, unlike the claims
    title: str                             # same source; written to
    topics: list[str]                       # conversation_findings by
    entities: list[str]                     # finalize, never to AgentResponse
    chart_urls: list[str]
    sources: list[str]
```

**`cancelled` is written by nodes, not set at invocation.** The flag lives in
Firestore (the gateway and the running turn are separate requests, possibly on
separate instances). A node reads it live and returns it as an ordinary state
update, which is what gives the telemetry writer a source for the `cancelled`
field. `conversation_id` in state is all a node or tool needs for
that lookup. See "Cancellation" below.

**One `AgentResponse` field has no state counterpart — produce it when
building the response:**
- **`pending_query`** — state holds `pending_queries` (plural, all of them).
  Display **the most expensive one**: it's the figure the human is actually
  being asked to approve, and showing a cheaper one understates the decision.

`suggested_follow_ups` used to fall in this category too, before `submit_answer`
— now it's a real `AgentState` field, set the same round as `answer_markdown`.

**Two deterministic checks — plain Python, never another LLM call.**

**Not citation, and not full-prose regex-scanning — pooled matching against
model-declared claims plus automatically-extracted table values.** Two
designs were tried and dropped before this one:
- **Per-claim citation** (an earlier `Claim{text, numeric_value,
  source_tool_call_id}` design): dropped because the citation is exactly as
  model-produced and unverified as the number itself — a false-negative
  failure mode (a real number rejected over a mismatched call id) with no
  real gap closed.
- **Regex-scanning all of `answer_markdown`, prose included, for every
  number**: dropped because prose contains meaningful numbers verification
  was never meant to catch — *"I performed 3 queries"* (process narration)
  or *"the Q3 2024 model"* (a year, not a value) — and the exclusion-rule
  list needed to keep discovering and patching these cases has no natural
  end. Self-reported claims trade some of regex-scanning's completeness (it
  had no way for the model to omit a number from scrutiny) for eliminating
  that open-ended false-positive problem; `docs/llm-judge.md`'s faithfulness
  scoring backstops the residual gap.

**Prose and tables are verified two different ways, deliberately — not one
uniform mechanism for both.** Making the model re-declare every table cell
into `all_prose_numeric_claims` was considered and rejected: a 25-row table (the
existing cap) can mean 50-100+ values, doubling the output-token cost of a
table-heavy answer, and worse, it invites a new failure mode — the model
transcribes a table cell correctly, then mistypes the *same* value in the
claims list, and a genuinely correct table fails verification over a
copy-paste slip that has nothing to do with the data being real.

So: `all_prose_numeric_claims` covers **prose only** — exactly where the model's
judgment is needed to separate a real claim from narration, which a table
cell can never be (a `|` row is data by construction). **Table values are
extracted automatically**, reusing the same table-detection block-scanning
`check_table_rows` already uses (above) — no LLM re-declaration, no
transcription-drift risk, and the model doesn't pay extra tokens re-stating
a table it already wrote.

```python
# NUMERIC_SOURCE_TOOLS and MEMORY_SOURCE_TOOLS live in app/config.py, not
# here -- entry_exit.py's rebuild_paused_messages needs the first too, and
# config.py is the shared, dependency-light module both sides can import
# without pulling in the other.

def build_numeric_pool(tool_calls: list[ToolCallRecord], history_verified_numbers: list[float]) -> set[float]:
    """Every number the answer may cite: this turn's successful query results
    (and combine_results output, which only recombines them, including null-to-0
    fills), every numeric leaf -- never incidental numbers embedded in code, doc
    chunks, or metadata -- plus the verified_numbers of a successful recall and of
    the stored turns still in the history."""
    pool: set[float] = set(history_verified_numbers)
    for tc in tool_calls:
        if not tc["success"]:
            continue
        if tc["name"] in NUMERIC_SOURCE_TOOLS:
            pool.update(extract_numeric_values(tc["result"]))
        elif tc["name"] in MEMORY_SOURCE_TOOLS:
            pool.update(extract_verified_numbers(tc["result"]))
    return pool

def claim_matches_pool(claim: float, pool: set[float]) -> bool:
    """True if claim matches some pool value, or its *100 percentage-scaled
    form, once both are rounded to the claim's own decimal precision --
    precision-aware, not a flat tolerance. Capped at MAX_CLAIM_PRECISION (5)
    since a model reproducing a 15+ digit DAX/BigQuery float can round OR
    truncate a trailing digit -- checked symmetrically (rounded-to-rounded,
    truncated-to-truncated, never crossed) so it covers either direction
    without the false-accept risk a wider flat tolerance, or comparing
    mismatched round/truncate pairs, carries on large numbers. Confirmed
    live (2026-10-02): a real DAX float with its last digit dropped by the
    model was failing verification until this fix; an earlier floor/ceil-
    against-a-single-rounded-claim version of this fix was then found to
    admit a genuine false-accept (two values a full unit apart at the
    target precision could match if each rounded toward the other's
    opposite boundary) -- this symmetric version closes that gap."""
    precision = min(_decimal_places(claim), MAX_CLAIM_PRECISION)
    factor = 10 ** precision
    claim_rounded = round(claim, precision)
    claim_truncated = math.floor(claim * factor) / factor

    def _matches(v: float) -> bool:
        return (round(v, precision) == claim_rounded
                or math.floor(v * factor) / factor == claim_truncated)

    return any(_matches(v) or _matches(v * 100) for v in pool)

def verify_response(state: AgentState) -> tuple[bool, str | None]:
    # 1. Shape check first -- catches "never called submit_answer" (answer_markdown
    # stays "" all turn, since agent_node no longer falls back to response.content)
    # and any malformed submission, before touching the numbers at all.
    try:
        SubmitAnswerArgs.model_validate({
            "answer_markdown": state["answer_markdown"],
            "all_prose_numeric_claims": state["all_prose_numeric_claims"],
            "suggested_follow_ups": state["suggested_follow_ups"],
        })
    except ValidationError as e:
        return False, (f"No valid answer was submitted: {e}. Call submit_answer "
                       "with your final answer_markdown and any numeric claims it makes.")

    # 2. Pooled matching -- declared prose claims plus automatically-extracted
    # table values, both checked against the same pool. Every unmatched claim
    # is collected and reported together, not just the first -- a multi-number
    # answer that fixed one claim per retry was the real bug this replaced.
    pool = build_numeric_pool(state["tool_calls"], state["history_verified_numbers"])
    all_claimed = state["all_prose_numeric_claims"] + extract_table_values(state["answer_markdown"])
    unmatched = [c for c in all_claimed if not claim_matches_pool(c, pool)]
    if unmatched:
        return False, (
            f"{unmatched} do not match any number this answer is allowed to cite: this turn's query results, "
            "numbers returned by get_conversation_findings or search_conversation_findings, or the claims "
            "and table values of earlier turns still in your message history, at the precision they stated "
            "them. Re-run the query for them, restate them exactly as that earlier turn did, or call "
            "get_conversation_findings for older turns.")
    return True, None
```

**The pool has three sources.** This turn's `NUMERIC_SOURCE_TOOLS` results; a successful recall's
`verified_numbers` (`MEMORY_SOURCE_TOOLS`); and `history_verified_numbers`, the `verified_numbers` stored on
each turn still in the history (`StoredTurn`, `.claude/rules/gateway.md`; decided 2026-11-01). The third is the
same set a recall returns for those turns, reachable without a call, so a summary can restate a recent turn's
numbers without a retry. A declined or cancelled turn stores none, and neither a compaction summary nor an
entry written before the field existed has any. Matching is unchanged and precision-aware, so a restatement
can't carry more decimals than the turn stated. What the model is told it may state lives in one place,
`submit_answer`'s description (`.claude/rules/tools.md`).

**`verify_response` checks `answer_markdown` only, never `clarifying_question`.**
On a question turn, `answer_markdown` already holds the question (copied in
`call_tool_node`), so passing both fields here would trip the exactly-one
validator and reject a valid question.

On failure: retry with a corrective message, **max 1–2**, then return an honest
"no verified figure for that." Never emit the number.

**Known limitation, don't solve now:** matching confirms a number appears
*somewhere* in this turn's real data, not that it's semantically the right
value for what the answer claims about it. Not solvable by tightening this
check; `docs/llm-judge.md`'s faithfulness scoring is the layer for that
residual risk.

**Derived numbers (a percentage change, a difference, an average) must come
from the query, not the model's own arithmetic — steered by system
instructions, enforced by this same check regardless of compliance.** If the
model computes something itself instead of the query, that value won't exist
in any tool result, `verify_response` won't find it, and the turn retries —
the prompt only affects how often it succeeds on the first try, never the
safety guarantee. System instructions (`app/orchestrator/context.py`):
*"If you need a value you'd otherwise compute by hand — a rank, percentage
change, difference, ratio, average — you have two options: add it to the
query (RANKX/ROW_NUMBER, a calculated DAX measure, a SQL expression) and
re-run it before calling submit_answer, or leave it out of the table/prose
entirely and describe the pattern in words instead."* A hand-numbered rank
column is the concrete case that surfaced this: not grounded in a live tool
call, so verification correctly rejects it — confirmed live, not
hypothetical. Cross-domain comparison (BigQuery vs. DAX) is Phase 7
(`docs/cross-domain-compute.md`), not covered by this instruction.

**No `is_projection` field — deliberately not carried.** The Phase 7
`run_projection` tool (§9) would need one, because a
projected number has no live tool result to match and would have to be
*exempt* from this check. Add the field **with** that feature, not
before: a field nothing sets and nothing reads is one more thing to keep in
sync across `AgentResponse`, `AgentState`, and telemetry for no current
benefit.

**Scope rule (Phase 3+):** decline forecast/projection questions outright.
*"This system answers from historical/current data and existing model outputs.
It does not generate forecasts or projections."*

**Two helpers you'll need to write** (both pure functions, both worth their
own Layer-1 test — see `docs/testing.md`):
- `extract_numeric_values(raw_result) -> list[float]` — **called by
  `build_numeric_pool`.** Walks nested dicts/lists, returning every number
  found. Handles both BigQuery row dicts and `executeQueries`'
  `results[0].tables[0].rows` shape, where keys are fully qualified
  (`Table[Column]`) — irrelevant here since this returns values, not keys.
- `extract_verified_numbers(result) -> list[float]` — **called by
  `build_numeric_pool` for `MEMORY_SOURCE_TOOLS`** (`get_conversation_findings`,
  `search_conversation_findings`; `docs/conversation-memory.md`). Returns only each
  turn's or hit's `verified_numbers`, so nothing else in the result (`turn`,
  `approx_tokens`, `distance`, decoded `args`) can reach the pool; a dict result (the
  overview or tags view) returns `[]`.
- `extract_table_values(answer_markdown) -> list[float]` — **called by
  `verify_response`.** Shares `_markdown_ast` (mistune's GFM table parser, an
  AST walk) with `check_table_rows` above — not duplicated, per the
  no-pass-through-helpers rule (`CLAUDE.md`), since both need the identical
  "find every real table block" walk. Replaced an earlier hand-rolled regex
  scan after it silently dropped bold-formatted cells (`**66.8%**`) —
  confirmed live before shipping (`docs/build-order.md`). For each cell,
  recursively extracts its text (`_cell_text`, since a bold cell nests inside
  its own AST node) and float-parses it, normalizing commas/`%`/currency,
  skipping cells that aren't numeric. Deliberately scoped to table cells
  only — never runs on prose lines, so it can't reproduce the
  year/version/index false-positive problem `extract_numeric_tokens` had.

**`extract_numeric_tokens` is retired, not revived by `extract_table_values`**
— the latter never touches prose, so it can't reproduce the false positives
that killed the former (previously an open "ask me" item).

## Assembling the prompt

Lives here, not `.claude/rules/gateway.md`, because the only consumer is the
`agent` node below — `app/orchestrator/context.py` and `app/model_schema.py`
are the real files, moved out of `app/gateway/` once it was clear nothing in
the gateway package actually touches them (`docs/build-order.md`, Phase 3
item 3).

- Calling `build_static_context(model_provider, static_text)` — the single
  caching adapter. **Takes the provider, not the model name — model choice is
  per-request now, not a fixed config value** (below), so the dispatch only
  ever needs to know which of the three caching strategies applies, never
  which specific model is live. **Six components in the static block, two of
  them split further** — `SYSTEM_INSTRUCTIONS`
  first, then reference material, with DAX's few-shot examples interleaved
  next to the schema they demonstrate rather than trailing as a separate block:

  1. **`SYSTEM_INSTRUCTIONS`** — role and behavioral rules, established
     before the model sees any reference material.
  2. **Orientation bundle** — exec summary + navigator + architecture summary.
  3. **`TABLE_REGISTRY`** — every semantic-model table with its columns,
     types, and descriptions. Includes *disconnected* tables (measure-only
     and parameter tables), which never appear in relationships, so this is
     their only source.
  4. **`MEASURE_REGISTRY`** — every measure's name and description.
     **Names and descriptions only — never the DAX bodies**, which are
     fetched per-measure by `get_measure_dax`
     (`.claude/rules/tools.md`). Also excludes the numeric what-ifs'
     value measures — `PARAMETERS` already covers them fully.
  5. **`RELATIONSHIPS`** — join paths, parsed from the same artifact. Needed
     on nearly every composition, so present rather than retrieved.
     **`PARAMETERS`** rides along here too: what-if/field parameters with
     their filter column, value measure, and `range` (`{min, max, step}`,
     read from each parameter table's real values, not its formula —
     `docs/data-pipeline.md`).
  6. **DAX few-shot examples** — real, verified query patterns against the
     schema just shown.
  7. **`BIGQUERY_SCHEMA`** — read from `agent_safe` on first real use.

  **BigQuery few-shot examples live in `run_bigquery_sql`'s own tool
  description, not here (2026-10-19) — moved out of the static block, unlike
  DAX's.** `BIGQUERY_FEW_SHOT_EXAMPLES` is a module-level constant in
  `app/orchestrator/tools.py`, appended to `run_bigquery_sql.description`
  right after the `@tool`-decorated function is defined — not inlined into
  the docstring itself, since a docstring can't interpolate a variable
  (an f-string docstring doesn't populate `__doc__` at all, confirmed
  directly: it's a runtime expression, not the compile-time constant Python's
  docstring mechanism requires). `run_bigquery_sql` is already local, not
  MCP-hosted (`.claude/rules/tools.md`), so there's no reusability contract
  its description could violate by carrying this project's real table/query
  patterns — putting them where the model reads them right as it's deciding
  how to call the tool follows the same salience reasoning `submit_answer`'s
  formatting contract did (`.claude/rules/tools.md`). DAX's few-shots stay
  here instead, deliberately: `run_dax_query` *is* MCP-hosted and meant to be
  reusable against a different semantic model, so baking this project's own
  measure/table names into its description would misrepresent it as portable
  when it isn't (`.claude/rules/tools.md` has the full reasoning, including
  the deferred idea of adding *generic*
  DAX examples there instead, once an actual second deployer exists).

  **3 and 4 are why the semantic model isn't vector-searched.** With the whole
  schema in context, measure discovery is deterministic: the model can't fail
  to find one, and can't invent one.

  **Order is part of the contract, but the specific order is a choice, not a
  caching requirement.** A cache hit needs a byte-identical prefix, so
  whatever order is picked must be fixed and everything varying per turn —
  question, filter context, history — goes *after* the `cache_control`
  marker. Interleaving DAX's few-shot examples with its schema, or leading
  with `SYSTEM_INSTRUCTIONS`, costs nothing: the whole block is one static,
  contiguous prefix regardless of the order chosen inside it.

  ```python
  static_text = "\n\n".join([
      SYSTEM_INSTRUCTIONS,
      ORIENTATION_BUNDLE,
      TABLE_REGISTRY, MEASURE_REGISTRY,   # from model_schema.json, at startup
      RELATIONSHIPS, PARAMETERS,          # same artifact, same parse
      DAX_FEW_SHOT_EXAMPLES,
      get_bigquery_schema(),              # lazy, cached on first call
  ])
  ```

  **Tool schemas sit ahead of `system` in the request by construction**, so
  the system-message breakpoint should cover them — caching is prefix-based,
  and everything before the marker is included. Confirm it with the token
  counters rather than assuming: if `cache_read` is materially smaller than
  the tool schemas plus this block, tools aren't being covered and they need
  their own trailing breakpoint. (LangChain's Anthropic middleware places one
  on the last tool definition for exactly this reason, though it's doing so
  for the general case where a system prompt may be absent or dynamic.)

**`filter_context` and `active_page` fold into the turn's `HumanMessage`,
not the static block** — they vary per turn, so they belong after the
`cache_control` marker alongside `question`, per the ordering rule above.
General grounding for the model's prose, separate from `get_page_info`'s own
`active_page` parameter (`.claude/rules/tools.md` explains why both exist).

```python
def build_human_message(question: str, filter_context: list[dict],
                        active_page: str | None) -> HumanMessage:
    content = question
    if active_page:
        content += f"\n\nCurrently viewing: {active_page}"
    if filter_context:
        content += f"\n\nFilters active: {filter_context}"
    return HumanMessage(content=content, additional_kwargs={
        "turn_start": datetime.now(timezone.utc).isoformat()})
```

`turn_start` marks the question as a turn's first message, and its value is the
turn's start time (UTC ISO string). The history cache breakpoints (below) find
turn boundaries with it by truthiness, and compaction uses the time to find the
ledger rows that fall before the first kept turn
(`docs/conversation-memory.md`). The retry notes stay unmarked, user-role
`HumanMessage`s. History stored before 2026-10-28 has `True` here and can't
compact (reading the time fails and the error is logged); a new conversation
replaces it.

## Model schema — one parse, six artifacts

`context/schema/model_schema.json` is built locally by
`scripts/build_model_context.py` — querying live Power BI (`executeQueries` +
the Scanner API), not `.pbip`/TMDL parsing — and committed
(`docs/data-pipeline.md`). Read **once at import**, never Power BI directly.

```python
# app/model_schema.py — module level, evaluated at import
import json
from typing import Literal

_schema = json.loads((CONTEXT_DIR / "schema" / "model_schema.json").read_text())

# -> static context
TABLE_REGISTRY   = _render_tables(_schema["tables"])
MEASURE_REGISTRY = _render_measures(_schema["measures"])       # NAMES + DESCRIPTIONS ONLY
RELATIONSHIPS    = _render_relationships(_schema["relationships"])
PARAMETERS       = _render_parameters(_schema["parameters"])   # whole — nothing withheld

# -> get_measure_dax only, never static context
MEASURE_DAX      = {m["name"]: m["dax"] for m in _schema["measures"]}
MEASURE_NAMES    = Literal[tuple(MEASURE_DAX)]                 # the tool's arg type
```

**All six from one parsed object.** Derived separately they could disagree —
the registry advertising a measure `get_measure_dax` then fails on. From one
object that's structurally impossible. This is also why the module lives at
the top of `app/`, a sibling to `app/config.py`, rather than under
`app/orchestrator/` or `app/mcp_server/`: both the `agent` node here and the
`get_measure_dax` tool (`.claude/rules/tools.md`) need it, and a single
shared parse is what keeps them from disagreeing structurally.

**`_render_measures` must not emit the `dax` field.** It's the one part that
never enters static context (`.claude/rules/tools.md`).

**Schema changes need a redeploy**, since this is read at import. Same
tradeoff as `BIGQUERY_SCHEMA`, now covering the semantic model too.

## Caching dispatch — `build_static_context(model_provider, static_text)`

**Takes the provider, and is called once per turn, not once per process —
a real change from the original design, now that model choice is per-request
(`AskRequest.model`, below) rather than a fixed `app/config.py` value.** The
static *text* is still process-lifetime invariant (see `get_static_context()`
below) — only the wrapping shape varies, by whichever provider the current
turn picked. **Returns the payload that goes into the LLM call's system
slot** — shape differs by provider, which is the whole point of the function.

```python
def build_static_context(model_provider: str, static_text: str) -> list[dict] | str:
    """The system-prompt payload, shaped for this provider's caching mechanism.

    Anthropic -> list of content blocks, the last carrying cache_control.
    OpenAI    -> plain string; caching is automatic, nothing to mark.
    Gemini    -> plain string; implicit caching above its threshold.
    """
    if model_provider == "gemini":
        static_text = static_text + "\n\n" + GEMINI_THINKING_INSTRUCTIONS
        return static_text
    if model_provider == "anthropic":
        # Everything up to the marker caches. Everything volatile —
        # question, filter context, history — goes AFTER this, in messages.
        return [{"type": "text", "text": static_text,
                 "cache_control": {"type": "ephemeral"}}]
    if model_provider == "openai":
        return static_text           # automatic longest-prefix caching
    raise ValueError(f"No caching strategy for provider: {model_provider}")
```

**Only one branch is really "ours".** OpenAI and Gemini cache automatically
with no markup — returning the string unchanged *is* the correct handling,
not a stub. Anthropic is the only provider requiring an explicit breakpoint,
which is also why LangChain ships a caching middleware for Anthropic and none
for the other two.

**`GEMINI_THINKING_INSTRUCTIONS` folds in here, not in `get_static_context()`
— deliberately, now that provider varies per turn.** Appending it inside
`get_static_context()` (the original design) baked in whichever provider was
live at *first* call, process-wide; appending it here means every turn gets
the instructions appropriate to the provider *that turn* actually picked.

**`ttl` can be `"1h"` instead of the 5-minute default**, at a higher
cache-write price. Worth considering here: turns are seconds, but the gap
between a user's questions often isn't — a 5-minute TTL will miss more often
than the write premium costs. Measure before deciding.

**Don't swap in `AnthropicPromptCachingMiddleware`.** It's an
`AgentMiddleware` for `create_agent`; this graph is hand-wired and calls the
model directly, so there's nothing for it to intercept. It also caches
conversation history, which here is a 5-turn window that changes every turn —
churn, not savings.

**`get_static_context()` is still called ONCE per process, not per turn —
but lazily, not at import — because its output no longer depends on which
model/provider a turn picks.** The seven components are all module
constants; nothing about them varies with model choice, so computing the
joined text once is what *guarantees* the byte-identical prefix a cache hit
requires regardless of which provider wraps it. It can't be a bare
module-level constant the way that reasoning first suggests, though:
`static_text` includes `get_bigquery_schema()`, which needs a live
`bigquery.Client()` call, and constructing that at import time would make
importing `app/orchestrator/context.py` require live credentials — breaking
Layer 1 tests the same way an eager Firestore client would. `@lru_cache` on a
zero-arg function gets the same "computed once" guarantee without that cost
— it just moves *when* "once" happens from import time to first real use:

```python
# app/orchestrator/context.py
from functools import lru_cache

@lru_cache
def get_static_context() -> str:
    return "\n\n".join([...])   # the components, in the fixed order
                                 # given under "Assembling the prompt" --
                                 # no build_static_context call here anymore
```

**The component list lives in one place** — that section. Repeating it here
would be a second list to keep in sync, and it drifted exactly that way once
already.

**Per-turn code wraps the cached text for that turn's provider** — `agent_node`
(below) calls `build_static_context(state["model_provider"], get_static_context())`
fresh every turn and wraps it in a `SystemMessage`. The `get_static_context()`
call inside that is cheap regardless — `@lru_cache` means every turn after
the first just returns the same string object, so the per-turn cost is only
`build_static_context`'s own dict-wrap-or-no-op.

LangChain passes a list of content blocks through to Anthropic as-is, so the
same call works for all three providers — only what `build_static_context`
returns for *this turn's* provider differs.

**Verifying it actually cached — confirmed live on all three providers,
2026-10-20, via `notebooks/phase3_graph.ipynb`: inspect
`AIMessage.usage_metadata["input_token_details"]` across a turn's rounds**
(every round's `AIMessage` is in `state["messages"]`, so one multi-tool-call
turn is enough — no need for two separate turns). The exact keys populated
differ by provider, confirmed live rather than assumed:

```python
response.usage_metadata["input_token_details"]
# ANTHROPIC: cache_read behaves as expected -- 0 on the write round, then the
# full static-block size on every later round (a fixed prefix -- Anthropic's
# cache_control marks one explicit breakpoint, so cache_read doesn't grow
# further once that prefix is fully covered). cache_creation is UNRELIABLE,
# though -- confirmed live it reports 0 even on the real write round. The
# real write size shows up instead in the sibling ephemeral_5m_input_tokens/
# ephemeral_1h_input_tokens keys, or in response_metadata's flat
# cache_creation_input_tokens -- a langchain_anthropic mapping gap, not a
# caching failure.
#   round 0: {"cache_read": 0,     "cache_creation": 0, "ephemeral_5m_input_tokens": 32716}
#   round 1: {"cache_read": 32716, "cache_creation": 0, "ephemeral_5m_input_tokens": 0}

# OPENAI: both keys populate correctly and behave as originally expected --
# but because OpenAI has no explicit cache-boundary marker (automatic
# longest-prefix caching), it ends up caching the WHOLE growing prefix each
# round -- the static block plus however much of this turn's tool-call
# history has accumulated by then -- not just the static block the way
# Anthropic's single breakpoint does. cache_read each round equals the
# PRIOR round's total input; cache_creation is just that round's new
# increment.
#   round 0: {"cache_creation": 18421, "cache_read": 0}
#   round 1: {"cache_creation": 706,   "cache_read": 18421}
#   round 2: {"cache_creation": 554,   "cache_read": 19127}

# GEMINI: only cache_read is populated at all -- no cache_creation key
# appears in input_token_details, and response_metadata has no raw
# fallback for it either (just finish_reason/model_name/safety_ratings/
# model_provider) -- confirmed live there's no visibility into the write
# side for this provider/langchain_google_genai version. cache_read still
# grows turn over turn, same growing-prefix pattern as OpenAI.
#   round 0: {"cache_read": 0}
#   round 1: {"cache_read": 19561}
#   round 2: {"cache_read": 21151}
```

**Broken, any provider: `cache_read` stays `0`/absent across every round of
a turn** (not just round 0) — that's the one signal common to all three,
since each provider's *write-side* field is either unreliable (Anthropic),
present (OpenAI), or absent (Gemini). The same numbers show per-LLM-call in
the LangSmith trace tree, so checking during development needs no
instrumentation.

**Raise on an unknown provider, don't fall through to plain text — now
caught earlier than this function, at request validation.** `AskRequest`'s
own `_model_must_be_available` field validator (`.claude/rules/gateway.md`)
rejects a `model` not in `AVAILABLE_MODELS` with a 422 before a turn starts,
so `build_static_context`'s `raise ValueError` for an unrecognized
`model_provider` is now a defense against a config.py authoring mistake
(a model mapped to a provider string outside `{anthropic, openai, gemini}`),
not something a caller's bad input can reach. Still worth keeping: a silent
default would otherwise be the worst case — the request succeeds, the answer
is correct, and the entire static block is re-billed every turn with nothing
to signal it.

**The minimum cacheable prefix is model-specific** (currently 512–4,096
tokens) and below it nothing caches, silently. Seven components including the
table and measure registries puts this block clear of any of
those floors — worth confirming once with real token counts, but not worth
encoding as a table that goes stale.

## Constrained decoding — `strict=True` on every structured call

**A layer *below* verification, not overlapping it.** Every guardrail here
assumes a well-formed tool call or response already arrived, then checks
whether its *content* is acceptable. Constrained decoding stops the sampler
from emitting a shape that doesn't match the schema at all — a different
failure class, currently unguarded.

Apply it on **tool-call argument schemas** — including `submit_answer`'s,
which is how the final answer arrives now (below). Not a separate
structured-output call; the answer is just one more tool call under the same
`strict=True` binding as every other tool.

**It eliminates a failure class rather than speeding up recovery from one.**
If an invalid structure can't be sampled, "malformed output" stops being an
outcome to retry from. **But structural only** — it can guarantee valid JSON,
a real `tool_name`, required fields present. It cannot guarantee the SQL is
right or a claim is backed. The verification retry loop is unchanged; this is
a cheap deterministic filter in front of it.

**Confirmed live across all three providers, 2026-10-19 (Phase 6 item 1,
`docs/build-order.md`) — not just Claude's own schema constraints.**
`submit_answer`'s real schema (the exactly-one-of validator included) binds
under `strict=True` and produces well-formed calls on OpenAI and Gemini
exactly as it already did on Anthropic — no reshaping needed.

**Enabled globally, 2026-10-02 — five tools bound outside it on Claude
(`generate_chart`, plus the memory and state tools `get_conversation_findings`,
`search_conversation_findings`, `manage_tasks` and `manage_notes`), now
provider-conditional.** Claude's `strict=True` rejects `oneOf`/
`discriminator` and `ge=`/`le=` outright, both of which `generate_chart`'s
schema needs (`docs/chart-tool.md`); confirmed `ge=`/`le=` also broke
`SearchDocsArgs.top_k` (`.claude/rules/tools.md`), so the rejection isn't
`generate_chart`-specific. The fix: `init_orchestrator()` pre-converts
`generate_chart`'s stand-in via `convert_to_anthropic_tool()` (no `strict`
kwarg) **only when the turn's `model_provider == "anthropic"`** — confirmed
live that OpenAI/Gemini bind the plain schema directly under `strict=True`
with no workaround at all, so `generate_chart_tool` and the memory tools in
`ANTHROPIC_NON_STRICT_TOOLS` are the pieces of
`build_tools_list(model_provider)`'s assembly (below — rebuilt fresh every
turn, not a module-level list, now that provider is per-request) that
branch by provider; every other tool binds identically regardless. The
memory and state tools (`get_conversation_findings`, `search_conversation_findings`,
`manage_tasks`, `manage_notes`) move out of strict on Claude only because every strict schema shares one
size limit on the compiled grammar (a 400, "compiled grammar is too large");
they stay validated at dispatch.
`agent_node` binds with `bind_tools(build_tools_list(state["model_provider"]),
strict=True)` unconditionally either way. This
also resolved a real live failure: `submit_answer` previously got no
structural guarantee at all, and a live run produced a malformed call (a
model-emitted XML-style fragment inside `answer_markdown`, missing
`all_prose_numeric_claims` entirely) that Pydantic caught but only after the
fact — `strict=True` makes that specific shape unsampleable instead.

## Shared exceptions

**`ToolError` is built — `app/orchestrator/tools.py`, not a separate
`app/exceptions.py`.** It subclasses `langchain_core.tools.ToolException`,
so `handle_tool_error=True` converts it to a `ToolMessage` automatically
inside a tool's own `.ainvoke()` — but `call_tool_node` also catches it
explicitly in a couple of places (`resolve_chart_data`/`resolve_combine_data`,
which run as plain Python *before* any tool's `.ainvoke()`, so nothing
converts it for them automatically). Naming it `ToolError` rather than
raising bare `ToolException` is what makes those explicit catches scoped to
errors this project deliberately raises as recoverable, not any stray
`ToolException` something else might raise for an unrelated reason.

**Neither `ToolTimeoutError` nor `TurnCancelledError` exist — both were
considered, neither was built.** The timeout path (`run_bigquery_sql`'s
`job.result(timeout=...)` + `cancel_job()`) and the Level 2 cancellation
race (below) both just raise a plain `ToolError`. A distinct, non-
`ToolException` exception type for cancellation was the original plan, from
before Level 1's per-routing-function `is_cancelled` check existed — in that
earlier design, the *only* way the graph would learn about a cancellation
was whatever `run_bigquery_sql` itself raised, so the exception's type had
to carry the signal. That stopped being true once every routing function
got its own independent live Firestore read: the "stop the whole turn"
guarantee never depends on which exception type `run_bigquery_sql` raises,
only on the flag `is_cancelled` reads. Reusing `ToolError` also means
`call_tool_node` needs no special `except` clause around the BQ dispatch
`gather()` — `handle_tool_error=True` already turns a cancelled call's
`ToolError` into its own per-call `ToolMessage`, same as any other BQ
error, for free.

## `sources` — ordered provenance badges, built in `finalize`

`AgentResponse.sources` is what the UI renders as badges under the answer
(`docs/frontend.md`). Contract: **friendly labels, in dispatch order, consecutive runs
collapsed with a count.**

```python
from itertools import groupby

# Separate from SOURCE_NAMES (.claude/rules/gateway.md): those are
# present-tense progress messages, these are past-tense badges. No tool jargon
# — the reader is field-operations staff. "Model fields" is the SEMANTIC
# model's, not BigQuery's.
SOURCE_LABELS = {
    "run_bigquery_sql": "Query warehouse",
    "run_dax_query":    "Query dashboard",
    "get_measure_dax":  "Measure lookup",
    "get_page_info":    "PBI page info",
    "get_repo_contents": "Read code",
    "search_docs":      "Doc search",
    "get_conversation_findings": "Searched memory",
    "search_conversation_findings": "Searched memory",
    "manage_tasks": "Managed task plan",
    "manage_notes": "Managed memory",
    "generate_chart":   "Create chart",
    "generate_beeswarm": "Create chart",
    "combine_results":  "Combine results",
}

def build_sources(tool_calls: list[ToolCallRecord]) -> list[str]:
    """Ordered source labels, consecutive runs collapsed with a count.

    -> ["Looked up model fields", "Queried warehouse (3)", "Created chart"]

    Consecutive, not global: schema → query → schema → query stays four
    entries in order, rather than collapsing to two and losing the sequence.
    groupby() groups adjacent equal items, which is exactly that.

    tool_calls isn't already in chronological order as built: call_tool_node
    dispatches run_bigquery_sql calls and other tool calls as two separate
    batches (needed for the cost-cap dry-run gate), so a batch mixing both
    returns bq calls before other calls regardless of the model's actual
    request order. Sort by (started_at, name) first — every call dispatched
    in the same batch shares one timestamp (one asyncio.gather()), so same-batch
    calls land adjacent regardless of that grouping, while different batches
    (different timestamps) stay separated even for the same tool name.
    """
    ordered = sorted(tool_calls, key=lambda tc: (tc["started_at"], tc["name"]))
    names = [SOURCE_LABELS.get(tc["name"], tc["name"])
             for tc in ordered if tc["success"]]
    return [f"{name} ({n})" if (n := len(list(grp))) > 1 else name
            for name, grp in groupby(names)]
```

**Failed calls are excluded** — a call that errored and was retried isn't a
source the answer rests on. Every attempt is still in `agent_telemetry`.

**Built in `finalize`**, where `tool_calls` is complete and ordered.

## Guardrails

| Guard | Bounds | Notes |
|---|---|---|
| Dry-run → `PENDING_APPROVAL_THRESHOLD` (or `TABLESAMPLE`) → approval | Summed bytes of **this batch** | In `call_tool_node`, not the tool — only the node sees the batch |
| **Absolute byte cap** → hard decline | `AgentState.bytes_consumed`, **cumulative across every loop** and carried through a pause | No approval offered above this |
| BigQuery row cap (`max_results`) | Rows returned | Orthogonal to cost |
| DAX row cap (`TOPN` + count check) | Rows returned | Different mechanism than BigQuery |
| Answer-length check | Chars in `answer_markdown`, and rows in any single table | Separate from verification |
| Question-length cap | Chars in the incoming `question` | Gateway request validation, not a turn guard |
| `max_iterations` (50 tool calls, a backstop) | Loop count | Conditional edge |
| Per-tool timeout (40s) | One slow call | Recoverable — agent retries narrower. Sized to leave room under the ~90s gateway budget for retries + synthesis; a long query isn't inherently a wrong one |
| Gateway timeout (~90s) | Whole turn | Enforced in `app/gateway/` |

Each is detailed below.

### Row caps — orthogonal to cost, not covered by it

`LIMIT` does **not** reduce BigQuery bytes scanned (engine scans full columns
first), so a cheap query can still return tens of thousands of rows with
nothing in the cost gate to catch it. Two tools, two mechanisms —
implementation in `.claude/rules/tools.md`:

- **BigQuery:** `query_job.result(max_results=1000)` — caps the *fetch*, no
  query-text rewriting.
- **DAX:** no client-side fetch cap exists. Steer generated DAX toward `TOPN`
  **plus** a deterministic post-fetch row-count check as the real guarantee.
  Power BI's own 100,000-row/15MB ceiling is far too generous to rely on.
- **On exceeding either:** actionable tool error — *"Query returned 1,000+
  rows. Add a filter or aggregate to narrow it."* **Never silently truncate.**

### Answer-length check — display feasibility, not numeric trust

Row caps protect the *tool result*. Nothing stops the model writing a giant
markdown table into `answer_markdown` anyway, and HTML inflates markdown
tables significantly. Kept separate from `verify_response` on purpose — this
is about whether an answer can be *displayed*, not whether its numbers are
real.

**`MAX_ANSWER_CHARS` is a readability ceiling, not a workaround for a
platform limit — confirmed live (2026-09-28) that no such limit exists.**
This previously cited a "hard 16,384-character limit" on the Power Apps HTML
text control; Microsoft's Power Fx docs state Text/Hyperlink/Image/Media
properties have no preset character limit at all (memory is the real
constraint). Corrected here and in `docs/frontend.md`, which also has an open
decision — inline chart images vs. a separate `Image` control — that would
bump this to 7000 if resolved one particular way. Decide before Phase 4's
frontend build-out (`docs/build-order.md`). **Now 14,000 (2026-11-01), measured without image
URLs:** a signed chart URL is about 850 characters, so several charts used up the old budget, and a
long multi-chart summary kept retrying under it.

Two checks, not one: total length, and the size of any single table within
it — a table can blow well past what's reasonable to read in chat while
still fitting under the character cap.

```python
MAX_ANSWER_CHARS = 14000       # readability ceiling, not a platform limit; image URLs are not counted
MAX_ANSWER_TABLE_ROWS = 25     # a table this size is still fine as generate_chart's
                                # input data -- this only ever checks answer_markdown,
                                # never a tool's raw result, so the chart path is untouched

# Separate budgets, deliberately: a length failure must not consume
# verification's retries. Both are the graph's routing conditions.
MAX_LENGTH_RETRIES = 2
MAX_VERIFY_RETRIES = 2

# Shared GFM table parser (mistune, AST walk) -- used by check_table_rows
# here and by extract_table_values below. Replaced an earlier hand-rolled
# regex scan, see that section for why.
_markdown_ast = mistune.create_markdown(renderer="ast", plugins=["table"])

def check_table_rows(answer_markdown: str) -> bool:
    """True if every markdown table in the answer has at most
    MAX_ANSWER_TABLE_ROWS data rows."""
    for block in _markdown_ast(answer_markdown):
        if block.get("type") != "table":
            continue
        body = next((c for c in block["children"] if c["type"] == "table_body"), None)
        if body is None:
            continue
        if len(body["children"]) > MAX_ANSWER_TABLE_ROWS:
            return False
    return True


def extract_image_urls(answer_markdown: str) -> list[str]:
    """Every markdown image URL in the answer, in order (walks paragraphs, tables, lists)."""
    ...


def answer_text_length(answer_markdown: str) -> int:
    """Characters in the answer, not counting image URLs: they render as pictures, not text."""
    return len(answer_markdown) - sum(len(url) for url in extract_image_urls(answer_markdown))


async def check_length_node(state: AgentState) -> dict:
    answer = state["answer_markdown"]
    length = answer_text_length(answer)
    if length <= MAX_ANSWER_CHARS and check_table_rows(answer):
        return {}
    if length > MAX_ANSWER_CHARS:
        note = (f"Your answer is {length:,} characters; the limit is {MAX_ANSWER_CHARS:,}, not counting image "
                "URLs. Summarize the key findings concisely, or generate a chart instead of listing rows.")
    else:
        note = (f"Your answer includes a table with more than {MAX_ANSWER_TABLE_ROWS} rows. "
                "Show only the top results, summarize the rest, or generate a chart "
                "instead of listing every row.")
    return {
        "length_retry_count": state["length_retry_count"] + 1,
        "messages": [HumanMessage(content=note)],
    }
```

**`check_table_rows`'s table-detection logic — the same block-scanning
pattern `extract_table_values` reuses below**, for the verification check.
One shared way of recognizing "this is a real markdown table," not two.

1. **Steering:** many rows → summarize (top N, key stats) and/or offer a chart
   via `generate_chart`. System prompt also tells the model the real
   `MAX_ANSWER_TABLE_ROWS` figure directly (`app/orchestrator/context.py`), so
   it can plan around a known number instead of guessing.
2. **Backstop:** on failure, loop back — **same retry pattern and 1–2 cap as
   verification.** Never truncate silently; that can cut off a partial-answer
   caveat mid-sentence.
3. **Retries exhausted:** a fixed decline, not another attempt at the answer
   — *"I wasn't able to generate a response short enough to display. Try
   breaking your question into smaller, more specific parts."* No numbers —
   `check_length` runs before `verify` (`finalize`'s table, below), so
   nothing here has been verified yet.

### Question-length cap — bounded at the gateway, not here

Answer-length is bounded here; the incoming `question` is bounded as
**gateway request validation** before a turn starts. Implementation lives in
`.claude/rules/gateway.md` — it isn't a turn guardrail.

### Cost tiers and the approval pause

Three tiers, the byte-vs-dollar decision rule, and the full pause/resume
design are in `docs/approval-workflow.md` — single source, read it before
touching `call_tool_node`'s cost logic.

**Three things that are easy to get wrong — full design in
`docs/approval-workflow.md`, read it before building this:**
1. **The cost gate is in `call_tool_node`, not in the tool.** Dry-run every
   BigQuery call, decide once on the **summed** bytes (plus `TABLESAMPLE`'s
   unconditional force), then execute. Per-tool gating can't see the batch —
   three individually-cheap queries would each pass while blowing the cap
   together. `asyncio.gather()` is also the join point: never inspect
   results as they arrive and return early, or a finished sibling's result
   is abandoned and the resumed turn re-runs it.
2. **The pause caches the whole turn, not just the query.** A compound
   question may have already run `search_docs`; caching only the pending query
   would discard it. Cached in `live_turns/{conversation_id}` in Firestore
   — **not process memory**, which wouldn't survive Cloud Run's multiple
   concurrent instances (`.claude/rules/gateway.md`). Telemetry is never
   read back for this.
3. **On approve, resume replays the dangling `AIMessage.tool_calls` through
   the ordinary graph — no separate execution path.** `route_entry` sends
   it straight to `call_tool`, which re-dispatches the exact batch that
   paused (BigQuery and everything else requested alongside it), then the
   loop continues completely normally from there — `agent` may run once or
   several more times, exactly as it would for any other turn, not a single
   special "write the final answer" call. On reject, no graph runs at all —
   a canned response and a minimal telemetry row, written directly by the
   gateway (`docs/approval-workflow.md`).

**The cap (50) is a backstop, not the working limit.** The turn timeout ends a
turn long before 50 rounds, and a request too large for one turn is handled by
the task plan (`manage_tasks`, `docs/conversation-memory.md`) and the soft time
guard, which are more graceful than hitting this cap. It only bounds a runaway
loop if the timeout is ever lifted. `iteration_count` is still logged per turn
(`.claude/rules/telemetry.md`).

### Hitting `max_iterations` — partial answer, not a blanket refusal

Different failure mode from verification exhaustion: the model ran out of
budget, possibly mid-progress.

1. **`route_after_call_tool` sends `iteration_cap_hit` back to `agent`, not
   straight to `finalize`** — `agent` still needs its own turn to actually
   write `answer_markdown` from whatever it has so far, the same path any
   normal final answer takes. No special-casing a repeat attempt: if `agent`
   tries another tool call anyway, `call_tool` checks the cap again like any
   other turn and produces the same actionable "answer now" message — same
   symmetric handling as `cost_cap_exceeded`, not a one-time allowance. Cheap
   even if it recurs (the check short-circuits before any real tool runs),
   and bounded regardless by the gateway's own turn timeout.
2. **The model must state plainly that the answer is partial** and name what
   wasn't reached. System prompt: *"If you're stopped by the iteration limit
   before fully answering, state clearly that this is a partial answer and
   specify what you weren't able to address."*
3. `iteration_cap_hit: True` lets the UI render it distinctly rather than
   relying on the prose being read carefully.
4. **Verification still applies normally** to whatever answer results — cap
   exhaustion never bypasses it.

### DAX grounding — structural, with no pre-call requirement

**No "search before query" rule.** The table schema and measure registry are
in static context on every turn (`.claude/rules/gateway.md`), so the model
composes DAX against a schema it can already see. There's no retrieval step
to require first, and nothing to reject on.

**Do NOT block on measure-name validation.** Validating every `[Bracket]`
rejects valid novel compositions — the agent must stay free to build new logic
from real parts (`DIVIDE([Hits], [Total])`), and a regex can't separate a
*consumed* reference from one *defined* inline (`SUMMARIZECOLUMNS(..., "Avg
Recall", ...)`, `DEFINE MEASURE`, `VAR`). The engine already rejects
nonexistent measures. Instead, on engine failure append a fuzzy-match hint:
*"Measure `[Recall@5]` not found. Closest: `[Recall at 5]`"*. Advisory only.

### Batch ordering — stop `generate_chart` running before its data exists

`generate_chart`'s spec classes, required fields, and rendering contract are
in `docs/chart-tool.md`; what matters here is only *when* it may be dispatched.

LangGraph dispatches a batch concurrently, so a data-fetch call and a
`generate_chart` referencing it in the *same* batch would race. **Not solved
with an explicit same-batch id check (`check_batch_ordering`, the originally
planned design) — superseded by how `source_ref` resolution actually works.**
A chart call's `source_ref` is matched against `ref_id`s already present in
`state["tool_calls"]`, which only holds *prior rounds'* results — the current
round's own records aren't accumulated into state until after `call_tool_node`
returns. A same-batch reference therefore can't resolve at all;
`_lookup_chart_source` raises the same actionable `ToolError` it would for any
other unresolvable reference, with no separate ordering check needed:

```python
CHARTABLE_TOOLS = {"run_bigquery_sql", "run_dax_query", "combine_results"}

def _lookup_source_ref(source_ref: str, prior_tool_calls: list[ToolCallRecord]) -> ToolCallRecord:
    """Finds the successful, chartable tool call source_ref points at, or
    raises an actionable error if it can't be found."""
    source_tc = next(
        (r for r in prior_tool_calls
         if r.get("ref_id") == source_ref and r["success"] and r["name"] in CHARTABLE_TOOLS),
        None)
    if source_tc is None:
        raise ToolError(
            f"'{source_ref}' is not a successfully completed run_bigquery_sql, "
            "run_dax_query, or combine_results call from earlier this turn -- it may not "
            "exist, may have failed, or may be from later in this same batch and hasn't "
            "run yet. Fetch the data first, then reference it in a follow-up step.")
    return source_tc
```

`ref_id` (`"ref_1"`, `"ref_2"`, ...) is assigned only to a successful
`run_bigquery_sql`/`run_dax_query`/`combine_results` result and printed back to the model
alongside that result, so it has something real to copy into `source_ref` —
never the raw tool-call id (`source_tool_call_id`, the field's original name,
dropped along with the id-based design).

### Cancellation — two levels, because stopping and killing are different

`POST /ask/cancel` writes `cancel_requested: true` to
`live_turns/{conversation_id}` and returns immediately. It's a separate,
concurrent request — Firestore is the mailbox between it and the running
turn, and it has to be Firestore rather than memory because the two requests
may land on different Cloud Run instances.

**Level 1 — built, 2026-10-09: a check in every routing function, not a gate
inside the nodes.** The originally-sketched design here had nodes set
`state["cancelled"]` and a conditional edge read it. That's not what shipped
— a routing function can't update state at all (LangGraph conditional edges
return only a destination string), so there's no way for one node's check to
hand the *next* routing decision anything. What actually works: every one of
the five routing functions (`route_entry`, `route_after_agent`,
`route_after_call_tool`, `route_after_check_length`, `route_after_verify`) is
`async def` and independently calls `is_cancelled` first, returning
`"finalize"` directly if it's set — covering every edge in the graph, not
just the ones leading into slow nodes. `is_cancelled` itself lives in
`app/shared_helpers.py`, not here — the first thing in this
whole codebase where orchestrator-layer code reads Firestore directly rather
than the gateway:

```python
async def is_cancelled(conversation_id: str) -> bool:
    snap = await live_turn_doc(conversation_id).get()
    return bool(snap.exists and snap.to_dict().get("cancel_requested", False))

async def route_after_call_tool(state: AgentState) -> str:
    if state["needs_approval"] or await is_cancelled(state["conversation_id"]):
        return "finalize"
    if state["answer_submitted"]:
        return "check_length"
    return "agent"   # also cost_cap_exceeded and iteration_cap_hit
```

Since routing functions can't write `state["cancelled"]`, `finalize_node`
does its own, separate `is_cancelled` read to decide the canned message and
populate the telemetry row — same flag, read once more at the one place
that actually needs to act on it, regardless of which routing function sent
it there. It also overrides `needs_approval` to `False` whenever cancelled —
a cancelled turn no longer needs approval for anything, and leaving the old
value in place would write a self-contradicting telemetry row.

**`Command(goto=...)` still doesn't belong on a node that already has a
conditional edge**, for whenever Level 2 (below) does add a node-level
check inside `call_tool_node`. LangChain's docs are explicit — dynamic
routing *or* static edges per node, never both; when both exist, *both*
destinations execute.

**Level 2 — built and live-verified, 2026-10-09: inside `run_bigquery_sql`
itself.** Node-level routing checks can't help once execution is already
*inside* a node awaiting a query — they only run between nodes. Only this
level can kill a live BigQuery job, and the `cancel_job()` machinery already
existed for timeouts; this widens what triggers it rather than building it
from scratch.

```python
# A plain LangChain @tool, not MCP-registered (`.claude/rules/tools.md`).
# conversation_id is Annotated[str, InjectedToolArg] -- confirmed live that
# Field(exclude=True) does NOT hide an arg from the model-facing schema (it
# only controls .model_dump() serialization); InjectedToolArg does, and the
# value still reaches the function when injected manually at the call site
# (call_tool_node's full_bq_calls list), same shape as the MCP tools'
# exclude_args + injection-function pattern.
@tool(args_schema=BigQuerySqlArgs, response_format="content_and_artifact")
async def run_bigquery_sql(query: str, conversation_id: Annotated[str, InjectedToolArg]) -> tuple[list[dict], dict]:
    # maximum_bytes_billed is the hard fail-safe: BigQuery kills the job
    # server-side if it exceeds this, independent of anything below.
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)
    job = await asyncio.to_thread(get_bq_client().query, query, job_config=job_config)

    query_task = asyncio.create_task(asyncio.to_thread(_wait_for_query_result, job))
    cancel_task = asyncio.create_task(watch_for_cancel(conversation_id))
    try:
        first_completed, _ = await asyncio.wait({query_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        query_task.cancel()
        cancel_task.cancel()
        _cancel_job_quietly(job.job_id)
        raise

    if cancel_task in first_completed:
        query_task.cancel()
        _cancel_job_quietly(job.job_id)
        raise ToolError("Cancelled by user request.")

    cancel_task.cancel()
    return await query_task
```

**No `job.result_async()` — it doesn't exist.** Confirmed directly against
the installed `QueryJob` class before writing this (an earlier version of
this section assumed it did): only a sync `result()`. The real race wraps
that sync call in `asyncio.to_thread` (`_wait_for_query_result`, a plain
function taking `job` as a real parameter rather than closing over it, so
it reads as its own step in the race) — the same mechanism the existing
timeout path already used, nothing native-async to reach for here.

**Poll `is_cancelled` once a second, in its own watcher task — not inside
the query's own await path.** Checking Firestore on every tick of the
query's own wait would tax every successful query on the hot path to catch
a cancel that rarely comes. `watch_for_cancel` is its own small coroutine
(`while not await is_cancelled(conversation_id): await asyncio.sleep(1)`),
racing the real query via `asyncio.wait`; it's cancelled the instant the
query wins. 1s, not 2-3s — matches the frontend's own status-poll cadence
rather than lagging behind it; the cost is a cheap Firestore read, bounded
by however long the query itself runs.

**`run_dax_query` is best-effort only** — the Power BI REST path has no
server-side cancellation, so closing the connection is the only lever and the
query may keep running regardless (XMLA, the documented backup, would
support real cancellation — component reference §8). `run_bigquery_sql`'s
cancellation is real, confirmed live — a materially stronger claim than
DAX's best-effort one, worth stating precisely rather than leaving both
implied as equivalent.

**Cancellation is genuinely honored, but not instant — confirmed live, not
assumed.** A deliberately slow, free-to-run test query (`GENERATE_ARRAY` +
`CROSS JOIN` over literal arrays, no real table scanned, so `bytes_billed`
stays 0) surfaced a real BigQuery characteristic worth keeping straight: a
query with no shuffle/sort/aggregation stage can run to full completion
*regardless* of `cancel_job()` succeeding — BigQuery checks for a
cancellation between execution stages, and a single pipelined compute stage
has none. Adding a `GROUP BY`/`ORDER BY` (forcing real stage boundaries)
confirmed `cancel_job()` does genuinely stop a job — `error_result` showed
`{"reason": "stopped", "message": "Job execution was cancelled: User
requested cancellation"}` directly from BigQuery's own job metadata — but
the job still took ~21s to actually reach `DONE`, even though the cancel
request landed only a few seconds after the query started. The turn's
response to the user is still immediate (this code never waits for
BigQuery's own confirmation before returning); cost exposure during that
gap is bounded by the existing `maximum_bytes_billed` fail-safe regardless
of how long the actual stop takes.

**`run_bigquery_sql` also needs an `except asyncio.CancelledError:` handler,
separate from the watcher race above — now a confirmed, not just traced,
propagation path.** When the *gateway's own* turn timeout fires,
`asyncio.wait_for` cancels the task directly — confirmed from
`asyncio.timeouts.Timeout`'s installed source: `_on_timeout` calls
`self._task.cancel()` where `self._task` is `tasks.current_task()`, not a
separately-wrapped child, so the signal lands exactly wherever that single
task is currently suspended. That reaches all the way down through
`consume_graph` → `graph.astream` → `call_tool_node`'s `asyncio.gather` →
`.ainvoke()` → `run_bigquery_sql`'s own `await asyncio.wait(...)`, as long
as nothing in that chain introduces its own `asyncio.create_task` boundary
— whether LangGraph's own `astream`/node-dispatch internals preserve that
or quietly break it was an open question (CLAUDE.md's "Known-unverified"
list had an entry for exactly this). Now settled, with a permanent
regression test (`test_gateway_timeout_cancellation_reaches_bigquery_job`,
`tests/test_tools.py`) that drives the *real* compiled `graph` — not a
bare call to `run_bigquery_sql` — under a short `asyncio.wait_for`, against
a fake BigQuery client with no live credentials (`approved_batch: True`
sends `route_entry` straight to `call_tool`, so no real LLM call is
needed), and confirms the fake job actually gets cancelled. This is also
*why* an earlier plan to have the gateway's timeout handler write
`cancel_requested: true` as a fallback was dropped — by the time that
handler runs, `asyncio.wait_for` has already torn down the task tree,
including whatever watcher task would have read that flag. Nothing is left
to act on it; confirmed, the stale `# TODO(item 15)` comment that had
proposed it was deleted, not implemented.

A cancelled turn still writes telemetry — `cancelled: True` plus whatever
`tool_calls` completed (`.claude/rules/telemetry.md`).

### The graph — seven nodes

**Named functions, not lambdas, for every conditional edge** — easier to
read once a routing condition has more than one branch, and traces by name
rather than `<lambda>` if LangSmith ever surfaces the routing step.

**Always pass `path_map`, confirmed live.** Without it, `add_conditional_edges`
has no way to know what a routing function might return — it's just a
Python function LangGraph calls at runtime — so `graph.get_graph()` can't
draw the real edges either; it fabricates a fallback straight to `END` and
leaves every other destination disconnected. Routing itself still works
correctly either way — this is a visualization/introspection gap, not a
runtime one — but `path_map` closes it and makes each router's real
destinations explicit in the code, not just inferable from reading its body.

```python
from langgraph.graph import StateGraph, START, END

async def route_entry(state: AgentState) -> str:
    # A resumed, approved turn replays its dangling AIMessage.tool_calls
    # straight into call_tool -- a fresh turn starts at agent as usual.
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    return "call_tool" if state["approved_batch"] else "agent"

async def route_after_agent(state: AgentState) -> str:
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    return "call_tool" if state["messages"][-1].tool_calls else "check_length"

async def route_after_call_tool(state: AgentState) -> str:
    if state["needs_approval"] or await is_cancelled(state["conversation_id"]):
        return "finalize"
    if state["answer_submitted"]:
        return "check_length"
    return "agent"   # also cost_cap_exceeded and iteration_cap_hit — see
                     # "Hitting max_iterations"; symmetric, no special routing

async def route_after_check_length(state: AgentState) -> str:
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    answer = state["answer_markdown"]
    if answer_text_length(answer) <= MAX_ANSWER_CHARS and check_table_rows(answer):
        return "verify"
    if state["length_retry_count"] < MAX_LENGTH_RETRIES:
        return "agent"
    return "finalize"

async def route_after_verify(state: AgentState) -> str:
    if await is_cancelled(state["conversation_id"]):
        return "finalize"
    if state["verified"]:
        return "finalize"
    if state["verification_retry_count"] < MAX_VERIFY_RETRIES:
        return "agent"
    return "finalize"


g = StateGraph(AgentState)
g.add_node("agent",            agent_node)
g.add_node("call_tool",        call_tool_node)
g.add_node("check_length",     check_length_node)
g.add_node("verify",           verify_node)
g.add_node("finalize",         finalize_node)

# A list, not a dict path_map, once every value already matches the real
# node name -- "finalize" appears here even though route_entry/route_after_agent's
# own logic above never names it directly; it's only ever reached via the
# shared is_cancelled check each one runs first.
g.add_conditional_edges(START, route_entry,
    ["call_tool", "agent", "finalize"])

g.add_conditional_edges("agent", route_after_agent,
    ["call_tool", "check_length", "finalize"])
g.add_conditional_edges("call_tool", route_after_call_tool,
    ["finalize", "check_length", "agent"])
g.add_conditional_edges("check_length", route_after_check_length,
    ["verify", "agent", "finalize"])
g.add_conditional_edges("verify", route_after_verify,
    ["finalize", "agent"])

g.add_edge("finalize", END)
graph = g.compile()
```

**`agent` never writes `answer_markdown` directly — the model has to call
`submit_answer` to produce one.** No `response.content` fallback: if the
model ends a round with plain text instead of calling a tool,
`answer_markdown` simply stays whatever it was before (`""` on a fresh turn),
which is exactly the signal `verify_node`'s `model_validate` step needs to
catch "never submitted" (above).

```python
ALL_TOOLS = {
    **MCP_TOOLS,
    "run_bigquery_sql": run_bigquery_sql,
    "submit_answer": submit_answer,
    "get_measure_dax": get_measure_dax,
    "search_docs": search_docs,
    "get_page_info": get_page_info,
    "generate_beeswarm": generate_beeswarm,
}
# None of these six is MCP-based (`.claude/rules/tools.md`) -- MCP_TOOLS
# alone would leave all six uncallable.

def build_tools_list(model_provider: str) -> list[BaseTool]:
    """Stand-in tools (generate_chart, combine_results) + every other real
    tool, assembled fresh per turn -- cheap, no I/O. Replaces an earlier
    module-level BIND_TOOLS_LIST computed once in init_orchestrator(); that
    stopped being correct once model_provider became per-request instead of
    a fixed app/config.py value -- a list built once at startup for whichever
    provider happened to be configured then would silently keep using it
    for every turn, regardless of what a later turn's AskRequest.model asked
    for.

    generate_chart's entry is swapped for a stand-in schema (source_ref +
    spec, `.claude/rules/tools.md`) -- dispatch still uses ALL_TOOLS, which
    keeps the real MCP-loaded tool object. Pre-converted to a raw Anthropic
    tool dict (no strict kwarg) only for Anthropic, so bind_tools(strict=True)
    below passes it through untouched there -- OpenAI/Gemini bind the plain
    schema directly, confirmed live."""
    generate_chart_tool = (convert_to_anthropic_tool(chart_tool_call_standin)
                           if model_provider == "anthropic" else chart_tool_call_standin)
    stand_in_tool_names = ["generate_chart", "combine_results"]
    stand_in_tools = [generate_chart_tool, combine_results_call_standin]

    non_stand_in_tools = []
    for name, tool in ALL_TOOLS.items():
        if name in stand_in_tool_names:
            continue
        if model_provider == "anthropic" and name in ANTHROPIC_NON_STRICT_TOOLS:
            tool = convert_to_anthropic_tool(tool)     # {"get_conversation_findings", "search_conversation_findings"}
        non_stand_in_tools.append(tool)
    return non_stand_in_tools + stand_in_tools


def build_model(model: str, model_provider: str) -> BaseChatModel:
    """Bare, correctly-configured chat model for model_provider -- unbound;
    agent_node binds the tools list the same way regardless of provider.
    Takes both as parameters now, not a module-level MODEL/MODEL_PROVIDER --
    model choice is per-request (below)."""
    if model_provider == "anthropic":
        return ChatAnthropic(model=model, thinking={"type": "adaptive", "display": "summarized"}, effort="medium")
    if model_provider == "openai":
        return ChatOpenAI(model=model, use_responses_api=True,
                           reasoning={"effort": "low", "summary": "auto"})
    if model_provider == "gemini":
        return ChatGoogleGenerativeAI(model=model, reasoning_effort="low", include_thoughts=True)
    raise ValueError(f"No model factory branch for provider: {model_provider}")


async def agent_node(state: AgentState) -> dict:
    """Extended thinking enabled for frontend display value -- incompatible
    with tool_choice="any" (confirmed live: "Thinking may not be enabled when
    tool_choice forces tool use"), so a plain-text finish without
    submit_answer is possible again; verify_node's retry already recovers
    from that. No cost difference: thinking tokens bill the same regardless
    of display."""
    model = build_model(state["model"], state["model_provider"]).bind_tools(
        build_tools_list(state["model_provider"]), strict=True)
    system_message = SystemMessage(content=build_static_context(
        state["model_provider"], get_static_context()))
    history = state["history_messages"]
    if state["model_provider"] == "anthropic":
        history = mark_anthropic_history_cache(history)
    messages = state["messages"]
    if state["notes"] or state["task_plan"].get("tasks"):
        messages = attach_context(messages, state["notes"], state["task_plan"])
    seconds_left = GATEWAY_TURN_TIMEOUT_SECONDS - (now - state["turn_started_at"]).total_seconds()
    if seconds_left <= TURN_WARNING_SECONDS_REMAINING:
        messages = [*messages, HumanMessage(content=time_check)]    # the text is in orchestrator.py
    response = await model.ainvoke([system_message, *history, *messages])
    return {"messages": [response], "llm_calls": state["llm_calls"] + 1}
```

**History is cached with two Anthropic breakpoints (2026-10-26).**
`mark_anthropic_history_cache` puts `cache_control` on the last message of the
previous stored turn and of the newest one, found via the `turn_start` marker.
The older one exactly matches the previous turn's newest breakpoint, so a turn
reads earlier history from cache and writes only the newest turn; with one
breakpoint, a turn past the 20-position lookback window re-wrote the whole
history. Confirmed live. It needs byte-stable history: `build_history_messages`
sorts map keys (`.claude/rules/gateway.md`), and a history that has
that grows without bound defeats it (the first entry changes every turn),
which compaction prevents. OpenAI and Gemini cache automatically.

**What `agent_node` adds to the request, never to state.** The saved notes, then the task plan, go ahead
of the turn's first message (`attach_context`; each block's header says it is current as of this message
and that earlier `manage_notes`/`manage_tasks` calls in the history are already reflected) and, in the last `TURN_WARNING_SECONDS_REMAINING` seconds of the turn budget, a time check goes at
the end. Both are built on copies at request time, so neither is in `state["messages"]`, neither is saved
to history, and both sit after the cache breakpoints (`docs/conversation-memory.md`).

**`state["model"]`/`state["model_provider"]` drive every branch above —
per-turn now, not a fixed `app/config.py` pair read once at startup.**
`AskRequest.model` (`.claude/rules/gateway.md`) defaults to `app/config.py`'s
`MODEL` but can be overridden per request; `run_agent_turn` derives
`model_provider` from it (`AVAILABLE_MODELS[model]`) and seeds both onto
`AgentState`/`PendingApproval`, so a resumed turn continues on the same model
it paused on rather than whatever `app/config.py` currently says — confirmed
live, including through a pause/resume round trip. OpenAI's
`use_responses_api=True` isn't optional: plain `/v1/chat/completions` flatly
rejects combining `reasoning_effort` with tool calling for a reasoning
model — confirmed live, not a style choice. Each provider picks up its API
key from the environment — **`init_orchestrator()` now fetches all three
providers' secrets unconditionally at startup**, not just the configured
one, since which provider a given turn needs is no longer known until that
turn's `AskRequest` arrives; none of the three pass `api_key=`/
`google_api_key=` explicitly.

| Node | Role |
|---|---|
| `agent` | The LLM call, tools bound. Emits tool calls only — `answer_markdown` only ever arrives via `submit_answer` |
| `call_tool` | Dispatches tools (including `submit_answer`), increments `iteration_count`, sets `answer_submitted`, owns the cost gate |
| `check_length` | Owns `length_retry_count` |
| `verify` | `verify_response()`. Owns `verification_retry_count` |
| `finalize` | Sets `AgentResponse` fields and writes telemetry — six routes in, see below |

- `route_entry` is a plain function wired straight onto `START`'s
  conditional edge, not a node — checking `is_cancelled` plus
  `approved_batch` doesn't warrant one. All five routing functions, not just
  `call_tool`'s, now check cancellation first (`"Cancellation"` above) —
  that's the actual cancel check, not a node property.
- **Cost gating is not a node** — it lives inside `call_tool_node`, which
  dry-runs every BigQuery call in the batch before dispatching anything
  (`docs/approval-workflow.md`). `call_tool` routes on what that decision
  produces.
- `check_length` before `verify` — cheap check first; no point walking the
  answer's numbers against tool data if it can't be displayed regardless.
- `finalize` is the **single exit** — full breakdown below.

**Any routed-back retry into `agent` needs a fresh message first, confirmed
live, not assumed.** Claude refuses to generate a new response when the
conversation already ends in an assistant turn ("This model does not support
assistant message prefill. The conversation must end with a user message.")
— exactly what `agent`'s own prior tool-call-free `AIMessage` leaves it as.
`check_length`'s retry message and `verify_node`'s (both `model_validate`
failure and a claim that didn't match) aren't optional styling — each is what
keeps that specific retry path alive at all. `submit_answer`'s own dispatch
already produces a real `ToolMessage`, satisfying the "conversation can't end
on an assistant turn" requirement on its own — the *retry* message on top of
that is what actually carries the corrective instruction.

**`SubmitAnswerArgs` is a minimal model, not the full `AgentResponse`.**
Fields like `needs_approval` and `pending_query` are guardrail outcomes
`finalize` assembles from graph state, not something the LLM should be asked
to set — constraining the tool's schema to exactly what the model actually
produces (the answer, its claims, follow-ups) is what `strict=True` schemas
are for (below).

### `finalize` — the single exit, five ways in

`check_length` and `verify` each collapse two different outcomes into one
edge. `iteration_cap_hit` and `cost_cap_exceeded` aren't their own routes —
`route_after_call_tool` sends both straight back to `agent`, and each rides
along as a flag on whichever row below `agent` eventually reaches (above).
Kept in sync with the actual conditional edges above; read those directly if
this table and the code ever disagree.

| From | Condition | Outcome | `AgentResponse` fields `finalize` sets | Telemetry |
|---|---|---|---|---|
| any routing function | `is_cancelled` (live Firestore read, not `state["cancelled"]` -- nothing sets that until Layer 2 lands) | Cancelled | `answer_markdown` = `CANCELLED_MESSAGE` always, even if `verify_node` already confirmed an answer (a cancelled turn is discarded, never written to history); `needs_approval` forced to `False` regardless of what `call_tool` last set it to | Normal row, `cancelled: True`, `needs_approval: False` |
| `call_tool` | `needs_approval` (only reached if not also cancelled) | Approval pause | `answer_markdown` = `PENDING_APPROVAL_MESSAGE`; `needs_approval: True`, `pending_query` (the largest), `estimated_cost` | **Pause row**, `approval_decision: null` (`.claude/rules/telemetry.md`) |
| `check_length` | too long, `length_retry_count` exhausted | Length decline | Fixed decline: couldn't produce a short enough answer, suggests breaking up the question | Normal row |
| `verify` | `verified: True` | Success | Full assembly: `answer_markdown`, `sources` (`build_sources`), `suggested_follow_ups`; `iteration_cap_hit`/`cost_cap_exceeded` set `True` if either is how this turn got here | Normal row |
| `verify` | failed, `verification_retry_count` exhausted | Honest decline | `VERIFICATION_FAILURE_MESSAGE` — no unverified number emitted | Normal row |

`answer_markdown`'s four canned-message constants (`VERIFICATION_FAILURE_MESSAGE`,
`PENDING_APPROVAL_MESSAGE`, `CANCELLED_MESSAGE`, plus the success case's real
text) are picked by one `if`/`elif`/`elif`/`else` chain in `finalize_node`,
in that priority order — **`cancelled` first** (a cancel request wins over a
pause determined in the same tick, and a cancelled turn is always discarded:
`CANCELLED_MESSAGE`, never a draft or even a verified answer, so what the user
sees matches what history and the ledger keep), then `needs_approval`, then
`not verified`, then the real answer.

**`finalize` also writes `conversation_findings`** (`docs/conversation-memory.md`):
when `verified and not cancelled`, it builds the row from the values it already
holds plus `verified_numbers` (`all_prose_numeric_claims` +
`extract_table_values(answer_markdown)`) and awaits it in an `asyncio.gather`
with the telemetry write. The same list is returned as state `verified_numbers` (empty
otherwise), which `build_updated_history` stores on the turn's history entry. The findings writer logs its own failures, so a
ledger gap can't fail the turn.

**`finalize` never touches `live_turns` — that lives in `consume_graph`
(`app/gateway/gateway.py`) instead.** `live_turns` is gateway-owned
(`.claude/rules/gateway.md`); splitting its writes across two modules risks
drift. `finalize` only sets the `AgentState` fields above and writes
`agent_telemetry` (and the findings row, above). `consume_graph`'s own `finally` block, after the `astream`
loop ends, decides what happens to the document:

```python
try:
    ...
finally:
    if final_state is not None and final_state["needs_approval"] and not final_state["cancelled"]:
        await live_turns_doc.set({
            "status": "Approval workflow",
            "pending_approval": build_pending_approval(final_state),
        })
    else:
        await live_turns_doc.delete()
```

**Cancelled wins over needs_approval here too — the same narrow, same-tick
race as the message-selection chain above.** Without the `not cancelled`
guard, a cancel landing in the same tick `call_tool_node` sets
`needs_approval` would still get a `pending_approval` card written for a
query the user just said to stop, even though the message they saw already
said "Cancelled." `finalize_node`'s own override of `needs_approval` to
`False` (above) makes this condition redundant in practice — but it's kept
anyway, as cheap insurance against anything else ever setting
`needs_approval` without going through `finalize_node`.

`build_pending_approval` (`app/gateway/entry_exit.py`) is pure, no I/O — the
schema and carry rule are in `.claude/rules/gateway.md`. No `merge=True`:
nothing else in the document matters once paused, so a full replace is
simpler and smaller than merging.

**Why not in `finalize`:** `astream` yields a node's update only after it
finishes. A `finalize`-side write would run *before* `consume_graph`'s own
per-node status write for that same step, which would then re-create the
document, orphaned with no TTL (`live_turns` has none, unlike `sessions`).

### Node names are a design decision, and status depends on them

The gateway consumes this graph with **`astream`, not `ainvoke`** (see
`.claude/rules/gateway.md`) so it can report per-node progress. That means
**node names are part of the contract**, not throwaway labels: the status
mapping matches on them. Rename a node and the gateway's status mapping must
change in the same step.

### Smaller, easy to miss

- **Cancellation must be explicit** — `asyncio` cancellation alone doesn't
  stop a running BigQuery job. Call `client.cancel_job(job.job_id)` in the
  timeout handler.
- **`AgentState` doesn't carry raw results across turns** — `messages` and
  `tool_calls` are seeded fresh at each invocation; nothing accumulates
  turn over turn in memory. What a later turn sees of an earlier one is only
  what's reconstructed from Firestore, deliberately bounded by
  compaction and per-turn redaction (`.claude/rules/gateway.md`).
- **Confirm parallel dispatch isn't accidentally serialized** — LangGraph runs
  multiple tool calls concurrently by default. A free win, not something to
  build.

### Why flat tool-calling, not sub-agents

More LLM round-trips work against the 5–15s target, and citation granularity
gets murky once a result comes from a sub-agent. If DAX quality becomes a
problem, escalate in order: more DAX few-shots → richer tool descriptions →
forced tool choice → *then* sub-agents. Decide from eval metrics, not feel.

## LangSmith tracing — local and deployed

`LANGSMITH_TRACING`/`LANGSMITH_API_KEY`/`LANGCHAIN_PROJECT` give a per-turn
trace of the agent loop: every LLM call, tool call, and iteration, nested in
order. **Not deploy flags** — `LANGSMITH_TRACING` and `LANGCHAIN_PROJECT` are
literals in `app/config.py` (identical in every environment), and
`LANGSMITH_API_KEY` is fetched via `get_secret()` at startup like every other
secret. All three get pushed to `os.environ` inside `init_orchestrator()`
(2026-10-02), the same place `ANTHROPIC_API_KEY` is set — confirmed this is
early enough: nothing calls a LangChain tracing check before the first real
LLM call, so it doesn't need to happen before `langchain_anthropic`/
`langgraph` are *imported*, only before that first call. Once set, every
`ainvoke()` auto-attaches a `LangChainTracer` — confirmed at the source level
(`langchain_core.callbacks.manager._configure`) — no explicit callback wiring
needed anywhere else.

**No flush call on turn end, deliberately.** Trace uploads go through a
background thread by default, and Cloud Run throttles CPU to near-zero the
instant a response is sent — the same trap already solved for telemetry
writes, confirmed via independent sources, not just this project's own
assumption. `langchain_core.tracers.langchain.wait_for_all_tracers()` is the
real mechanism to force a synchronous flush (**not** an env var —
`LANGCHAIN_CALLBACKS_BACKGROUND`, documented here until 2026-10-02, does not
exist anywhere in the installed SDK; confirmed by source search, not
assumed). Decided not to call it: traces are a debugging aid, not something
the judge/golden-dataset pipeline depends on (`agent_telemetry` is the real
record), so an occasional dropped trace is an accepted risk, not worth the
added complexity.

**Separate from `agent_telemetry`, not a replacement**
(`.claude/rules/telemetry.md`). Traces are a per-turn debugging view; the
BigQuery table is the queryable record that feeds the judge. Different jobs.

**Traces are queryable programmatically, not just via the LangSmith UI** —
`langsmith.Client().list_runs(...)` (a separate package from `langchain-core`)
filters by project/time/tags/metadata against the same data already being
sent. No setup beyond the tracing config above.

Setup: `local-dev-environment-setup.md` Step 17. Why tracing runs in
production here rather than Cloud Trace: component reference §3.
