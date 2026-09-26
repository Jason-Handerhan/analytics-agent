# Testing Strategy

**When to write them: in the same step as the code, not a later pass** — see
`CLAUDE.md`, "How we work."

**Scope, deliberately narrow.** 28 tests across 11 files — one per
main component, one per guardrail *category*, plus real end-to-end coverage.
It verifies the system *works*, not every historical edge case. The full, much finer-grained suite (~83 tests, one per specific
silent-failure mode caught during design) was archived outside this doc set
for personal reference. It is **not** part of the build. Backfill a test from
it individually only if a bug at that granularity actually recurs.

**Split tests by what they need to run.**

| Layer | Needs | Runs |
|---|---|---|
| **1 — Pure logic / mocked I/O** | No credentials | Every commit |
| **2 — Live system** | Real credentials | Manually / on events |

**CI runs Layer 1 only, zero credentials configured.** Anything reaching
BigQuery, Power BI, Secret Manager, GitHub, Firestore, or an LLM API is
Layer 2 — mock the client for Layer 1.

**The golden dataset is not here, and not automated — a deliberate,
different kind of testing, equally critical.** Hand-curated correctness
checks (is the *number* actually right — the judge can't catch a confidently
cited wrong query), run manually on prompt/schema changes. See
`docs/golden-dataset.md`. **Nothing about it is tested here** — the curated
questions are judgment, and the runner's tolerance comparison is verified by
reading its own report, which you do every run anyway.

## Fixtures (`tests/conftest.py`)

| Fixture | Returns |
|---|---|
| `mock_graph_yielding(nodes)` | Fake graph whose `astream` yields given nodes |
| `build_telemetry_row(**kwargs)` | One `agent_telemetry` row dict |
| `seed_telemetry_rows(rows)` | Writes rows the judge test reads back |
| `save_message` / `get_recent_messages` | Thin Firestore wrappers, mocked client |
| `build_mock_query_result(total_rows)` | Mock BigQuery result with `.total_rows` |
| `build_mock_dax_response(row_count)` | Dict shaped like `executeQueries`' JSON — **confirm the real nesting first** (open items) |
| `sample_chart_df()` | Small DataFrame for chart dispatch tests |
| `MULTI_STEP_EXPENSIVE_QUESTION` | Needs further tool calls *after* the approved query |
| `telemetry_rows_for(conv_id)` | The (pause, response) row pair for one conversation |
| `tests/fixtures/model_schema/` | Small, hand-trimmed `INFO.VIEW.*`/Scanner API response shapes — plus the hand-verified expected artifact |
| `create_conversation(as_user)` | Calls `POST /conversation` as a given identity |
| `get_status(conv_id, as_user)` | Calls `GET /ask/status` as a given identity |
| `respond_to_approval(conv_id, decision)` | Calls `POST /ask/respond` |
| `call_tool(name, args)` | Invokes one tool through the MCP server |
| `build_prompt(conv_id, question)` | Assembles the prompt exactly as a turn would |
| `build_spec(chart_type, **kw)` | A minimal valid `ChartSpec` of the given type |
| `generate_chart_with_df(spec, df)` | Renders a spec against a supplied DataFrame |
| `run_judge(rows)` | Runs the judge over seeded telemetry rows |

## 1. Authentication

```python
# tests/test_gateway_auth.py
@pytest.mark.parametrize("token,api_key,expected", [
    (VALID, VALID,   200),
    (BAD,   VALID,   401),
    (VALID, BAD,     401),
])
async def test_both_auth_factors_are_enforced(token, api_key, expected):
    """Both factors, one test. Not a case per failure reason
    (signature/expiry/audience) — that detail lives in the archive."""

async def test_cannot_access_another_users_conversation():
    """conversation_id is client-supplied on every request after creation, so
    the ID alone proves nothing. 404 not 403 — a 403 confirms the
    conversation exists (.claude/rules/gateway.md)."""
    conv = await create_conversation(as_user=USER_A)
    resp = await get_status(conv, as_user=USER_B)
    assert resp.status_code == 404
```

## 2. Persistent prompt context & caching dispatch

