---
paths:
  - 'app/orchestrator/**'
  - 'app/model_schema.py'
  - 'app/exceptions.py'
  - 'tests/test_guardrails.py'
  - 'tests/test_e2e.py'
  - 'tests/test_approval_e2e.py'
  - 'tests/test_static_context.py'
---

# Orchestrator: verification contract + guardrails

> **Deep dives:** `docs/testing.md` for how these checks get tested (the
> composition test is the important one). Read `docs/approval-workflow.md`
> before building the cost tiers or `/ask/respond`.

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
    answer_markdown: str = Field(min_length=1)  # non-empty is what lets
                                                 # verify_node's model_validate
                                                 # tell "never submitted" apart
                                                 # from a real answer
    all_prose_numeric_claims: list[float]  # every number stated in PROSE as
                                            # fact -- table cells are checked
                                            # separately by extract_table_values,
                                            # not re-declared here. No min,
                                            # empty is legitimate for a purely
                                            # qualitative answer
    suggested_follow_ups: list[str] = []   # no min, same reasoning

class AgentResponse(BaseModel):
    answer_markdown: str
    sources: list[str]
    needs_approval: bool = False
    chart_url: str | None = None
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
from typing import Annotated, TypedDict
from datetime import datetime
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class ToolCallRecord(TypedDict):
    id: str              # matches the AIMessage.tool_calls id — what
                         # generate_chart's source_tool_call_id resolves
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
    result: list[dict] | str   # query tools: one dict per ROW, native Python
                               # types (no serialization in state). Other tools
                               # return their own shape. FastMCP JSON-encodes
                               # whatever the tool returns for the model.
    success: bool
    error: str | None    # WHICH failure — agent retry behavior distinguishes
                         # ToolError from ToolTimeoutError; `success` alone loses that
    started_at: datetime
    completed_at: datetime

class TurnError(TypedDict):
    stage: str                # a node name: "execute_approved" | "agent" |
                              # "call_tool" | "check_length" | "verify"
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
    # (one new sessions.recent_messages entry per turn) — if history were seeded
    # into `messages` too, every stored turn would recursively contain every
    # prior one, growing unbounded instead of staying FIFO-trimmed. Prepended
    # at LLM-call time only, same as the static system prompt:
    # model.ainvoke([SYSTEM_MESSAGE, *state["history_messages"], *state["messages"]]).
    history_messages: list[BaseMessage]

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

    # Resource accumulators
    bytes_consumed: int          # Cumulative across EVERY tool loop this turn,
                                 # and carried through an approval pause. The
                                 # absolute cap compares against this; the
                                 # per-batch BIG_QUERY_THRESHOLD does not. Must
                                 # be summed live -- per-call figures are gone
                                 # by the time telemetry writes.
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

    # Written mid-run by a node that finds the Firestore cancel flag set —
    # never at invocation, where it would always be False. See "Cancellation".
    cancelled: bool

    # Guardrail outcomes
    needs_approval: bool
    pending_queries: list[dict]    # {id, query} — every BigQuery call to run
                                   # on approve. Also the route_entry branch:
                                   # non-empty means this is a resumed turn.
    deferred_dax: list[dict]       # {id, dax} — deferred at the pause (its
                                   # results would otherwise sit in Firestore).
                                   # Seeded from PendingApproval on resume.
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
    all_prose_numeric_claims: list[float]  # set by call_tool_node from
                                            # submit_answer's args when
                                            # answer_submitted -- verify_node
                                            # checks these plus
                                            # extract_table_values(answer_markdown)
                                            # against build_numeric_pool()
    suggested_follow_ups: list[str]        # same source; flows straight to
                                            # AgentResponse, unlike the claims
    chart_url: str | None
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
def build_numeric_pool(tool_calls: list[ToolCallRecord]) -> set[float]:
    """Every number across this turn's non-search_docs, non-submit_answer
    tool results."""
    pool: set[float] = set()
    for tc in tool_calls:
        if tc["success"] and tc["name"] not in ("search_docs", "submit_answer"):
            pool.update(extract_numeric_values(tc["result"]))
    return pool

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
    # table values, both checked against the same pool.
    pool = build_numeric_pool(state["tool_calls"])
    all_claimed = state["all_prose_numeric_claims"] + extract_table_values(state["answer_markdown"])
    for claim in all_claimed:
        if not any(abs(claim - v) < 0.01 for v in pool):
            return False, f"{claim} does not match any tool result this turn."
    return True, None
