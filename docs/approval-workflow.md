# Approval Workflow

The cost-approval pause, its tiers, and how a paused turn resumes. Read
before touching `call_tool_node`'s cost logic or `run_agent_turn`'s
`approval_decision` branch. `.claude/rules/orchestrator.md` carries the
summary that points here.

## Three tiers

**Decisions are made in BYTES; dollars are display only.** Compare dry-run
estimates against byte thresholds — the native unit, no conversion, nothing
to drift. `estimated_cost` is a derived dollar string for humans
(`format_cost`), computed from bytes via BigQuery's per-TB rate. If that
pricing constant goes stale the *display* is wrong; the *decision* never is.

| Tier | Condition | Behavior |
|---|---|---|
| Auto | Batch under `PENDING_APPROVAL_THRESHOLD`, turn total under `ABSOLUTE_CAP` | Runs normally |
| Approval | Batch over `PENDING_APPROVAL_THRESHOLD` (or any query uses `TABLESAMPLE`), turn total under `ABSOLUTE_CAP` | `needs_approval: True` + pause |
| Hard decline | Turn total at/over `ABSOLUTE_CAP` | `cost_cap_exceeded: True` — no approval offered |

**Values, set in `app/config.py`:** `ABSOLUTE_CAP = 4 GiB` (turn-cumulative),
`PENDING_APPROVAL_THRESHOLD = ABSOLUTE_CAP // 2` (per-batch). `MAX_BYTES_BILLED`
equals `ABSOLUTE_CAP` — the per-query server-side fail-safe
(`maximum_bytes_billed`), independent of these gates.

**Two different scopes.** `PENDING_APPROVAL_THRESHOLD` gates **this batch** —
the summed dry-run bytes of every `run_bigquery_sql` call dispatched
together, so it triggers whether one query is enormous or three are merely
large. `ABSOLUTE_CAP` gates **the whole turn**: `bytes_consumed` accumulates
across every tool loop and survives an approval pause, so a turn can't spend
its way past the cap in installments. BigQuery-only — DAX (`executeQueries`)
has no dry-run to price against, and isn't cost-gated at all.

**`TABLESAMPLE` forces a pause regardless of the estimate.** Its dry-run byte
count has repeatedly underestimated actual billed bytes in practice — not a
hypothetical risk, an observed one — so any query containing it (case-insensitive
substring check on the query text) always sets `needs_approval`, even when
the (untrustworthy) estimate reads under threshold. It does **not** bypass
`cost_cap_exceeded` — that check still uses the same estimate, so a
`TABLESAMPLE` query estimated far enough over `ABSOLUTE_CAP` still hard-declines.
Scoped to `run_bigquery_sql` only; the keyword doesn't exist in DAX.

**Hard decline is a distinct terminal state**, not a refusal to answer and not
an approval prompt. `estimated_cost` is still populated so the decline message
can cite a figure.

## The cost gate lives in `call_tool_node`, not the tool — dry-run, decide, dispatch

**Per-tool gating is unsafe for a batch.** Three BigQuery calls dispatched
together each read the same `state["bytes_consumed"]`, captured before any of
them ran. Each individually under the threshold, all three pass — combined
they blow the cap. No dispatch ordering fixes this; the sum only exists at the
node, so the decision belongs there.

```python
bq_calls = [tc for tc in tool_calls if tc["name"] == "run_bigquery_sql"]
other_calls = [tc for tc in tool_calls if tc["name"] != "run_bigquery_sql"]

if bq_calls:
    estimates = await asyncio.gather(*[dry_run(tc["args"]["query"]) for tc in bq_calls])
    batch_bytes = sum(estimates)
    cost_cap_exceeded = state["bytes_consumed"] + batch_bytes >= ABSOLUTE_CAP
    uses_tablesample = any("TABLESAMPLE" in tc["args"]["query"].upper() for tc in bq_calls)
    needs_approval = ((batch_bytes > PENDING_APPROVAL_THRESHOLD or uses_tablesample)
                      and not cost_cap_exceeded and not state["approved_batch"])

if needs_approval:
    # Nothing dispatched -- the whole batch (BigQuery AND everything else
    # in this round) waits. largest_pending_query is re-derived by index,
    # not a lambda, for the card's single display query.
    largest_index = estimates.index(max(estimates))
    return {
        "needs_approval": True,
        "pending_queries": [{"id": tc["id"], "query": tc["args"]["query"], "estimated_bytes": est}
                             for tc, est in zip(bq_calls, estimates)],
        "largest_pending_query": bq_calls[largest_index]["args"]["query"],
        "estimated_cost": format_cost(batch_bytes),
        "paused_at": datetime.now(timezone.utc),
    }
```

**`not state["approved_batch"]` is what lets a resumed, already-approved batch
through without re-pausing itself.** `approved_batch` is `True` only on the
first `call_tool_node` call after a resume, and the node's own return dict
resets it to `False` immediately — so a *second* huge query discovered later
in the same resumed turn pauses normally. `TABLESAMPLE`'s forced pause is
gated by the same two conditions (`not cost_cap_exceeded`, `not approved_batch`)
as the threshold check, for the same reason.