```python
# tests/test_static_context.py
def test_static_context_contains_all_seven_components():
    """Orientation bundle, TABLE_REGISTRY, MEASURE_REGISTRY,
    RELATIONSHIPS/PARAMETERS, bigquery_schema, system instructions, few-shots —
    all seven, every turn, in a FIXED order: a cache hit needs a
    byte-identical prefix.

    Two silent-failure modes this covers. Table registry and relationships
    describe DIFFERENT tables (disconnected vs. connected) — dropping either
    blinds run_dax_query to half the model. And MEASURE_REGISTRY must carry
    names and descriptions but NOT dax bodies; leaking those defeats the
    reason the split exists (.claude/rules/gateway.md)."""

@pytest.mark.parametrize("model,expects_marker", [
    ("claude-sonnet-5-...", True),    # explicit cache_control required
    ("gpt-4.1-...",         False),   # automatic
    ("gemini-2.5-pro-...",  False),   # implicit
])
def test_caching_dispatch_matches_the_model(model, expects_marker):
    """Switching models is a config change — this is what makes that safe.
    A wrong branch fails SILENTLY: correct answer, full re-billing of the
    whole static block every turn (.claude/rules/gateway.md). Also covers
    the raise on an unknown model, which is what stops a typo'd name from
    quietly falling through to no caching at all."""
    out = build_static_context(model, "x" * 5000)
    assert isinstance(out, list) == expects_marker
    with pytest.raises(ValueError):
        build_static_context("mystery-model-1", "x" * 5000)

```

## 3. Model schema build — fixture-driven assembly, real API calls stay Layer 2

**Decided 2026-09-17:** the model schema is now built from live Power BI
(`executeQueries` + the Scanner API), not TMDL parsing — `docs/data-pipeline.md`.
`scripts/build_model_context.py` itself isn't rewritten to this design yet;
this section describes the test shape it needs, carrying forward the same
split the old TMDL parser had: **fetching is Layer 2 (real credentials,
real network calls), assembling the artifact from an already-fetched
response is Layer 1 (fixture-driven, no credentials).**

Once the script exists, its assembly function should take already-fetched
API response shapes as plain arguments — the `INFO.VIEW.*` rows and the
Scanner API's `scanResult` JSON — the same way `build_artifact` used to take
file paths. Point the test at a small, hand-trimmed fixture of each API's
real response shape, assert the assembled artifact matches expected values
(not just expected shape), regenerate deliberately and **read the diff**
before committing.

**What the fixture needs to cover, carried forward from the old design's real
bugs, plus what's new:** DAX bodies free of fences; HTML-display measures
excluded (`Financial_Assumptions_HTML` is a real one, confirmed present); the
strict-vs-broad `SELECTEDVALUE` distinction for parameter detection — a
fixture with an ordinary measure that merely *uses* `SELECTEDVALUE` on a
plain data column must NOT be classified as a parameter; `LocalDateTable_*`
skipped from tables and relationships; and, until `docs/data-pipeline.md`'s
two open gaps are resolved, a parameter missing `range` should assert `null`
there rather than silently passing with an absent key.

**Layer 2, run manually, not fixture-covered:** the actual `executeQueries`
and Scanner API calls succeeding against the real dataset — that needs the
real dashboard to investigate, not something a fixture can stand in for.

## 4. Chat history reaches the prompt via Firestore

```python
# tests/test_conversation_state.py
async def test_recent_messages_reach_the_prompt():
    """The exact bug this project actually hit: storing history in
    Firestore and never reading it back. Save a message, start a new turn
    in the same conversation, confirm the prior answer's content is in
    what actually gets sent to the model — not just that Firestore has it."""
    await save_message(CONV, question="what was recall?", answer="0.367")
    prompt = await build_prompt(CONV, question="what about last quarter?")
    assert "0.367" in prompt

async def test_queries_and_filters_reach_the_prompt():
    """A stored query is only safe to adapt if its filter context comes with
    it — one composed under Region=West is wrong under different slicers."""
    await save_message(CONV, question="q", answer="a",
                       queries=["EVALUATE TOPN(...)"],
                       filter_context=[{"target": {"table": "Region"},
                                        "values": ["West"]}])
    prompt = await build_prompt(CONV, question="what about East?")
    assert "EVALUATE TOPN" in prompt
    assert "West" in prompt

def test_truncation_never_splits_a_number():
    """A character cut turns 0.367 into 0.36 — a WRONG number in history.
    A naive [.!?]\s regex survives that but dies on abbreviations."""
    text = "Approx. 0.367 recall. " + "x" * 600
    assert "0.36 " not in truncate_at_sentence(text)
    assert "0.367" in truncate_at_sentence(text)
```