```

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
*"When your answer needs a computed value — a percentage change, a
difference, a ratio, an average — add it to the query itself (a calculated
DAX measure, a SQL expression) rather than computing it in your response.
Only compare BigQuery and DAX results directly when no query can produce
the computed value already — Phase 7 (`docs/cross-domain-compute.md`)."*

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
- `extract_table_values(answer_markdown) -> list[float]` — **called by
  `verify_response`.** Same table-block-detection *shape* as `check_table_rows`
  (a `|---|`-style separator as a block's 2nd line means it's a real table) —
  its own independent scan, not a shared call, per the no-pass-through-helpers
  rule (`CLAUDE.md`). For each detected block, float-parses every cell,
  normalizing commas/`%`/currency, skipping cells that aren't numeric.
  Deliberately scoped to table cells only — never runs on prose lines, so it
  can't reproduce the year/version/index false-positive problem
  `extract_numeric_tokens` had.

**`extract_numeric_tokens` is retired, not revived by `extract_table_values`**
— the latter never touches prose, so it can't reproduce the false positives
that killed the former (previously an open "ask me" item).

## Assembling the prompt

Lives here, not `.claude/rules/gateway.md`, because the only consumer is the
`agent` node below — `app/orchestrator/context.py` and `app/model_schema.py`
are the real files, moved out of `app/gateway/` once it was clear nothing in
the gateway package actually touches them (`docs/build-order.md`, Phase 3
item 3).

- Calling `build_static_context(model, static_text)` — the single caching
  adapter. **Takes the model name, not a provider**, so switching models is a
  one-line config change with nothing else to update. **Seven components in
  the static block, two of them split further** — `SYSTEM_INSTRUCTIONS`
  first, then reference material, with the few-shot examples interleaved
  next to the schema each set demonstrates rather than trailing as one block:

  1. **`SYSTEM_INSTRUCTIONS`** — role and behavioral rules, established
     before the model sees any reference material.
  2. **Orientation bundle** — exec summary + navigator + architecture diagram.
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
  8. **BigQuery few-shot examples** — same reasoning as 6, next to their schema.

  **3 and 4 are why the semantic model isn't vector-searched.** With the whole
  schema in context, measure discovery is deterministic: the model can't fail
  to find one, and can't invent one.

  **Order is part of the contract, but the specific order is a choice, not a
  caching requirement.** A cache hit needs a byte-identical prefix, so
  whatever order is picked must be fixed and everything varying per turn —
  question, filter context, history — goes *after* the `cache_control`
  marker. Interleaving the few-shot examples with their schema, or leading
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
      BIGQUERY_FEW_SHOT_EXAMPLES,
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
    return HumanMessage(content=content)
```

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

## Caching dispatch — `build_static_context(model, static_text)`

**Takes the model name, not a provider.** Switching models is a one-line
config change; nothing else needs updating. **Returns the payload that goes
into the LLM call's system slot** — shape differs by provider, which is the
whole point of the function.

