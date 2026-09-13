# Approval Workflow

The cost-approval pause, its three tiers, and how a paused turn resumes. Read
before building `POST /ask/respond` or the cost-tier logic.
`.claude/rules/orchestrator.md` carries the summary that points here.

## Three cost tiers

**Decisions are made in BYTES; dollars are display only.** Compare dry-run
`total_bytes_processed` against byte thresholds — the native unit, no
conversion, nothing to drift. `estimated_cost` is a derived dollar string for
humans, computed from bytes via BigQuery's per-TB rate. If that pricing
constant goes stale the *display* is wrong; the *decision* never is.

| Tier | Condition | Behavior |
|---|---|---|
| Auto | Under per-query byte threshold | Runs normally |
| Approval | Batch over `BIG_QUERY_THRESHOLD`, turn total under absolute cap | `needs_approval: True` + pause |
| Hard decline | **Combined total** at/over absolute cap | `cost_cap_exceeded: True` — no approval offered |

**Both constants are deliberately unvalued** — they're on `CLAUDE.md`'s
ask-first list. Pick them together once you can see real dry-run figures from
`agent_safe`: a threshold that never fires teaches nothing, and one that fires
on every query makes approval noise the user learns to click through.

**Two different scopes.** `BIG_QUERY_THRESHOLD` gates **this batch** — the
summed bytes of every BigQuery call dispatched together, so it triggers
whether one query is enormous or three are merely large. The **absolute cap**
gates **the whole turn**: `bytes_consumed` accumulates across every tool loop
and survives an approval pause, so a turn can't spend its way past the cap in
installments. BigQuery-only; `executeQueries` has no dry-run to price
against.

**Tune the threshold value for a batch, not a single query.** Summing means
the same number pauses more often than it would per-query — three moderate
queries can cross a bar none of them would alone.

**Hard decline is a distinct terminal state**, not a refusal to answer and not
an approval prompt. Populate `estimated_cost` so the message is specific:
*"This would cost an estimated $47, which exceeds the maximum for a single
question. Try narrowing your question."*

## The cost gate lives in the node, not the tool — three phases

**Per-tool gating is unsafe for a batch.** Three BigQuery calls dispatched
together each read the same `state["bytes_consumed"]`, captured before any of
them ran. Each individually under the threshold, all three pass — combined
they blow the cap. No dispatch ordering fixes this; the sum only exists at the
node, so the decision belongs there.

```python
async def call_tool_node(state: AgentState) -> dict:
    if await is_cancelled(state["conversation_id"]):
        return {"cancelled": True}

    tool_calls = state["messages"][-1]["message"].tool_calls
    check_batch_ordering(tool_calls)
    bq  = [tc for tc in tool_calls if tc["name"] == "run_bigquery_sql"]
    rest = [tc for tc in tool_calls if tc["name"] != "run_bigquery_sql"]

    # Validate at the boundary — strict=True constrains generation, but this
    # is the first RUNTIME check, and the node unpacks args before any tool
    # function (and its own validation) is ever reached.
    try:
        bq_args = [RunBigQuerySqlArgs.model_validate(tc["args"]) for tc in bq]
    except ValidationError as e:
        return {"errors": [TurnError(stage="call_tool",
                                     error_type="ValidationError",
                                     message=str(e), occurred_at=now())]}

    # PHASE 1 — price every query. Dry runs are free, scan nothing, and are
    # where BigQuery validates syntax and resolves table references.
    estimates = await asyncio.gather(
        *[dry_run(a.query) for a in bq_args], return_exceptions=True)

    # A malformed query fails HERE, for free. Fail the whole batch: pricing a
    # subset would decide cost against an incomplete picture, and the agent is
    # about to rewrite and re-dispatch anyway. Surface BigQuery's own message
    # verbatim — "Unrecognized name: reveune at [3:8]" is what makes the retry
    # land; wrapping it in something generic throws away the useful part.
    failed = [(a, e) for a, e in zip(bq_args, estimates) if isinstance(e, Exception)]
    if failed:
        return {"tool_calls": [failed_record(a, e) for a, e in failed],
                "errors": [TurnError(stage="call_tool", error_type="QueryError",
                                     message=str(e), occurred_at=now())
                           for _, e in failed]}

    # PHASE 2 — ONE decision, on the summed batch. Nothing dispatched yet.
    batch_bytes = sum(estimates)

    if state["bytes_consumed"] + batch_bytes >= ABSOLUTE_CAP:
        # Dispatch NOTHING — spending on searches for a turn about to be
        # declined outright is pure waste.
        return {"cost_cap_exceeded": True,
                "pending_queries": [a.query for a in bq_args]}

    if batch_bytes > BIG_QUERY_THRESHOLD:
        # DEFER all three query-ish tools; run everything else and bank it.
        #   run_bigquery_sql — the thing being approved
        #   run_dax_query    — deferring keeps its (potentially large) result
        #                      out of Firestore, and re-reads fresher data
        #   generate_chart   — needs a completed query; its output is a GCS
        #                      URL for a chart nobody will see
        # The turn stops here either way, so the agent isn't reasoning during
        # the pause — deferring changes WHEN these run, not what it knows.
        DEFERRED = {"run_bigquery_sql", "run_dax_query", "generate_chart"}
        other = await asyncio.gather(
            *[dispatch_tool(tc, state) for tc in rest
              if tc["name"] not in DEFERRED],
            return_exceptions=True)
        return {"tool_calls": records_from(other),   # small results only
                "needs_approval": True,
                "pending_queries": [a.query for a in bq_args],
                "deferred_dax": [tc["args"]["dax"] for tc in rest
                                 if tc["name"] == "run_dax_query"],
                "estimated_cost": format_cost(batch_bytes)}

    # PHASE 3 — normal path: one plain dispatch, no special ordering. gather()
    # IS the join point; nothing below runs until every call resolves, so a
    # completed sibling's result is never abandoned.
    results = await asyncio.gather(
        *[execute(a.query, state) for a in bq_args],
        *[dispatch_tool(tc, state) for tc in rest],
        return_exceptions=True)
    return {"tool_calls": records_from(results),
            "bytes_consumed": state["bytes_consumed"] + batch_bytes}
```

