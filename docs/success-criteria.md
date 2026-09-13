# Success Criteria

**What proves each phase actually works.** Read the relevant gates *before*
building a piece, not just after — a gate like the sensitivity-slider check
is far more useful as a design constraint than as a surprise at phase end.

**Anything mechanical gets a test, not a checklist row.** Manual gates are
reserved for what needs human eyes: does it *look* right, does the *UX*
work, does the guarantee hold end to end.

**The automated suite (`docs/testing.md`) is deliberately scoped — 28 tests, one per main component and per guardrail category, plus real
end-to-end coverage** — not exhaustive per-edge-case coverage. A much finer-grained ~83-test version was archived outside this
doc set for personal reference, and is not part of the build.

## Coverage matrix

| Component | Verified by | Where |
|---|---|---|
| Authentication accepts valid creds, rejects invalid | Automated | `test_gateway_auth.py` |
| One user can't reach another's conversation (404, not 403) | Automated | `test_gateway_auth.py` |
| Static context has all seven components, with no DAX bodies in the measure registry | Automated | `test_static_context.py` |
| TMDL parses to the expected artifact — values, not just shape | Automated | `test_model_schema_parser.py` |
| Static context actually caches (`cache_read` non-zero on turn 2) | **Manual** — needs two real turns | Phase 3 |
| All 8 tools return a sensible result, called through the MCP server | Automated (parametrized) | `test_tools.py` |
| Chat history reaches the assembled prompt | Automated | `test_conversation_state.py` |
| Stored queries + filter context reach the prompt; truncation preserves numbers | Automated | `test_conversation_state.py` |
| Caching dispatch matches the model (switching models stays a config change) | Automated | `test_static_context.py` |
| Chart tool preserves the discriminator; dispatch renders | Automated | `test_chart_tool.py` |
| Every turn logs telemetry with all required fields | Automated | `test_telemetry.py` |
| Cancel flag actually stops the loop; BigQuery job is explicitly cancelled | Automated | `test_guardrails.py` |
| Each guardrail category fires (verification, batch cost + cumulative cap, row cap, answer-length, question-length, iteration cap) | Automated | `test_guardrails.py` |
| Approval resume produces a real answer, not just a pause | Automated | `test_approval_e2e.py` |
| Resumed turn can still call tools; pause/response rows don't double-count | Automated | `test_approval_e2e.py` |
| A real question produces a valid, cited answer end to end | Automated | `test_e2e.py` |
| Judge distinguishes a faithful claim from a fabricated one | Automated | `test_judge_e2e.py` |
| Filter capture includes slicer state, not just report/page filters | Automated | `test_guardrails.py` |
| Numbers never sourced from docs | **Manual** — needs a real turn | Phase 3 |
| `agent_safe` IAM boundary holds | **Manual** — adversarial prompt | Phase 3 |
| Approval flow triggers on cost | **Manual** — needs real BigQuery | Phase 3 |
| Screenshot / no-screenshot paths | **Manual** — UI | Phase 4 |
| Chart *values* match source data | **Manual** — spot check | Phase 4 |
| Response rendering (HTML, badges, cards) | **Manual** — visual | Phase 4 |
| Judge job produces scores | **Manual** — real run | Phase 6 |
| Cancel button | **Manual** — UI | Phase 4 |
| `generate_chart` doesn't fire before its data exists (batch-ordering guard) | **Manual** — needs a real multi-tool turn | Phase 3 |

## Per-phase manual gates

A phase isn't done because the code runs — it's done when the guarantee holds.

**Phase 1**
- [ ] A real message round-trips app → connector → gateway → chat.
- [ ] A request with a bad/missing Entra token is rejected before anything
      downstream runs.
- [ ] Same with a bad/missing API key — confirming **both** factors enforce.
- [ ] `executeQueries` smoke test returns 200 (setup guide Step 14).
- [ ] Exactly one `agent_telemetry` row per turn — even for the echo answer.
      `tool_calls` is empty until Phase 3; the row itself must exist now.

**Phase 2**
- [ ] A trivial tool is callable end-to-end: LangGraph → MCP over
      `streamable_http` → tool → back. No real tool yet, by design.

**Phase 3**

*Data prep & static context*
- [ ] **Ask two questions in a row, then check `cache_read` on the second.**
      Non-zero means the static block cached; both counters zero means it
      silently didn't (`.claude/rules/gateway.md`). Visible per-call in the
      LangSmith trace — no instrumentation needed. Costs real money to miss,
      and nothing errors.
- [ ] Ask *"what is this project about?"* — answered from the orientation
      bundle with **zero tool calls**. That's the bundle's whole job; if it
      calls `search_docs`, the static block isn't reaching the model.
- [ ] A DAX question requiring a cross-table relationship answers
      correctly — parsed relationships in static context are what supply
      that; vector search reliably misses it.
- [ ] A DAX question referencing a **disconnected** table (a measure-only
      or parameter table) answers correctly — the table registry is the
      only thing that knows it exists, since it never appears in
      the relationships list.