```python
def build_static_context(model: str, static_text: str) -> list[dict] | str:
    """The system-prompt payload, shaped for this model's caching mechanism.

    Anthropic -> list of content blocks, the last carrying cache_control.
    OpenAI    -> plain string; caching is automatic, nothing to mark.
    Gemini    -> plain string; implicit caching above its threshold.
    """
    if model.startswith("claude-"):
        # Everything up to the marker caches. Everything volatile —
        # question, filter context, history — goes AFTER this, in messages.
        return [{"type": "text", "text": static_text,
                 "cache_control": {"type": "ephemeral"}}]
    if model.startswith(("gpt-", "o1-", "o3-")):
        return static_text           # automatic longest-prefix caching
    if model.startswith("gemini-"):
        return static_text           # implicit caching above its threshold
    raise ValueError(f"No caching strategy for model: {model}")
```

**Only one branch is really "ours".** OpenAI and Gemini cache automatically
with no markup — returning the string unchanged *is* the correct handling,
not a stub. Anthropic is the only provider requiring an explicit breakpoint,
which is also why LangChain ships a caching middleware for Anthropic and none
for the other two.

**`ttl` can be `"1h"` instead of the 5-minute default**, at a higher
cache-write price. Worth considering here: turns are seconds, but the gap
between a user's questions often isn't — a 5-minute TTL will miss more often
than the write premium costs. Measure before deciding.

**Don't swap in `AnthropicPromptCachingMiddleware`.** It's an
`AgentMiddleware` for `create_agent`; this graph is hand-wired and calls the
model directly, so there's nothing for it to intercept. It also caches
conversation history, which here is a 5-turn window that changes every turn —
churn, not savings.

**Call it ONCE per process, not per turn — but lazily, not at import.**
Both inputs are invariant — the seven components are module constants,
`MODEL` is config — so computing it once is what *guarantees* the
byte-identical prefix a cache hit requires. It can't be a bare module-level
constant the way that reasoning first suggests, though: `static_text`
includes `get_bigquery_schema()`, which needs a live `bigquery.Client()`
call, and constructing that at import time would make importing
`app/orchestrator/context.py` require live credentials — breaking Layer 1
tests the same way an eager Firestore client would. `@lru_cache` on a
zero-arg function gets the same "computed once" guarantee without that cost
— it just moves *when* "once" happens from import time to first real use:

```python
# app/orchestrator/context.py
from functools import lru_cache

@lru_cache
def get_static_context() -> list[dict] | str:
    static_text = "\n\n".join([...])   # the components, in the fixed order
                                        # given under "Assembling the prompt"
    return build_static_context(MODEL, static_text)
```

**The component list lives in one place** — that section. Repeating it here
would be a second list to keep in sync, and it drifted exactly that way once
already.

**Per-turn code just calls it** — the return value is the `system` argument,
unchanged:

```python
llm = init_chat_model(MODEL)          # MODEL is a hardcoded literal in app/config.py
response = await llm.ainvoke([SystemMessage(content=get_static_context()), *messages])
```

LangChain passes a list of content blocks through to Anthropic as-is, so the
same call works for all three providers — only what `build_static_context`
returned at import differs.

**Verifying it actually cached** — two counters on the response:

```python
response.usage_metadata["input_token_details"]
# first turn:  {"cache_creation": 12400, "cache_read": 0}      cache written
# later turns: {"cache_creation": 0,     "cache_read": 12400}  cache HIT
# broken:      {"cache_creation": 0,     "cache_read": 0}      not caching
```

Both zero on a Claude model means caching silently isn't happening. The same
numbers show per-LLM-call in the LangSmith trace tree, so checking during
development needs no instrumentation.

**Raise on an unknown model, don't fall through to plain text.** A silent
default is the worst case: the request succeeds, the answer is correct, and
the entire static block is re-billed every turn with nothing to signal it.
Failing at startup on a typo'd model name is cheap; finding it on an invoice
is not.

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

**Confirm at build time — this is provider-specific, which matters here given
model-swappability:** OpenAI's `strict: true` carries real schema constraints
(every field in `required`, optionality as nullable types, `additionalProperties:
false`), so enabling it may mean *reshaping* a schema, not just flipping a
flag. Gemini has an equivalent enforced JSON-schema mode, but its exact
parameter surface through the LangChain wrapper — and how closely its
guarantees match OpenAI's — needs verifying, not assuming. Same category as
`query_job.result()`'s param name.