**Never inspect results as they arrive and return early** (`as_completed` with
a `return` inside the loop). Still-running calls get orphaned — they keep
executing and keep costing — and a `search_docs` result that already finished
is lost, so the resumed turn re-runs a search it already paid for.

**`result` is written as a native Firestore array-of-maps, not a JSON
string.** Firestore has a real timestamp type, so datetimes round-trip
properly — JSON would force `default=str` and lose them. Two constraints:
`Decimal` isn't a Firestore type (the query tools already coerce it to
`float`, `.claude/rules/mcp-tools.md`), and **arrays can't directly contain
arrays** — fine for flat scalar rows, but an ARRAY column would need
flattening first. Map keys holding `Table[Column]` (DAX's fully-qualified
names) write fine: the bracket restriction applies to dot-notation *field
paths*, and these documents are written whole with `set()`.

**Deferring both query tools reduces Firestore exposure — it doesn't
eliminate it.** Deferral only stops *this batch's* queries from running. A
query that completed in an **earlier iteration** is already in `tool_calls`
when a later batch pauses, and it's written to `PendingApproval` with
everything else — that's the round trip the resumed turn depends on for
citing and verifying those numbers.

So:
- **Single-iteration pause** (the common case) — no query results at all,
  just vector-search hits, a file read, a page's HTML.
- **Multi-iteration pause** — carries whatever prior batches returned,
  bounded by the 1,000-row cap per call. A few hundred KB at the extreme.

**The 1,000-row cap is what bounds the worst case**, not deferral. If a turn
ever does approach 1 MB, the fix is offloading large results to GCS and
storing a reference — unlikely enough not to build now, but not ruled out.

**Nothing is truncated anywhere.** A result is either complete or the call
failed — a 10,000-row DAX result raises at the row cap, so `success: False`
with the error recorded and no result. A failed record carried into
`PendingApproval` is useful in its own right: the resumed agent knows it
already tried that query and why.

**Firestore vs. telemetry.** Firestore holds the minimum needed to resume;
`agent_telemetry` holds the durable record — `query_text` *and* `result`,
written when the query actually runs, which for a deferred query is on the
resumed turn. BigQuery has no document-size limit, so full results live there
regardless.

**The two helpers the node leans on:**

```python
async def dry_run(query: str) -> int:
    """Price a query without scanning anything. Free, and where BigQuery
    validates syntax and resolves table references."""
    job = client.query(query, job_config=QueryJobConfig(dry_run=True))
    return job.total_bytes_processed

async def dispatch_tool(tool_call: dict, state: AgentState):
    """Route one tool call, injecting the state each tool needs.

    Injection happens HERE, in Python — never in a tool's args model. The
    model supplies only its own arguments; conversation_id, bytes_consumed,
    and tool_calls come from state (.claude/rules/mcp-tools.md).
    """
    name, args = tool_call["name"], tool_call["args"]
    if name == "run_bigquery_sql":
        return await run_bigquery_sql(args["query"], state["conversation_id"])
    if name == "generate_chart":
        return await generate_chart(GenerateChartArgs.model_validate(args),
                                    state["tool_calls"])
    return await mcp_client.call_tool(name, args)
```

**`dry_run` is a plain helper, not a tool.** It wraps
`QueryJobConfig(dry_run=True)` and returns bytes. The model never calls it —
the node does, on queries the model already produced. Exposing it would add a
tool that returns a byte count and invite the model to report an estimate as
if it were data.

## The pause is resumable — cache the whole turn, not just the query