## 5. Every tool works, called through the MCP server

Two different things — a tool can be a correct function and still never get
properly registered.

```python
# tests/test_tools.py
@pytest.mark.parametrize("tool_name,args", [
    ("run_bigquery_sql", {...}), ("run_dax_query", {...}),
    ("get_measure_dax", {...}), ("search_docs", {...}),
    ("get_page_info", {}), ("list_repo_files", {}),
    ("read_repo_file", {...}), ("generate_chart", {...}),
])
async def test_tool_returns_a_sensible_result(tool_name, args):
    """Failures report per-case: test_tool_returns_a_sensible_result[run_dax_query]
    fails on its own — no need to split into 8 separate test functions."""
    result = await call_tool(tool_name, **args)
    assert result is not None

```

## 6. Chart tool — polymorphism and constrained decoding

```python
# tests/test_chart_tool.py
def test_chart_tool_schema_preserves_the_discriminator():
    """The regression this project actually hit: a bare discriminated union
    as the tool parameter silently loses its discriminator."""
    schema = GenerateChartArgs.model_json_schema()
    spec = schema["properties"]["spec"]
    if "$ref" in spec:
        spec = schema["$defs"][spec["$ref"].split("/")[-1]]
    assert spec["discriminator"]["propertyName"] == "chart_type"

def test_chart_dispatch_renders(chart_type, sample_chart_df):
    """One simple type plus both composites — concentration's crossing must
    land on the right x-value, and pareto's on the right categorical position
    (the reset_index trap)
    — proves polymorphic dispatch works without re-testing all 10 types."""
    spec = build_spec(chart_type)
    assert generate_chart_with_df(spec, sample_chart_df).chart_url
```

## 7. Telemetry — every turn logs a complete row

```python
# tests/test_telemetry.py
def test_every_turn_logs_a_row_with_every_required_field():
    row = build_telemetry_row(question="q", tool_calls=[], answer_markdown="a")
    for field in REQUIRED_TELEMETRY_FIELDS:
        assert field in row

```

## 8. Guardrails — one per category, not per edge case

```python
# tests/test_guardrails.py
def test_verification_rejects_an_unfounded_claim():
    """A value in all_prose_numeric_claims with no match in this turn's tool_calls
    pool fails verify_response, regardless of how plausible it looks."""

def test_verification_rejects_a_never_submitted_answer():
    """answer_markdown still at its untouched "" default (submit_answer was
    never called) fails model_validate before any claim is even checked."""

def test_verification_checks_table_values_without_redeclaring_them():
    """A number only inside a markdown table (not in all_prose_numeric_claims)
    still gets checked -- extract_table_values, not the model, is the source."""

def test_cost_threshold_triggers_approval():

def test_batch_sums_before_deciding():
    """Three queries each under BIG_QUERY_THRESHOLD, over it combined.
    Per-tool gating would pass all three (docs/approval-workflow.md)."""

def test_hard_decline_dispatches_nothing():
    """Over the absolute cap, no tool runs — not even the cheap ones."""

def test_per_turn_byte_budget_sums_across_calls():
    """Distinct from BIG_QUERY_THRESHOLD: three individually-cheap
    queries can still blow the turn budget. Only the SUM catches that."""

def test_slicer_state_reaches_filter_context():
    """getFilters() does NOT return slicer selections — they need
    getSlicers()/getSlicerState(). Without them a sensitivity-slider question
    answers against the WRONG scenario, silently
    (.claude/rules/gateway.md). End-to-end from a captured payload."""

def test_row_cap_rejects_an_oversized_result():

def test_answer_length_triggers_retry():

def test_max_iterations_produces_partial_not_refusal():
    """Partial answer, not a blanket refusal — and verification still runs
    on what's already gathered."""

def test_cancel_flag_stops_the_loop(mock_graph_yielding):
    """The interrupt itself, not its telemetry row. cancel_requested is a
    POLLED flag, not asyncio cancellation (.claude/rules/gateway.md) — so the
    astream loop has to actually check it between nodes. Flip it mid-turn and
    confirm the loop routes to finalize instead of dispatching another tool.
    Without this, test_cancelled_turn_is_logged passes on a feature that
    never fires."""

def test_cancel_kills_the_bigquery_job(mock_bq_client):
    """Level 2 — inside the tool, where a node-level check can't reach
    (.claude/rules/orchestrator.md). asyncio cancellation alone does NOT stop
    a running BigQuery job, so the explicit call is the whole mechanism.
    Mocked client: assert cancel_job(job_id) was actually called and
    TurnCancelledError — not ToolTimeoutError — was raised.

    Layer 1 because the RACE is our code; only the job really dying is
    BigQuery's. That half is the Phase 3 manual gate (check job status in
    the console), which this doesn't replace."""

```