## Shared exceptions

`ToolError` and `ToolTimeoutError` are referenced throughout these docs but
aren't defined anywhere yet. Define them once in a shared module (e.g.
`app/exceptions.py`) rather than per-tool — the agent's retry behavior depends
on distinguishing "recoverable, fix your input" from "timed out, try
narrowing." Confirm the module location before creating it.

## `sources` — ordered provenance badges, built in `finalize`

`AgentResponse.sources` is what the UI renders as badges under the answer
(`docs/frontend.md`). Contract: **friendly labels, in dispatch order, consecutive runs
collapsed with a count.**

```python
from itertools import groupby

# Separate from FRIENDLY_TOOL_NAMES (.claude/rules/gateway.md): those are
# present-tense progress messages, these are past-tense badges. No tool jargon
# — the reader is field-operations staff. "Model fields" is the SEMANTIC
# model's, not BigQuery's.
SOURCE_LABELS = {
    "run_bigquery_sql": "Queried warehouse",
    "run_dax_query":    "Queried dashboard data",
    "get_measure_dax":  "Looked up a measure",
    "get_page_info":    "Read dashboard page",
    "list_repo_files":  "Browsed project code",
    "read_repo_file":   "Read project code",
    "search_docs":      "Searched documentation",
    "generate_chart":   "Created chart",
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
| Dry-run → `BIG_QUERY_THRESHOLD` → approval | Summed bytes of **this batch** | In `call_tool_node`, not the tool — only the node sees the batch |
| **Absolute byte cap** → hard decline | `AgentState.bytes_consumed`, **cumulative across every loop** and carried through a pause | No approval offered above this |
| BigQuery row cap (`max_results`) | Rows returned | Orthogonal to cost |
| DAX row cap (`TOPN` + count check) | Rows returned | Different mechanism than BigQuery |
| Answer-length check | Chars in `answer_markdown`, and rows in any single table | Separate from verification |
| Question-length cap | Chars in the incoming `question` | Gateway request validation, not a turn guard |
| `max_iterations` (10 tool calls) | Loop count | Conditional edge |
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
markdown table into `answer_markdown` anyway — and the Power Apps HTML control
has a hard **16,384-character limit** (HTML inflates markdown tables
significantly). Kept separate from `verify_response` on purpose — this is
about whether an answer can be *displayed*, not whether its numbers are real.

Two checks, not one: total length, and the size of any single table within
it — a table can blow well past what's reasonable to read in chat while
still fitting under the character cap.

```python
MAX_ANSWER_CHARS = 6000        # real margin below 16,384 — HTML inflation + card chrome
MAX_ANSWER_TABLE_ROWS = 25     # a table this size is still fine as generate_chart's
                                # input data -- this only ever checks answer_markdown,
                                # never a tool's raw result, so the chart path is untouched

# Separate budgets, deliberately: a length failure must not consume
# verification's retries. Both are the graph's routing conditions.
MAX_LENGTH_RETRIES = 2
MAX_VERIFY_RETRIES = 2

def check_answer_length(answer_markdown: str) -> bool:
    return len(answer_markdown) <= MAX_ANSWER_CHARS


def check_table_rows(answer_markdown: str) -> bool:
    """True if every markdown table in the answer has at most
    MAX_ANSWER_TABLE_ROWS data rows."""
    lines = answer_markdown.split("\n") + [""]
    rows, block = 0, []
    for line in lines:
        if line.strip().startswith("|"):
            block.append(line)
            continue
        # A real table needs a separator row (e.g. |---|---|) as its 2nd
        # line -- otherwise this is just prose that happens to start with "|".
        if len(block) >= 2 and re.match(r"^\|[\s\-:|]+\|$", block[1].strip()):
            rows = max(rows, len(block) - 2)
        block = []
    return rows <= MAX_ANSWER_TABLE_ROWS