**The pause returns before dispatching anything in the batch — BigQuery
*and* every other tool requested in the same round.** `other_calls` (DAX,
`search_docs`, `generate_chart`, whatever else the model asked for alongside
the big query) are never run, never recorded, and nothing tracks them for
later — they're simply re-requested on resume, for free, because resume
replays the *entire* dangling `AIMessage.tool_calls` list, not just the
approved query (see below). **`deferred_dax` is a field that exists on
`AgentState`/`PendingApproval` but is never populated in the current
build** — the deferral mechanism the original design sketched (re-invoking
specific deferred DAX calls by id on approve) was superseded by the simpler
replay approach once that was settled; the field is vestigial, kept rather
than removed since dropping it is a schema change for no behavioral gain.

**`pending_queries` is informational, not load-bearing, for the same
reason.** The original design had resume re-invoke each entry explicitly by
id. The real resume path doesn't read `pending_queries` for dispatch at
all — it replays the dangling `AIMessage`, and `call_tool_node` re-splits
*that* into `bq_calls`/`other_calls` fresh, exactly like any other round.
`pending_queries` only ever feeds the approval card's display and the pause
telemetry row.

**Multiple pending queries in one batch — rare, handled simply.** Show the
**largest query's text** on the card (`largest_pending_query` — two SQL
blocks would hit the same wall-of-text problem the answer-length check
exists for), but show the **summed `estimated_cost`** across the whole
batch (`format_cost(batch_bytes)`, already a sum) — showing one query's
price while running several would recreate the exact "approving a number
that isn't the real one" failure this feature exists to prevent.

## The pause is resumable — cache the whole turn, not just the query

The original `POST /ask` **has already returned** by the time the user
decides. There's no open connection to resume, so a decision arrives as a
*new* `POST /ask` call — `approval_decision` is a field on `AskRequest`
itself (`"approved"` or `"rejected"`), mutually exclusive with `question`;
there is no separate `/ask/respond` endpoint. Linking the decision back to
the paused turn uses **`live_turns/{conversation_id}` in Firestore** — the
same document that already holds the status string and cancel flag
(`.claude/rules/gateway.md`).

**Not process memory.** Cloud Run runs multiple concurrent instances, and the
decision could land on a different one than the turn that paused. An
in-memory dict would silently miss it.

**Telemetry is never read back for this.** It's a write-only audit log of
completed turns; a paused turn isn't in it yet. Firestore is the live state.