- [ ] **Rename a measure in Desktop, rebuild, and confirm the registry
      changed.** `git diff context/schema/model_schema.json` should show it.
      Forgetting the rebuild leaves the registry advertising a name
      `get_measure_dax` will reject — loud, but still wrong.
- [ ] **Confirm the Dataform assertions actually ran and passed** on the
      vector build — non-empty, non-null embeddings, unique chunk ids
      (`docs/data-pipeline.md`). Assertions silently not *running* is its
      own failure mode; check the run output, don't assume green means
      checked.
- [ ] **Spot-check a docs chunk in BigQuery** — `chunk_text` should open
      with its heading breadcrumb. Empty breadcrumbs across the board mean
      the attribute lookup missed; it fails silently.
- [ ] **Spot-check a described measure's chunk in BigQuery** — the `///`
      text must be present in `chunk_text`, not just in the source file.
      This is the silent-failure case (`docs/data-pipeline.md`).
- [ ] **No HTML display measure appears in `MEASURE_REGISTRY`** after a
      real build against the actual `.pbip`. The unit test uses a fixture;
      this confirms the manual exclusion list matches reality. A display
      measure in the registry is one `strict=True` would accept as a valid
      `get_measure_dax` argument (`docs/data-pipeline.md`).

*Filter context*
- [ ] A DAX question answers **against the active dashboard filters**, not
      the unfiltered model — proves filter context is actually reaching
      the prompt, not just being logged.
- [ ] **On the financial-impact page, move a sensitivity slider, then ask
      for that number. The answer must match what's on screen.** Then move
      it again and re-ask — the answer must change. If it returns the same
      value both times, the field-parameter filter isn't reaching the query
      and the disconnected table is defaulting (`docs/frontend.md`). Fails silently
      otherwise: the number is real, plausible, and from the wrong
      scenario.

*The graph & status plumbing*
- [ ] **Force a turn to fail mid-loop, then poll `/ask/status`.** It must
      not return a stale status from the failed turn — `live_turns` has to
      be cleared on error paths too (`.claude/rules/gateway.md`).
- [ ] **Poll `GET /ask/status` from a script during a slow multi-tool turn**
      and confirm the value actually *changes* as nodes execute — proves
      `astream` consumption is wired, well before Phase 4's UI shows it.
      A status stuck on one value means `ainvoke` semantics crept back in.

*run_bigquery_sql & its guardrails*
- [ ] A real numeric answer appears in the chat, with `tool_calls`
      populated with real timing.
- [ ] A deliberately expensive query routes to approval rather than executing.
- [ ] A slow tool call returns a recoverable error, and the BigQuery job is
      *actually* cancelled — check job status in the console, don't assume.
- [ ] An adversarial prompt aimed at a non-`agent_safe` table fails at IAM, not
      just at prompt guidance.
- [ ] Ask a question against a real, deliberately unfiltered large table.
      Confirm the BigQuery row cap actually fires against the *real*
      `query_job.result()` behavior, not just the mocked test — the exact
      parameter is flagged unverified in the component reference's open items.
- [ ] Ask a question likely to produce a long answer (many results, no
      natural aggregation). Confirm it triggers the answer-length retry
      and comes back summarized or as a chart, not a wall of table rows.
- [ ] Force the loop past its tool-call budget (a deliberately hard
      multi-part question). The response is a **partial answer** using
      whatever was already verified, the prose explicitly says it's
      partial and names what's missing, and `iteration_cap_hit` is `true`
      on the response — not a blanket refusal, not a silent truncation.

*Verification*
- [ ] A number never comes from `search_docs`, even when it's called in the
      same turn for narrative.
- [ ] A forecast question is declined per the scope rule.

*vector-search-sa / run_dax_query / get_measure_dax*
- [ ] Same check on the DAX side against a real `executeQueries` call —
      confirm the response's actual row-nesting shape matches what the
      row-count check assumes before trusting it.