def check_answer_displayable(answer_markdown: str) -> bool:
    return check_answer_length(answer_markdown) and check_table_rows(answer_markdown)


async def check_length_node(state: AgentState) -> dict:
    answer = state["answer_markdown"]
    if check_answer_displayable(answer):
        return {}
    if not check_answer_length(answer):
        note = ("Your answer is too long to display. Summarize the key findings "
                "concisely, or generate a chart instead of listing rows.")
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
1. **The cost gate is in `call_tool_node`, not in the tool.** Three phases:
   dry-run every BigQuery call, decide once on the **summed** bytes, then
   execute. Per-tool gating can't see the batch — three individually-cheap
   queries would each pass while blowing the cap together. Phase 3's
   `asyncio.gather()` is also the join point: never inspect results as they
   arrive and return early, or a finished sibling's result is abandoned and
   the resumed turn re-runs it.
2. **The pause caches the whole turn, not just the query.** A compound
   question may have already run `search_docs`; caching only the pending query
   would discard it. Cached in `live_turns/{conversation_id}` in Firestore
   — **not process memory**, which wouldn't survive Cloud Run's multiple
   concurrent instances (`.claude/rules/gateway.md`). Telemetry is never
   read back for this.
3. **On approve, don't re-run the agent loop** — execute the cached queries,
   then one `agent` call writes the final answer across old + new results.
   On reject, reuse the `iteration_cap_hit` partial-answer machinery.

**10 is a calibration starting point, not a tuned value.** Exhaustion degrades
to a partial answer (below), so a high cap costs nothing — it just reveals how
many iterations real questions need. `iteration_count` is logged per turn for
exactly this (`.claude/rules/telemetry.md`): set the real cap above the 95th
percentile of *successful* turns, not the average — the DAX-heavy tail is what
a low cap truncates.

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
`generate_chart` referencing it in the *same* batch would race.

```python
def check_batch_ordering(tool_calls: list[dict]) -> None:
    ids = {tc["id"] for tc in tool_calls}
    for tc in tool_calls:
        ref = tc.get("args", {}).get("source_tool_call_id")
        if ref and ref in ids:
            raise ToolError(
                f"{tc['name']} references '{ref}' from this same batch. "
                "Fetch first, wait for the result, then call it in a follow-up step."
            )
```

### Cancellation — two levels, because stopping and killing are different

`POST /ask/cancel` writes `cancel_requested: true` to
`live_turns/{conversation_id}` and returns immediately. It's a separate,
concurrent request — Firestore is the mailbox between it and the running
turn, and it has to be Firestore rather than memory because the two requests
may land on different Cloud Run instances.

**Level 1 — a gate at the top of each node.** Stops the graph advancing into
further work. **Set state and let the conditional edge route — don't raise,
and don't return `Command`.** Raising unwinds the graph, forcing the
gateway's handler to reconstruct whatever `tool_calls`, tokens, and
`bytes_consumed` were accumulated; a plain state update keeps it intact so
telemetry writes the real picture.

**Do not add `Command(goto=...)` to a node that already has a conditional
edge.** LangChain's docs are explicit — use dynamic routing *or* static
edges per node, never both — and when both exist, *both* destinations
execute. The conditional edge below already tests `cancelled`, so the node
only needs to set it.

```python
async def call_tool_node(state: AgentState) -> dict:
    if await is_cancelled(state["conversation_id"]):
        return {"cancelled": True}      # the conditional edge routes to finalize
    ...

async def is_cancelled(conversation_id: str) -> bool:
    doc = await firestore_client.collection("live_turns").document(conversation_id).get()
    return doc.exists and doc.to_dict().get("cancel_requested", False)
```