**`PendingApproval`'s schema is in `.claude/rules/gateway.md`** (`app/orchestrator/state.py`
is where it's actually defined). **Built by `build_pending_approval()`**
(`app/gateway/entry_exit.py`), **called from `consume_graph`'s `finally`
block, not `finalize`** — `finalize` never touches `live_turns`
(`.claude/rules/gateway.md`'s "Why not in `finalize`" explains why: `astream`
only yields a node's update after it finishes, so a `finalize`-side write
would race the gateway's own cleanup). `consume_graph` replaces the whole
`live_turns` document with `{"pending_approval": ...}` (no merge — nothing
else in the document matters once paused) when `final_state["needs_approval"]`,
and deletes it on every other exit.

**Carry what bounds total turn consumption or is needed to resume reasoning;
reset what's scoped to one attempt; omit what's already recorded elsewhere.**
`iteration_count` and `bytes_consumed` carry, so approval can't be used to
bypass the absolute cap. `verification_retry_count`/`length_retry_count`
reset — scoped to one synthesis attempt, and post-approval synthesis runs
against a different, larger result set. Token counts and `errors` are
omitted — the pause telemetry row already recorded them. `question` *is*
carried (unlike the earlier design) — telemetry needs it for the response
row, and there's no other way to recover it once it's folded into the
`HumanMessage`'s content.

**Messages are redacted before storage, then selectively restored on
resume.** `build_pending_approval` redacts every successful non-`submit_answer`
`ToolMessage` with a generic notice (`PAUSE_REDACTED_MESSAGE`,
`app/gateway/entry_exit.py`'s `_redact_tool_results` — the same helper
`build_updated_history` uses for cross-turn staleness, just a different
message). On resume, `rebuild_paused_messages` walks the stored messages and
restores only the ones whose matching `ToolCallRecord` is a successful
`NUMERIC_SOURCE_TOOLS` result — reconstructing the exact labeled content
(`label_chartable_result`, `app/orchestrator/shared_helpers.py`) the model
originally saw, same `ref_id` included, by reading `tool_calls` (which is
*never* redacted, carried in full) rather than re-running anything. Numeric
results survive **every** pause/resume cycle in a turn, all the way back to
its start, because `tool_calls` only ever accumulates (`append_list`) and
each resume re-seeds from the full list. Non-numeric results
(`search_docs`, `get_page_info`, code search, `generate_chart`) are never
restored — once redacted, they stay redacted through every subsequent
resume; the model re-calls the tool if it still needs them. This is
deliberate: non-numeric results are the ones where staleness during an
unpredictable human-approval wait is a real risk, so they're always treated
as potentially stale rather than silently re-presented as current.

## Resuming — `POST /ask` with `approval_decision: "approved"`

`run_agent_turn` (`app/gateway/gateway.py`) branches on `body.approval_decision`
before building `initial_state`:

1. Read `live_turns/{conversation_id}`'s `pending_approval`. If it's missing
   (already resumed once, or a bogus request — the frontend disabling
   send-while-approval-card-showing is what should make this rare in
   practice), return the same generic fallback message any other
   unexpected failure gets (`GENERIC_TURN_ERROR_MESSAGE`) — no graph run.
2. **Delete the `live_turns` document — read first, then clear, before
   resuming.** Avoids a window where a second, racing resume request could
   read the same pending approval twice.
3. Build `initial_state` via the normal `build_initial_state(...)`, using
   `pending`'s `question`/`filter_context`/`active_page` (not the request
   body's — the body carries no question on a resume). Then override:
   `messages` (via `rebuild_paused_messages`), `tool_calls`, `iteration_count`,
   `bytes_consumed`, `bytes_consumed_baseline` (= `pending["bytes_consumed"]`,
   marking where this phase starts — see `.claude/rules/telemetry.md` for why),
   `approved_batch = True`, `approval_decision = "approved"`.
4. Run it through `consume_graph` exactly like a fresh turn. `route_entry`'s
   conditional edge off `START` sends a batch with `approved_batch` set
   straight to `call_tool` — **no separate resume node.** `call_tool_node`
   reads `state["messages"][-1].tool_calls`, which is the dangling
   `AIMessage`'s original request — the *same* batch that paused, BigQuery
   and non-BigQuery calls alike — and dispatches it normally, with
   `approved_batch` only skipping the pause decision, not the dry-run (still
   needed for `cost_cap_exceeded`) or anything else.
5. From there it's an ordinary turn: `agent`/`verify`/`check_length` all run
   as usual, including the possibility of a *second* pause later in the same
   resumed turn (each one gets its own pause row) or a hard decline if
   `bytes_consumed` crosses `ABSOLUTE_CAP` mid-loop — the user already paid
   for the approved query and can still get a decline; correct, if
   surprising the first time you see it.
6. `finalize` writes the **response row** the same way it writes any other
   row — post-approval work only, by construction: `bytes_consumed_baseline`
   is subtracted in `build_telemetry_row` so this row never double-counts
   bytes the pause row already reported (`.claude/rules/telemetry.md`).
7. History write and `live_turns` cleanup happen exactly as for a fresh turn.

## Rejecting — `POST /ask` with `approval_decision: "rejected"`

No graph run at all — rejection is a pure gateway short-circuit:

1. Same read-then-delete of `pending_approval` as the approved path (shared
   code, branches after).
2. Build and write a **minimal telemetry row** directly from `gateway.py`
   (`write_rejected_telemetry`) — the one case where a telemetry row isn't
   written by `finalize`, because there's no graph run for `finalize` to be
   part of. `question`/`filter_context`/`active_page`/`estimated_cost`/
   `pending_queries`/`deferred_dax` carry from `pending` for continuity;
   `bytes_consumed`/`iteration_count` are `0` (no new work happened, and the
   pause row already reported the running total); `cancelled: True` and
   `approval_decision: "rejected"` both set.
3. Return `AgentResponse(answer_markdown=REJECTED_MESSAGE, sources=[])`.

**Abandoned approvals need no cleanup code and no detection logic.** If a
user never responds, the `live_turns` document sits there until the next
turn on that conversation overwrites it (normal or another resume), or the
owning `sessions` document ages out on its own 30-day TTL (`SESSIONS_TTL_DAYS`,
`.claude/rules/gateway.md`) — nothing time-based targets `pending_approval`
specifically. They're already findable in telemetry regardless: a pause row
with no matching response row for the same `conversation_id` *is* the
record — one query, no scheduled sweep.

**A normal question arriving while a pending approval exists** is handled as
a frontend responsibility, not backend code — the send control should
disable while the approval card is showing, same `DisplayMode` mechanism
already used for "turn in flight" (`docs/frontend.md`). If that's ever
bypassed, the backend degrades safely with no special handling: a fresh
turn's `initial_state` never reads `pending_approval`, and `consume_graph`'s
first write to `live_turns` (`.set(...)`, not merged) overwrites the stale
object as a side effect.

**Safe to show the pending query verbatim:** it can only reference
`agent_safe` objects (IAM enforces this before it runs). Rendering is a
gateway/frontend concern — `.claude/rules/gateway.md`, `docs/frontend.md`.
