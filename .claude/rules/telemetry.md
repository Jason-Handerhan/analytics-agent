---
paths:
  - 'app/telemetry/**'
  - 'scripts/run_judge.py'
  - 'scripts/run_golden_tests.py'
  - 'tests/test_telemetry.py'
  - 'tests/test_judge_e2e.py'
---

# Telemetry: `agent_telemetry` schema and writer

> **Deep dives:** `docs/llm-judge.md` for the pipeline that consumes this
> table; `docs/golden-dataset.md` for regression testing (a separate
> mechanism — see component reference §9a for why they're distinct).

**One `INSERT` per turn — not per-step tracing.** No separate spans, no tracing
system. Per-tool granularity comes from a **nested, repeated `RECORD`** field
within that single row:

```python
tool_calls = [
    {"name": "run_bigquery_sql", "started_at": t1, "completed_at": t2,
     "args": json.dumps({"query": "SELECT ..."}), "query_text": "SELECT ...",
     "result": json.dumps(rows), "success": True, "error": None},
    {"name": "search_docs", "started_at": t3, "completed_at": t4,
     "args": json.dumps({"query": "model evaluation methodology", "top_k": 5}),
     "query_text": "model evaluation methodology",  # the search string, not SQL/DAX
     "result": json.dumps(chunks), "success": True, "error": None},
    {"name": "list_repo_files", "started_at": t5, "completed_at": t6,
     "args": json.dumps({}), "query_text": None,    # no meaningful query
     "result": json.dumps(paths), "success": True, "error": None},
]
```

**Field names match `ToolCallRecord` exactly** (`.claude/rules/orchestrator.md`) —
`name`, `args`, `result`, `success`, `error`, `started_at`, `completed_at`.
`sources` isn't logged — it's `build_sources(tool_calls)`, fully
reconstructible from the record below.

`args` and `result` are `json.dumps()`'d because the record is a BigQuery
`RECORD`; nothing is renamed. Only `id` is dropped — it's an in-turn handle,
meaningless once the turn ends.

**`query_text` is the one added field**, derived from `args`. It exists so the
query tools' SQL/DAX is directly queryable rather than needing
`JSON_VALUE(args, '$.query')` with a per-tool path. `args` is still logged in
full, so nothing is lost where `query_text` doesn't apply.

**`query_text` — log the query, not just its result. This closes a real gap:
`result` alone can't distinguish "right query, faithfully reported" from
"wrong query, faithfully reported."** A subtly wrong `JOIN` or filter still
executes, still returns a real result, still passes value-verification —
because the check only confirms the claimed number appears in what came back,
never whether that was the right thing to ask. Without the query text logged,
there's no way to inspect *why* a suspicious answer happened after the fact.

- **`run_bigquery_sql` / `run_dax_query`:** the exact generated SQL/DAX.
  Highest priority — these are the tools whose queries determine whether an
  answer is *correct*, not just self-consistent.
- **`search_docs`:** the natural-language string passed to
  `VECTOR_SEARCH`. Lower priority than the two above — a bad vector search
  degrades to "less relevant chunks retrieved," not "confidently wrong number
  reported," but logging it costs nothing once the field exists.
- **Everything else:** leave `query_text` null. A file path, a page name, or
  a chart spec isn't a query, and `args` already carries it verbatim — no
  placeholder, no misnomer.
- **A real bonus, not just debugging value:** logged, verified-correct
  queries are a natural source for *curating* future DAX few-shot examples
  from production, rather than writing synthetic ones by hand — directly
  useful if the DAX-quality escalation ladder (`.claude/rules/orchestrator.md`) ever gets pulled off
  the shelf.

**Log raw results, not summaries.** The judge's faithfulness check and the
value-verification check both need the actual returned data to compare a claim
against.

**Await the write — never background it.** Cloud Run's default request-based
billing throttles CPU to near-zero the instant a response is sent, so an
in-flight background write freezes silently and is lost if the instance scales
to zero. No error, no log entry. A streaming insert is <100ms against 5–15s
turns; awaiting it is a rounding error.

## Schema — define fully now, populate over time

Create the table with every field up front rather than migrating piecemeal.
Only the writer code grows as tools get built.

**One definition, two consumers.** `app/telemetry/schema.py` holds the
`SchemaField` list; `app/telemetry/create_table.py` imports it to create/
recreate the table, and `writer.py` imports the same list as `insert_rows`'s
`selected_fields` — so the table's real shape and what the writer sends can't
drift apart. Re-run `create_table.py` after any schema change (BigQuery can't
`ALTER` a column's mode or rename it, so a real change means drop + recreate
— `notebooks/delete_agent_telemetry_table.ipynb` is the companion for that).

| Field | Real when the table is first built (Phase 1)? | Populated from |
|---|---|---|
| `conversation_id`, `user_id`, `question` | Yes | Phase 1 |
| `filter_context`, `active_page` | No — null | Phase 3 (captured through the connector) |
| `tool_calls` (nested record: `ToolCallRecord` fields + derived `query_text`) | No — empty | Phase 3, one tool at a time as each lands |
| `answer_markdown` | Yes — the canned echo answer | Phase 1 |
| `turn_started_at`, `turn_completed_at` | Yes | Phase 1 |
| `verified` | No — `False` | Phase 3 — **not** on `AgentResponse` by design, but logged here: it's the only way to confirm in production that every non-declined answer actually passed verification |
| `verification_retry_count`, `length_retry_count` | No — 0 | Phase 3 — **two separate columns, same names as `AgentState`.** A single summed `retry_count` would hide which guardrail fired, which is the whole reason to log them |
| `needs_approval` | No — `False` | Phase 3 (approval routing, not the dry-run/cap that ships with the tool) |
| `pending_query`, `pending_queries`, `deferred_dax`, `estimated_cost` | No — null | Phase 3 — **top-level, not nested in `tool_calls`**: a query that hasn't run yet has no `started_at`/`result` to live alongside. `pending_query` (singular) is the largest pending query, same value as `AgentResponse.pending_query`, kept for cheap querying; `pending_queries`/`deferred_dax` (both plural) are the full lists, BigQuery and DAX respectively. `estimated_cost` scopes to *this batch only* — combine with `bytes_consumed` for the turn's running total at the moment of pause |
| `approval_decision` | No — null | Phase 3 — `"approved"`/`"rejected"` on the response row; null on the pause row |
| `cost_cap_exceeded` | No — `False` | Phase 3 — hard decline, logged synchronously like any normal turn |
| `chart_url` | No — null | Phase 3 (`generate_chart`) |
| `suggested_follow_ups` | No — empty | Phase 4 |
| `is_projection` | **Not a field yet** | Added *with* the Phase 7 `run_projection` tool, not before |
| `prompt_tokens`, `completion_tokens`, `llm_calls` | No — 0 | Accumulated in `AgentState` across every LLM call. **Instrument, don't gate**: `max_iterations` and the retry caps already bound LLM spend, and there's no unbounded-tail case like a table scan. Also the signal for prompt bloat — rising prompt tokens means static context grew or cache hits dropped |
| `bytes_consumed` | No — 0 | Phase 3 — the turn's BigQuery total, same counter the cost guardrail reads. The pause/response split means **summing both rows** gives the turn's real spend |
| `iteration_count` | No — 0 | Phase 3 — **the count, not just `iteration_cap_hit`.** The cap is a calibration starting point (`.claude/rules/orchestrator.md`); a boolean only says how often 12 was hit, never what the 95th percentile actually needs |
| `errors` | No — `[]` | Turn-level failures (stage, type, message, timestamp, `tool_call_id`). A tool-call-scoped failure carries the matching `tool_call_id` — a direct join to `tool_calls`, not a duplicate of it — one entry per failed call, not deduped per batch. Also covers LLM call failures, verification exhaustion, chart failures (`tool_call_id: null`). Carried in `AgentState.errors`; write it or the diagnosis is lost |
| `claims` | **Not a field** — dropped | Superseded by pooled numeric matching (`.claude/rules/orchestrator.md`); nothing produces a `Claim` list anymore. `AgentResponse.claims` stays only as an always-`[]` wire-contract placeholder and isn't mirrored here |
| `cancelled` | No — `False` | Phase 3 — set when `POST /ask/cancel` fires. **Different from abandoned approvals**, which are deliberately unlogged: a cancel is a real synchronous event with a clean trigger, not silence over time |
| `iteration_cap_hit` | No — `False` | Phase 3 — same name and value as the `AgentResponse` field; one boolean, logged once |

`user_id` comes from the Entra token's `oid` claim. Compute turn duration from
the two timestamps rather than storing a separate duration field that can drift.

**Questions rejected for length never reach this table** — they're a 400 at
the gateway before a turn starts, so there's no `tool_calls` or
`answer_markdown` to record. Cloud Run's own request logs capture the 400
already; no bespoke logging needed.

**Some fields also live in Firestore — deliberately, not redundantly.**
`user_id` and the pending-approval fields exist in both
(`.claude/rules/gateway.md`) because they serve different jobs: Firestore
is **live state** an in-flight turn reads back mid-pause; this table is the
**durable audit record**, written once and never read by serving code.
Firestore entries are cleared at turn end; these rows persist.

## Approval-gated turns write TWO rows, not one updated row

The pause/resume flow itself is in `docs/approval-workflow.md` — this is how
it lands in telemetry.

BigQuery is append-optimized, and every other part of this design treats
`agent_telemetry` as an append-only event log. An `UPDATE` on the original
row would reintroduce exactly the read-then-write pattern avoided
everywhere else. So:

1. **Pause row** — written when `/ask` returns `needs_approval: True`. A
   **complete record of everything up to the pause**: `tool_calls`,
   `prompt_tokens`, `completion_tokens`, `llm_calls`, `bytes_consumed`,
   `errors`, timing, plus `pending_query`, `pending_queries`,
   `deferred_dax`, and `estimated_cost`. `approval_decision` is null.
2. **Response row** — written when `/ask/respond` fires. Same
   `conversation_id`, `approval_decision` set, covering **post-approval work
   only**.

**Sum the two rows for the turn's true total.** Each row is self-contained
for its own half, so nothing is double-counted — this is why
`PendingApproval` carries `bytes_consumed` (a guardrail input the resumed
loop needs) but not token counts (already recorded here).

**Abandoned approvals are findable with no extra machinery.** A pause row
with no matching response row for the same `conversation_id` *is* the record
— one query, no scheduled sweep, no detection logic. What isn't captured is
*when* the user walked away, since there's no completion event; the pause
row's timestamp and the conversation's TTL bound it, which is enough.

## The judge job consumes this — don't couple them

`scripts/run_judge.py` runs as a **Cloud Run Job** (a different product from a
Cloud Run *service* — runs to completion, exits, billed for full execution
time), triggered by Cloud Scheduler. It only reads `agent_telemetry`; it never
shares a process with the live gateway.

Separate because they scale on completely different axes — the gateway with
user traffic, the judge with log volume — and a bug in judge logic must never
be able to degrade live serving.

The judge model can be a different, stronger model than the one serving live
traffic; latency and cost don't matter for offline batch scoring.