**Level 2 — inside `run_bigquery_sql`.** Node-level checks can't help once
execution is already *inside* a node awaiting a query. Only this level can
kill a live BigQuery job, and the `cancel_job()` machinery already exists for
timeouts — this widens what triggers it.

```python
# A plain LangChain @tool, not MCP-registered (`.claude/rules/tools.md`) —
# dispatch is in `docs/approval-workflow.md`.
@tool
async def run_bigquery_sql(query: str, conversation_id: str):
    # maximum_bytes_billed is the hard fail-safe: BigQuery kills the job
    # server-side if it exceeds this, independent of anything below.
    job = client.query(query, job_config=QueryJobConfig(
        maximum_bytes_billed=ABSOLUTE_CAP))
    query_task  = asyncio.create_task(job.result_async())
    cancel_task = asyncio.create_task(watch_for_cancel(conversation_id))

    done, pending = await asyncio.wait(
        {query_task, cancel_task}, timeout=40,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()

    # Success wins even if a cancel landed on the same tick — the job already
    # completed and was already paid for; discarding a ready result to report
    # "cancelled" is the wrong outcome.
    if query_task in done:
        return query_task.result()

    # asyncio cancellation alone won't stop the job — but this call can itself
    # fail (transient API error). Swallow and log: an unhandled exception here
    # would mask the real ToolTimeoutError AND leave the job running. Worst
    # case the scan completes unread, bounded by maximum_bytes_billed.
    try:
        client.cancel_job(job.job_id)
    except Exception as e:
        logger.warning("cancel_job failed for %s: %s", job.job_id, e)

    raise TurnCancelledError() if cancel_task in done else ToolTimeoutError(
        "Query exceeded 40s. If the question genuinely needs this much data, "
        "narrow the date range or aggregate further; otherwise check for a "
        "missing filter or join condition.")
```

**Race, don't poll inside the await.** Polling Firestore every second *inside*
the query's own await path would tax every successful query on the hot path
to catch a cancel that rarely comes. `watch_for_cancel` still polls
internally (Firestore listeners are the alternative, with their own
complexity), but a 2–3s interval is plenty for a human clicking a button, and
it's cancelled the instant the query wins.

**`run_dax_query` is best-effort only** — the Power BI REST path has no
server-side cancellation, so closing the connection is the only lever and the
query may keep running regardless (XMLA, the documented backup, would
support real cancellation — component reference §8). Real for BigQuery, best-effort for
DAX; document it, don't skip it.

**`TurnCancelledError` lives in `app/exceptions.py`** beside `ToolError` and
`ToolTimeoutError`. A cancelled turn still writes telemetry — `cancelled:
True` plus whatever `tool_calls` completed (`.claude/rules/telemetry.md`).

**Confirm at build time:** whether breaking out of `astream` propagates
cancellation into an in-flight node, or only takes effect at the next
inter-node checkpoint.

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

def route_after_route_entry(state: AgentState) -> str:
    return "execute_approved" if state["pending_queries"] else "agent"

def route_after_agent(state: AgentState) -> str:
    return "call_tool" if state["messages"][-1].tool_calls else "check_length"

def route_after_call_tool(state: AgentState) -> str:
    if state["cancelled"] or state["needs_approval"]:
        return "finalize"
    if state["answer_submitted"]:
        return "check_length"
    return "agent"   # also cost_cap_exceeded and iteration_cap_hit — see
                     # "Hitting max_iterations"; symmetric, no special routing

def route_after_check_length(state: AgentState) -> str:
    if check_answer_displayable(state["answer_markdown"]):
        return "verify"
    if state["length_retry_count"] < MAX_LENGTH_RETRIES:
        return "agent"
    return "finalize"

def route_after_verify(state: AgentState) -> str:
    if state["verified"]:
        return "finalize"
    if state["verification_retry_count"] < MAX_VERIFY_RETRIES:
        return "agent"
    return "finalize"