- [ ] **Ask what a measure means without naming it** (*"how much does a
      typical customer spend per order?"*). It should resolve to the right
      measure from the registry's descriptions — this is the exact case
      vector search failed on.
- [ ] **Ask a follow-up that adapts a prior query** (*"same thing but for
      East"*) — confirm the agent builds on the stored DAX rather than
      composing from scratch, and respects the current filters.
- [ ] **A filter value containing a quote or apostrophe** (a real category
      name) either answers correctly or fails loudly and retries — never
      silently wrong. The model writes the DAX; the error path is the
      guardrail (`.claude/rules/mcp-tools.md`).
- [ ] **Ask how a measure is calculated** — confirm `get_measure_dax` is
      called and the returned DAX matches the `.pbip`. Then ask about a
      measure with a near-identical sibling (`Recall at 5` vs `Recall at
      10`) and confirm the right formula comes back, not a blend.

*generate_chart*
- [ ] **Ask for a "Pareto" of products** — confirm a *concentration* chart
      comes back (percentile x-axis), not an attempted 50k-bar chart.
- [ ] Ask a question whose natural answer is a trend or breakdown (not a
      single number). Confirm `generate_chart` is called with the right
      type (bar/line/concentration) against the *query's actual result*,
      not before it — the batch-ordering guard holding, not just existing.

*Remaining tools — docs, page info, code*
- [ ] A code question (*"how does the chunking script work?"*) resolves via
      `list_repo_files` → `read_repo_file`, not a vector search. Check the
      tool_calls in telemetry, don't assume from the answer.
- [ ] A *"what does page X show"* question routes to
      `get_page_info`, not `search_docs` — and asking about the *other*
      page while on one page returns that other page's content.

*Firestore conversation state*
- [ ] **Ask a follow-up that only makes sense with prior context** (*"what
      about last quarter?"*). It answers correctly — proving chat history
      reaches the prompt, not just Firestore.
- [ ] Confirm the `sessions` TTL policy is actually enabled
      (`gcloud firestore fields ttls list`). It's configured once at setup and
      silently does nothing if it was skipped.

*Approval workflow*
- [ ] **Exercise `POST /ask/respond` directly with curl or a script** — the
      approve/reject buttons don't exist until Phase 4, but the resume
      logic is built now and shouldn't go a whole phase untested. Same
      pattern as Phase 1's `executeQueries` smoke test. Using a
      `conversation_id` from a real paused turn, confirm:
      - **approve** executes *every* pending query (not just the displayed
        one) and the answer covers **both** halves of a compound question
        — proving pre-pause `tool_calls` survived the pause;
      - **reject** runs no query (check BigQuery job history, don't
        assume) and still returns a partial answer naming what it skipped.
- [ ] **Approve a query that isn't the last step**, and confirm the resumed
      turn calls more tools rather than answering from partial data.
- [ ] **Abandon an approval** (close the app without deciding), then query
      for pause rows with no matching response row — it should appear.

*Cross-cutting*
- [ ] A compound question fires multiple tools in one turn and the answer
      addresses **both** halves.
- [ ] Each of the 5 reasoning tools answers a real question end-to-end — the
      registration test proves they exist; this proves they *work*.

**Phase 4**
- [ ] A question answers correctly with **no** screenshot uploaded — the normal
      case, not a degraded one.
- [ ] With a screenshot, the answer uses it for context but cites no number
      from it.
- [ ] Spot check two values on a rendered chart against the raw tool result.
- [ ] **Ask for a Pareto by department** — confirm a *Pareto* comes back
      with readable labels, and its threshold line lands on the correct bar
      with deliberately unsorted input (the `reset_index` trap).
- [ ] During a multi-tool turn, the status text in the app **changes**
      through recognizable stages, not one frozen string — and the
      thinking icon still shows if a status gap appears.
- [ ] `needs_approval` renders as a visually distinct card.
- [ ] The approval card shows `pending_query` as a **readable, monospace,
      line-broken** code block — not collapsed onto one line and not
      truncated by the control's character limit — with
      `estimated_cost` shown alongside it.
- [ ] Clicking **Approve** returns a real answer that covers **both**
      halves of a compound question — proving pre-pause tool results
      survived the pause, not just the approved query.
- [ ] Clicking **Reject** returns a partial answer that names what it
      couldn't address, and the query never executes (check BigQuery
      job history to confirm, don't assume).
- [ ] A deliberately enormous query hits `cost_cap_exceeded` and offers
      **no approval buttons at all** — a distinct third card state.
- [ ] A normal turn (no approval needed) shows no query text anywhere —
      confirms the field is genuinely approval-gated, not always present.
- [ ] `iteration_cap_hit` renders as a visually distinct "partial answer"
      card — same treatment as `needs_approval`, not identical to a
      complete response.
- [ ] Ask a code question. The returned code renders **monospace with line
      breaks intact** — same treatment as `pending_query`, not collapsed
      default `<pre>`.
- [ ] **Close and reopen the app** — a fresh `conversation_id` is minted and
      the transcript starts empty; the prior conversation isn't reachable.
- [ ] **New chat with a non-empty transcript prompts for confirmation**;
      with an empty one (or after a failed start) it fires immediately.
- [ ] **Send button is disabled while a turn is in flight** — the only thing
      preventing two concurrent turns in one conversation.
- [ ] Source badges, HTML formatting, and chart display render correctly in the
      actual Power Apps control — not just valid HTML in theory.
- [ ] **Cancel button: press mid-turn.** Response returns promptly, the
      BigQuery job is actually cancelled (check job status, don't assume),
      and the message is distinguishable from a timeout.

**Phase 6**
- [ ] Judge job produces `agent_eval_scores` rows on schedule.
- [ ] Deliberately break a prompt; confirm the golden dataset suite catches it
      — a test of the test.
- [ ] CI runs Layer 1 with **no credentials configured**.

**Phase 7 (only what's built)**
- [ ] Forced tool choice actually constrains the turn.
- [ ] Layout toggle cycles without breaking the chat panel.