The original `POST /ask` **has already returned** by the time the user decides.
There's no open connection to resume, so approval arrives as a *new* request.
Linking them uses **`live_turns/{conversation_id}` in Firestore** — the same
document that already holds the status string and cancel flag
(`.claude/rules/gateway.md`).

**Not process memory.** Cloud Run runs multiple concurrent instances, and the
approval click could land on a different one than the turn that paused. An
in-memory dict would silently miss it.

**Telemetry is never read back for this.** It's a write-only audit log of
*completed* turns; a paused turn isn't in it yet. Firestore is the live state.

**`PendingApproval`'s schema is in `.claude/rules/gateway.md`** — 12 fields,
one place. What matters here is the *rule* behind them:

**Carry what bounds total turn consumption; reset what's scoped to one
attempt.** `iteration_count` and `bytes_consumed` carry, so approval can't be
used to bypass the absolute cap. Token counts don't — the pause row already
recorded them, and counting them twice would inflate the turn. `filter_context`
and `active_page` carry as frozen copies: the resumed turn must answer against
the dashboard state the question was asked under, not whatever the user has
clicked since.

**`pending_queries` and `deferred_dax` are both plural lists of query text** —
not structured objects. A batch can trip the threshold with several queries in
it, and every one runs on approve.

**`tool_calls` must carry full `result`, not summaries** — on resume, the
model cites across the *combined* old-plus-new results, so old ones need to be
as complete as they were originally. Size is already bounded: the row caps
fired before the pause, so nothing here exceeds 1,000 rows.

**Multiple pending queries in one turn — rare, handled simply.** Show the
**largest query's text** on the card (two SQL blocks would hit the same
wall-of-text problem the answer-length check exists for), but show the
**summed `estimated_cost`** across all of them — showing one query's price
while executing several would recreate the exact "approving a number that
isn't the real one" failure this feature exists to prevent. On approval,
**execute every pending query**, not just the displayed one; they were all
part of answering the question.

## `POST /ask/respond/{conversation_id}` — the return path

Body: `{decision: "approve" | "reject"}`.

**On approve:**
1. Look up `PendingApproval` by `conversation_id`.
2. **Execute every `pending_queries` *and* `deferred_dax` entry directly in
   Python** — call the tool function with the stored query string. No LLM in
   this step. This is what guarantees the query that runs is the one the human
   priced and approved; asking the model to "decide" to call it again
   guarantees nothing. The DAX ones weren't priced (no dry-run exists for
   `executeQueries`) — they were deferred so their results wouldn't sit in
   Firestore, and they run here against fresher data.
3. Seed state: the cached `tool_calls` + the new results +
   `conversation_history`, with `iteration_count` and `bytes_consumed`
   carried forward.
4. **Re-enter the normal agent loop.** The approved query is not necessarily
   the last thing the question needed — the threshold can be crossed at step
   2 of a 4-step chain. Synthesizing immediately would answer from incomplete
   data; verification would then correctly decline, meaning the user paid for
   a query and still got nothing.
5. Normal verification, guardrails, and iteration cap apply.
6. Write the **response row** — post-approval work only
   (`.claude/rules/telemetry.md`).
7. Clear the `live_turns` document.

**Re-approval is expected, not an error.** The resumed loop can cross the
threshold again; each pause writes its own row.

**A resumed turn can still hard-decline.** If `bytes_consumed` crosses the
absolute cap mid-loop, `cost_cap_exceeded` fires exactly as in any other turn
— the user already paid for the approved query and still gets a decline.
Correct, and worth knowing before it happens.

**On reject** — no query execution, no reasoning call needed for tool
selection. If earlier tools already succeeded, this is a **partial answer**,
reusing the `iteration_cap_hit` machinery: synthesize from `tool_calls` alone
and state plainly what wasn't addressed — *"I wasn't able to answer the
numeric part; the query needed approval and was declined."* Log
`approval_decision: "rejected"` with `pending_query` retained.

**Abandoned approvals need no cleanup code and no detection logic.** If a user
never responds, the `live_turns` document sits there harmlessly and ages out
with the conversation. And they're already findable: a pause row with no
matching response row for the same `conversation_id` *is* the record
(`.claude/rules/telemetry.md`) — one query, no scheduled sweep. Only the
*timing* of abandonment is unrecorded, bounded by the pause row's timestamp
and the conversation TTL.

**Clear the whole `live_turns` document once resolved** — approved, rejected,
or cancelled. All three of its fields are scoped to one in-flight turn, so a
wholesale delete is safe. `sessions/{conversation_id}` is deliberately a
*separate* document precisely so this cleanup can't touch chat history.

**Safe to show the query verbatim:** it can only reference `agent_safe`
objects (IAM enforces this before it runs). Assumes the approver reads SQL —
true here. Rendering is a gateway concern — see `.claude/rules/gateway.md`.