g = StateGraph(AgentState)
g.add_node("route_entry",      route_entry_node)
g.add_node("execute_approved", execute_approved_node)
g.add_node("agent",            agent_node)
g.add_node("call_tool",        call_tool_node)
g.add_node("check_length",     check_length_node)
g.add_node("verify",           verify_node)
g.add_node("finalize",         finalize_node)

g.add_edge(START, "route_entry")
g.add_conditional_edges("route_entry", route_after_route_entry,
    {"execute_approved": "execute_approved", "agent": "agent"})
g.add_edge("execute_approved", "agent")   # even on failure — the agent sees
                                          # the error and can respond to it

g.add_conditional_edges("agent", route_after_agent,
    {"call_tool": "call_tool", "check_length": "check_length"})
g.add_conditional_edges("call_tool", route_after_call_tool,
    {"finalize": "finalize", "check_length": "check_length", "agent": "agent"})
g.add_conditional_edges("check_length", route_after_check_length,
    {"verify": "verify", "agent": "agent", "finalize": "finalize"})
g.add_conditional_edges("verify", route_after_verify,
    {"finalize": "finalize", "agent": "agent"})

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
ALL_TOOLS = {**MCP_TOOLS, "run_bigquery_sql": run_bigquery_sql,
             "submit_answer": submit_answer}  # neither is MCP-based
                                              # (`.claude/rules/tools.md`)
                                              # -- MCP_TOOLS alone would
                                              # leave both uncallable

async def agent_node(state: AgentState) -> dict:
    # llm is constructed with thinking={"type": "adaptive", "display": "summarized"}
    # -- readable thinking text for append_thinking() (.claude/rules/gateway.md).
    # No cost difference: thinking tokens bill the same regardless of display.
    model = llm.bind_tools(list(ALL_TOOLS.values()), strict=True)   # constrained
                                                                     # decoding, above
    response = await model.ainvoke([SYSTEM_MESSAGE, *state["history_messages"], *state["messages"]])
    return {"messages": [response], "llm_calls": state["llm_calls"] + 1}
```

| Node | Role |
|---|---|
| `route_entry` | Branches on `pending_queries`. Pure routing, no work |
| `execute_approved` | Runs each `pending_queries` entry directly in Python — no LLM |
| `agent` | The LLM call, tools bound. Emits tool calls only — `answer_markdown` only ever arrives via `submit_answer` |
| `call_tool` | Dispatches tools (including `submit_answer`), checks cancel, increments `iteration_count`, sets `answer_submitted` |
| `check_length` | Owns `length_retry_count` |
| `verify` | `verify_response()`. Owns `verification_retry_count` |
| `finalize` | Sets `AgentResponse` fields and writes telemetry — six routes in, see below |

- `route_entry` branches on `pending_queries` — a fresh turn's is empty.
- **Cost gating is not a node** — it lives inside `run_bigquery_sql`, which
  dry-runs the exact SQL immediately before executing it. `call_tool` routes
  on what the tool reports back.
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
| `call_tool` | `cancelled` | Cancelled | `answer_markdown` if `agent` had already set one, else a fixed "this turn was cancelled" message | Normal row, `cancelled: True` |
| `call_tool` | `needs_approval` | Approval pause | `needs_approval: True`, `pending_query` (the largest), `estimated_cost` | **Pause row**, `approval_decision: null` (`.claude/rules/telemetry.md`) |
| `check_length` | too long, `length_retry_count` exhausted | Length decline | Fixed decline: couldn't produce a short enough answer, suggests breaking up the question | Normal row |
| `verify` | `verified: True` | Success | Full assembly: `answer_markdown`, `sources` (`build_sources`), `suggested_follow_ups`; `iteration_cap_hit`/`cost_cap_exceeded` set `True` if either is how this turn got here | Normal row |
| `verify` | failed, `verification_retry_count` exhausted | Honest decline | "No verified figure for that" — no unverified number emitted | Normal row |

**`finalize` never touches `live_turns` — every write to it lives in the
gateway's `run_agent_turn` instead.** `live_turns` is gateway-owned
(`.claude/rules/gateway.md`); splitting its writes across two modules risks
drift. `finalize` only sets the `AgentState` fields above and writes
`agent_telemetry` (BigQuery — unchanged). The gateway acts on `final_state`
once the loop ends:

```python
def build_pending_approval(state: AgentState) -> PendingApproval:
    """Pure, no I/O — called from the gateway, not from finalize."""
    return PendingApproval(
        conversation_id=state["conversation_id"],
        filter_context=state["filter_context"],
        active_page=state["active_page"],
        messages=messages_to_dict(state["messages"]),
        pending_queries=state["pending_queries"],
        deferred_dax=state["deferred_dax"],
        tool_calls=state["tool_calls"],
        iteration_count=state["iteration_count"],
        bytes_consumed=state["bytes_consumed"],
        estimated_cost=state["estimated_cost"],
        paused_at=now(),
    )