## 9. Approval workflow, end to end

```python
# tests/test_approval_e2e.py
async def test_approve_resumes_and_returns_a_real_answer():
    """Not just that the pause fires (covered under guardrails) — that
    responding to it actually produces a complete, correct answer."""
    paused = await run_agent_turn(EXPENSIVE_QUESTION, conversation_id=CONV)
    assert paused.needs_approval
    resumed = await respond_to_approval(CONV, decision="approve")
    assert resumed.answer_markdown

async def test_resumed_turn_can_still_call_tools():
    """The approved query isn't necessarily the last thing needed. A resumed
    turn that synthesizes immediately would answer from incomplete data."""
    await run_agent_turn(MULTI_STEP_EXPENSIVE_QUESTION, conversation_id=CONV)
    resumed = await respond_to_approval(CONV, decision="approve")
    tool_names = {tc["name"] for tc in resumed.tool_calls}
    assert tool_names - {"run_bigquery_sql"}   # called something beyond the approved query

def test_pause_and_response_rows_do_not_double_count():
    """Pause row records pre-pause work; response row records post-approval
    only. Summing gives the turn total (.claude/rules/telemetry.md)."""
    pause, response = telemetry_rows_for(CONV)
    assert pause["approval_decision"] is None
    assert pause["prompt_tokens"] > 0
    assert response["approval_decision"] == "approved"
    assert response["prompt_tokens"] < pause["prompt_tokens"] + response["prompt_tokens"]
```

## 10. End-to-end: request to response

```python
# tests/test_e2e.py
async def test_a_real_question_produces_a_valid_verified_answer():
    """The one test that catches broken wiring even when every individual
    piece tests fine in isolation. Real astream path, not a shortcut."""
    response = await run_agent_turn(
        "What's the test-set recall?", conversation_id="smoke-test")
    assert isinstance(response, AgentResponse)
    assert response.answer_markdown
```

## 11. End-to-end: LLM-as-judge

```python
# tests/test_judge_e2e.py
async def test_judge_distinguishes_faithful_from_fabricated():
    """Not just 'does the judge run' — does it actually do its job. One row
    where the stated number genuinely matches its tool result, one where
    it's fabricated. Scores must diverge in the right direction — never
    assert exact values, that's testing the LLM's opinion, not the pipeline."""
    faithful_row = build_telemetry_row(
        answer_markdown=FAITHFUL_ANSWER, tool_calls=REAL_TOOL_CALLS)
    broken_row = build_telemetry_row(
        answer_markdown=FABRICATED_ANSWER, tool_calls=REAL_TOOL_CALLS)
    await seed_telemetry_rows([faithful_row, broken_row])
    scores = await run_judge()
    assert scores[faithful_row["conversation_id"]] > scores[broken_row["conversation_id"]]
```
