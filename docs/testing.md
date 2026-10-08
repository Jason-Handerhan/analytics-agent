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
| `build_mock_query_result(total_rows)` | Mock BigQuery result with `.total_rows` |
| `build_mock_dax_response(row_count)` | Dict shaped like `executeQueries`' JSON — **confirm the real nesting first** (open items) |
| `sample_chart_df()` | Small DataFrame for chart dispatch tests |
| `MULTI_STEP_EXPENSIVE_QUESTION` | Needs further tool calls *after* the approved query |
| `telemetry_rows_for(conv_id)` | The (pause, response) row pair for one conversation |
| `tests/fixtures/model_schema/` | Small, hand-trimmed `INFO.VIEW.*`/Scanner API response shapes — plus the hand-verified expected artifact |
| `create_conversation(as_user)` | Calls `POST /conversation` as a given identity |
| `get_status(conv_id, as_user)` | Calls `GET /ask/status` as a given identity |
| `respond_to_approval(conv_id, decision)` | Calls `POST /ask` with `approval_decision` set, no `question` |
| `call_tool(name, args)` | Invokes one tool through the MCP server |
| `build_spec(chart_type, **kw)` | A minimal valid `ChartSpec` of the given type |
| `generate_chart_with_df(spec, df)` | Renders a spec against a supplied DataFrame |
| `run_judge(rows)` | Runs the judge over seeded telemetry rows |

## 1. Authentication

```python
# tests/test_gateway.py
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

**File: `tests/test_model_schema_build.py`.**

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

## 4. Chat history — redaction, FIFO trim, and round-trip

Pure functions over plain dicts/`BaseMessage` — no Firestore, no mocking.
`fetch_history_messages`/`write_history_messages` (the actual Firestore I/O,
in `app/gateway/gateway.py`) are deliberately NOT unit-tested: mocking their
one dependency (`get_firestore_client()`) would just assert the mock does what the mock
was told to do, nothing real exercised. They're proven live instead, in
`notebooks/phase3_gateway_e2e.ipynb`.

One scenario covers three behaviors together, rather than one test each —
they're not independent properties; round-tripping the trimmed/redacted
output back through `build_history_messages` *is* the real end-to-end check:

```python
# tests/test_entry_exit.py
def test_build_updated_history_redacts_trims_and_round_trips():
    """One realistic turn exercises all three behaviors together: selective
    redaction (success-gated, not just name-gated — an error-status
    ToolMessage is left untouched), FIFO trim to HISTORY_TURN_COUNT, and
    round-tripping the result back through build_history_messages. The
    sample data is deliberately varied (a successful run_bigquery_sql call,
    a successful other-tool call, a failed call, and submit_answer) so a bug
    that redacted indiscriminately would actually fail this, not pass by
    accident."""