```

```python
# app/gateway/ — run_agent_turn, after the astream loop ends
if final_state["needs_approval"]:
    pending = build_pending_approval(final_state)
    await live_turns_doc(conversation_id).set({"pending_approval": pending}, merge=True)
else:
    await live_turns_doc(conversation_id).delete()
return build_agent_response(final_state)
```

**Why not in `finalize`:** `astream` yields a node's update only after it
finishes. A `finalize`-side clear would run *before* the gateway's own
`elif node == "finalize": set_status(...)` below — which would then
re-create the document via its `merge=True` upsert, orphaned with no TTL
(`live_turns` has none, unlike `sessions`).

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
  what's reconstructed from Firestore, deliberately bounded
  (`HISTORY_ROW_CAP`, `HISTORY_TURN_COUNT` — `.claude/rules/gateway.md`).
- **Confirm parallel dispatch isn't accidentally serialized** — LangGraph runs
  multiple tool calls concurrently by default. A free win, not something to
  build.

### Why flat tool-calling, not sub-agents

More LLM round-trips work against the 5–15s target, and citation granularity
gets murky once a result comes from a sub-agent. If DAX quality becomes a
problem, escalate in order: more DAX few-shots → richer tool descriptions →
forced tool choice → *then* sub-agents. Decide from eval metrics, not feel.

## LangSmith tracing — local and deployed

`LANGSMITH_TRACING`/`LANGSMITH_API_KEY` give a per-turn trace of the agent
loop: every LLM call, tool call, and iteration, nested in order. **Not deploy
flags** — `LANGSMITH_TRACING` and `LANGCHAIN_CALLBACKS_BACKGROUND` are
literals in `app/config.py` (identical in every environment), and
`LANGSMITH_API_KEY` is fetched via `get_secret()` at startup like every other
secret. All three get pushed to `os.environ` once before any
LangChain/LangGraph import, since the SDK only reads them from there
(`CLAUDE.md` Commands section).

**`LANGCHAIN_CALLBACKS_BACKGROUND=false` is required wherever it's deployed.**
Trace uploads go through a background callback by default; Cloud Run freezes
CPU the instant the response is sent, so the upload is lost — the same trap
already solved for telemetry writes.

**Separate from `agent_telemetry`, not a replacement**
(`.claude/rules/telemetry.md`). Traces are a per-turn debugging view; the
BigQuery table is the queryable record that feeds the judge. Different jobs.

**Traces are queryable programmatically, not just via the LangSmith UI** —
`langsmith.Client().list_runs(...)` (a separate package from `langchain-core`)
filters by project/time/tags/metadata against the same data already being
sent. No setup beyond the tracing config above.

Setup: `local-dev-environment-setup.md` Step 17. Why tracing runs in
production here rather than Cloud Trace: component reference §3.