```

No separate test for answer truncation — `gateway.md`'s "Conversation
history" section explains why it was decided against entirely, not just
deferred: redacted tool results are now the size pressure that would have
motivated it, and they're already small and fixed-size.

## 4b. Gateway -- auth, endpoints, and live status

```python
# tests/test_gateway.py
def test_expired_token_rejected(...)          # one per auth failure mode
async def test_wrong_owner_rejected(...)     # 404 for someone else's conversation
def test_post_conversation_success(...)
def test_post_ask_success(...)               # the turn writes live_turns, then deletes it
def test_question_too_long_rejected(...)
async def test_consume_graph_writes_status_and_cleans_up(...)  # the whole live_turns write sequence
def test_get_ask_status_returns_live_progress(...)
```

`session_doc` and `live_turn_doc` are monkeypatched to a recording `_Doc`, so no Firestore
client is built. The `consume_graph` test asserts every write in order, including the final
delete, so the status sequence and the cleanup are both covered.

## 5. Every tool works, called through the MCP server

Two different things — a tool can be a correct function and still never get
properly registered.

```python
# tests/test_tools.py
@pytest.mark.parametrize("tool_name,args", [
    ("run_bigquery_sql", {...}), ("run_dax_query", {...}),
    ("get_measure_dax", {...}), ("search_docs", {...}),
    ("get_page_info", {}), ("get_repo_contents", {...}),
    ("generate_chart", {...}),
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

## 6b. Combine tool - stack/join, guardrails

```python
# tests/test_combine_tool.py
def test_combine_results_stack_join_and_guardrails():
    """Stack and join success paths (out-of-order keys, composite keys,
    left-join fills) and every guardrail's error, in one batch of cases."""
```

Ref resolution for `generate_chart` and `combine_results` lives in `tests/test_orchestrator.py`, next to the other orchestrator functions:

```python
# tests/test_orchestrator.py
def test_resolve_chart_and_combine_data():
    """Ref resolution into real-tool args for charts and combines, plus every
    resolve error path (unknown, failed, non-chartable, too few refs)."""
```

## 6c. Submit answer -- validators and question normalization

```python
# tests/test_submit_answer.py
def test_submit_answer_args_validators():
    """Exactly-one answer/question rule, literal-newline unescape, leaked-tag strip,
    and markdown structure checks."""

# tests/test_orchestrator.py
@pytest.mark.asyncio
async def test_call_tool_node_submit_answer_branch():
    """A question turn copies the question into answer_markdown and clears
    follow-ups; an answer turn passes its answer and follow-ups through."""
```

## 7. Telemetry — every turn logs a complete row

```python
# tests/test_telemetry.py
def test_every_turn_logs_a_row_with_every_required_field():
    row = build_telemetry_row(question="q", tool_calls=[], answer_markdown="a")
    for field in REQUIRED_TELEMETRY_FIELDS:
        assert field in row

```

`test_write_telemetry_row_success` is parametrized on `clarifying_question`:
an empty value is written as NULL, and a question passes through as text.

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

def test_batch_sums_before_deciding():
    """Three queries each under PENDING_APPROVAL_THRESHOLD, over it combined.
    Per-tool gating would pass all three (docs/approval-workflow.md)."""

def test_hard_decline_dispatches_nothing():
    """Over the absolute cap, no tool runs — not even the cheap ones."""

def test_per_turn_byte_budget_sums_across_calls():
    """Distinct from PENDING_APPROVAL_THRESHOLD: three individually-cheap
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

## 9. Approval workflow

Built, Layer 1 tested, and live-verified — not the aspirational sketch this
section once was. The pause itself (the gate deciding `needs_approval`) is a
guardrail test, covered under §8:
`test_call_tool_node_pauses_over_threshold` (`tests/test_orchestrator.py`) —
a batch over `PENDING_APPROVAL_THRESHOLD` pauses before any tool runs, and
the card's query is the one with the largest estimate. This section is
everything *after* the pause.

```python
# tests/test_gateway.py
def test_post_ask_resume_approved_seeds_state(monkeypatch, patch_jwks, auth_headers):
    """Approving a pending approval rebuilds initial_state from the stored
    PendingApproval, not from the request body -- question, filter_context,
    active_page, tool_calls, iteration_count, bytes_consumed/_baseline,
    approved_batch, approval_decision, and the rebuilt messages. A recording
    fake graph captures the initial_state call_tool_node would actually see."""

def test_post_ask_resume_rejected_returns_message(monkeypatch, patch_jwks, auth_headers):
    """Rejecting returns REJECTED_MESSAGE and no graph runs -- the telemetry
    write itself is mocked out here (that's build_telemetry_row's test to
    cover, not this one's) and live_turns is confirmed cleared."""

# tests/test_entry_exit.py
def test_rebuild_paused_messages():
    """A numeric success gets its labeled content restored from the
    ToolCallRecord (same ref_id); a numeric error, a non-numeric success,
    and submit_answer all pass through exactly as stored."""

# tests/test_telemetry.py
async def test_write_telemetry_row_success(...):
    """Includes the bytes_consumed_baseline subtraction: bytes_consumed=900_000_000,
    bytes_consumed_baseline=100_000_000 in, row["bytes_consumed"] == 800_000_000 out --
    proves a resumed turn's response row doesn't double-count what its pause
    row already reported."""
```

**Live-verified in `notebooks/phase3_graph.ipynb`, `phase3_orchestrator_e2e.ipynb`,
and `phase3_gateway_e2e.ipynb`** (the last one through real HTTP routes, no
notebook-local shortcuts) — approve, reject, a `TABLESAMPLE`-forced batch
pausing multiple times in one turn (all approved, telemetry correct for
every row), the absolute cap producing a correct partial answer, and the
dry-run-failure decline path.

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
