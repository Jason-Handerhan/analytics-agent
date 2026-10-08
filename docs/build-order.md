# Build Order

## Current status — update this as we go

**Phase: 3 in progress — items 1-8 complete, `POST /ask` wired to the
real graph and verified live, and item 9 fully complete** — all three
remaining tools (`search_docs`, `get_page_info`, `get_repo_contents`) built,
live-verified, and now Layer 1 tested too (see the entries below item 8).
**Item 10 (Firestore chat history) complete, 2026-10-04** — ported into
`app/gateway/gateway.py`'s `run_agent_turn`, Layer 1 tested
(`tests/test_entry_exit.py`), and live-verified via
`notebooks/phase3_gateway_e2e.ipynb` (real write, real follow-up, real
read-back). **Item 11 (`combine_results`) complete, 2026-10-04** — ported
to `app/mcp_server/combine_tool.py`, dispatch wired in `orchestrator.py`,
Layer 1 tested (`tests/test_combine_tool.py`), and live-verified on Q2. Item 12 (`ask_user`) superseded by the clarifying-question design, 2026-10-05. **Item 13 (`live_turns` status polling) complete, 2026-10-06** -- the status path is `consume_graph`, `read_live_status`, and `GET /ask/status` in `app/gateway/gateway.py`, Layer 1 tested in `tests/test_gateway.py`, and live-verified in `notebooks/phase3_gateway_e2e.ipynb`. The final few statuses are best-effort; see `.claude/rules/gateway.md`.

**Item 14 (approval workflow) complete, 2026-10-08** -- pending-approval
pause/resume/reject fully built: `PendingApproval` (`app/orchestrator/state.py`),
the gateway pause path with a conditional `live_turns` delete (`consume_graph`),
message strip/rebuild (`_redact_tool_results`/`rebuild_paused_messages`,
`app/gateway/entry_exit.py`), `approval_decision` on `AskRequest` with its
exactly-one-of validator, `route_entry`'s conditional edge off `START`, and
`run_agent_turn`'s three-way branch (approved resume, rejected-as-cancellation,
fresh turn) in `app/gateway/gateway.py`. All three telemetry rows (pause,
approve, reject) write correctly, including a `bytes_consumed_baseline` fix so
a resumed turn's response row doesn't double-count bytes its pause row already
reported. Layer 1 tested across `tests/test_entry_exit.py`,
`tests/test_orchestrator.py`, `tests/test_gateway.py`, `tests/test_telemetry.py`;
live-verified via the resume-path sections added to `notebooks/phase3_graph.ipynb`
and `notebooks/phase3_orchestrator_e2e.ipynb`, and end to end through the real
HTTP routes in `notebooks/phase3_gateway_e2e.ipynb`'s own resume section --
approve and reject both pass; the `TABLESAMPLE` gate correctly forced multiple
sequential pauses on one turn, all approved and resolved correctly with
accurate telemetry; the absolute byte cap produced a correct partial answer;
and the dry-run-failure decline path works. The normal-question-while-pending
backstop is resolved as a frontend responsibility (disable send while the
approval card shows) rather than backend code -- the backend already degrades
safely if that's ever bypassed, since a fresh turn never reads `pending_approval`
and the next `consume_graph` run overwrites the stale doc regardless.

**Real deviations from the originally-documented design, still to reconcile in
the docs pass below:** resume replays the dangling `AIMessage.tool_calls`
through the ordinary `call_tool_node`, not a separate `execute_approved` node;
`PendingApproval.pending_queries` turned out to be informational only for
resume, not load-bearing; `last_activity_at`'s TTL field was fixed to write a
real future expiry instead of `now()` (it was expiring sessions almost
immediately); `get_db` was renamed to `get_firestore_client`.

**Docs pass (step 14 of this item) not yet done** -- `.claude/rules/gateway.md`,
`.claude/rules/orchestrator.md`, `.claude/rules/telemetry.md`,
`docs/approval-workflow.md`, `docs/frontend.md`, and `docs/testing.md` all need
updating to match what's actually built.

**Item 15 (cancellation) not started** — substantial remaining Phase 3 scope,
despite every *tool* from the original lineup now being done. All of items
1-6 promoted
out of
the notebooks into real code: `app/orchestrator/orchestrator.py` (state,
nodes, routing, graph) and `app/orchestrator/tools.py`
(`run_bigquery_sql`, `dry_run`, `submit_answer`) — verified live end to end
against a real MCP server, real Claude calls, and a real telemetry write.

**Clarifying question (folded into `submit_answer`), 2026-10-05** -- `submit_answer`
takes exactly one of `answer_markdown` or `clarifying_question`. A question turn
copies the question into `answer_markdown` so the length check, verification,
and `finalize` run unchanged, clears `suggested_follow_ups`, and writes
`clarifying_question` to `agent_telemetry` (NULL on answer turns). This replaces
the separate `ask_user` tool planned for item 12. Layer 1 tested
(`tests/test_submit_answer.py`, `tests/test_orchestrator.py`,
`tests/test_telemetry.py`). Live-verified in `notebooks/phase3_graph.ipynb` and
`notebooks/phase3_orchestrator_e2e.ipynb`, including the telemetry row.

**Item 6 (verification) done, 2026-09-25 — with real deviations from the
originally-documented design.** `verify_node`/`verify_response` are real, not
placeholders: shape-checks the submission via `SubmitAnswerArgs.model_validate`,
then checks every claim (`all_prose_numeric_claims` + `extract_table_values`)
against `build_numeric_pool` (only `run_bigquery_sql`/`run_dax_query` results
— an explicit allowlist, not a denylist, so an incidental number in code or
doc text can never leak in). Two deviations from the doc's original sketch:
- **Matching is precision-aware, not a flat `abs(claim - v) < 0.01` tolerance.**
  `claim_matches_pool`/`_decimal_places` round the pool value to the claim's
  own decimal precision (derived from its shortest string form) before
  comparing for equality — handles both ordinary rounding (a raw `0.379987`
  displayed as `0.38`) and percentage-scaled display (a raw fraction shown as
  a percent) without the false-accept risk a wider flat tolerance would carry
  on large numbers.
- **`extract_table_values` uses `mistune`'s real GFM table parser (AST
  walk), not hand-rolled regex.** Caught a real bug the regex version had —
  silently dropping bold-formatted cells (`**66.8%**`) — before it shipped.
- **Exhaustion fallback lives in `finalize_node`, not `verify_node`.**
  `verify_node` stays a simple two-branch function; `finalize_node` swaps in
  a static `VERIFICATION_FAILURE_MESSAGE` when verification ran and never
  passed (`not verified and not cancelled and not needs_approval` — the
  guard needed so a cancelled/paused turn, which never reached `verify_node`
  at all, doesn't get the same message).

Live-verified: a real turn where the model stated an invented rounded
threshold (`0.62`, describing "all above 0.62"), got correctly rejected by
verification with an actionable message, and self-corrected on retry by
dropping the number entirely rather than guessing a different one.

**Telemetry gained `all_prose_numeric_claims`** (new `REPEATED FLOAT`
column) and `suggested_follow_ups` is now a real `build_telemetry_row`
parameter instead of hardcoded `[]`. Added to the **live** table via
`ALTER TABLE ADD COLUMN`, not drop+recreate — confirmed against BigQuery's
own docs that adding a new column (never modifying/renaming an existing
one) doesn't have that restriction, contrary to `telemetry.md`'s original
blanket claim. `notebooks/alter_agent_telemetry_table.ipynb` is the new
scratchpad for this class of future additive migration, alongside the
existing `delete_agent_telemetry_table.ipynb` for real breaking changes.

**`tests/test_orchestrator.py` — first real Layer 1 coverage for the
graph module**, deliberately scoped to pure functions and plain-dict state,
no LLM/BigQuery/MCP mocking: `verify_response` (as the integration point
exercising `extract_numeric_values`/`build_numeric_pool`/`claim_matches_pool`
together), `extract_table_values` on its own (its failure mode — silently
under-extracting a cell — can't be observed through `verify_response`'s
pass/fail outcome, so it needs a direct test), all four routing functions,
and `iteration_cap_update`. Broader end-to-end/mocked tests are deliberately
deferred until the module is closer to final, not skipped.

**Item 7, `run_dax_query` half done and live-smoke-tested; `get_measure_dax`
still pending, 2026-09-27.** `run_dax_query` is registered on the real MCP
server and confirmed live end to end: a compound question using both
`run_bigquery_sql` and `run_dax_query` in one turn, a question that failed
verification and correctly got the honest-decline message, and a DAX-sourced
table answer. Real deviations from the doc's original sketch:
- **Auth/target/guardrail values (`access_token`, `workspace_id`,
  `dataset_id`, `row_cap`, `timeout_seconds`) are genuine per-call tool
  arguments, hidden from the model via `exclude_args`, not baked into the
  tool's own code.** `call_tool_node`'s `inject_dax_args` supplies them from
  `app.config`/`power_bi_auth.get_power_bi_token()` right before dispatch.
  `app/mcp_server/dax_tool.py` itself has zero project-specific imports —
  a different deployment could reuse it unmodified under its own credentials.
  Deliberately *not* wired for real per-end-user identity (OAuth
  on-behalf-of token exchange) — one shared service-principal token for
  every caller is an accepted, documented tradeoff for this project, not a
  limitation of the tool's own shape.
- **Fixed a real bug in `build_tool_call_record`'s MCP-result handling.** It
  assumed `message.artifact` held the raw tool result directly; live testing
  showed FastMCP/`langchain_mcp_adapters` actually wrap it
  (`{"structured_content": ...}`, further nested under `"result"` for a
  list-returning tool) — switched to reading `message.content` uniformly
  instead (unwrapping LangChain's content-blocks list when present). Never
  surfaced before this, since `ping` (the only prior MCP tool) returns a
  plain string.
- **Best-effort DAX cancellation, explicitly deferred to item 15** — no cost
  exposure like BigQuery's, so no urgency; not built as part of this item.
- **A real smoke-test finding, fixed via the system prompt, not code:** the
  model wrote literal `|` characters in a table header for absolute-value
  notation (`Mean |SHAP|`), which breaks GFM table parsing entirely —
  confirmed the malformed table is invisible to both `check_table_rows` and
  `extract_table_values`, a real gap that only didn't bite this time because
  the model also (redundantly) listed the same numbers in
  `all_prose_numeric_claims`. `SYSTEM_INSTRUCTIONS` now tells the model to
  escape a literal `|` as `\|`.
- `MAX_VERIFY_RETRIES` raised 2 → 3 based on live testing.

**Item 7 fully complete, 2026-09-27.** `get_measure_dax` added to
`app/orchestrator/tools.py` (not MCP-hosted — a pure lookup against the
committed schema, not a portable Power BI wrapper). One-hop dependency
expansion via plain key-matching (`f"[{name}]" in dax`), not regex —
deliberate, after regex bugs elsewhere in this build. `ToolCallRecord.result`
widened to `Any` (no type checker runs on this project; a precise Union needs
perfect upkeep or it's misleading). Verified live end to end with both
`run_dax_query` and `get_measure_dax` bound, including the one-hop dependency
case (`Champion Recall` → `Champion_Model`).

**Item 8, `generate_chart`, complete, 2026-09-30.** All ten spec classes
built with grain guardrails, MCP-hosted, and live-tested end to end through
GCS signed-url upload/read-back. Dispatch wiring (`resolve_chart_data`,
`inject_chart_args`, the bind-time substitution via `BIND_TOOLS_LIST`) landed
for real in `orchestrator.py`, replacing the notebook's by-hand chart data and
injected GCS args. State gained `chart_urls: list[str]` (plural — every
successful chart this turn, not just one), with a matching `agent_telemetry`
schema change (`chart_url` → `chart_urls REPEATED`, table dropped and
recreated by hand, not yet re-run against the live table).
`tests/test_chart_tool.py` is the new, consolidated Layer 1 suite (11 tests,
one per chart type packing render/dispatch/every guardrail into one function
rather than one test per concern — a deliberate pullback from a first,
46-test draft).

Two real deviations from the originally-documented design:
- **`check_batch_ordering` was never built as its own function — superseded
  by the `ref_id`/`CHARTABLE_TOOLS` mechanism.** A chart call's `source_ref`
  only resolves against `ref_id`s already in `state["tool_calls"]` (prior
  rounds only — the current round's own results aren't accumulated into
  state until after it completes), so a same-batch fetch-then-chart race is
  structurally unreachable rather than caught by an explicit same-batch id
  check. The model-facing field is also `source_ref`, not
  `source_tool_call_id` — a reference label the tool result prints
  (`"ref_1"`, ...), never the raw tool-call id.
- **`strict=True` dropped globally from `agent_node`'s `bind_tools()` call,
  still the standing trade-off.** Confirmed live: Claude's `strict=True`
  rejects `generate_chart`'s discriminated union (`oneOf`) and
  Concentration's `ge=`/`le=` bounds outright. A per-tool split (pre-convert
  just `generate_chart` to a raw Anthropic-format dict, which bypasses
  `strict` entirely) works, confirmed live, but ties the fix to Anthropic's
  own tool-dict shape — full reasoning in `docs/chart-tool.md` and
  `.claude/rules/orchestrator.md`. **Still circle back**: work out the
  OpenAI/Gemini equivalent, or confirm the global drop is the right call to
  keep long-term.

**`POST /ask` wired to the real graph, 2026-10-01 — not a numbered item, a
direct request to close the gap between "the graph works" and "the graph is
reachable over HTTP."** `/ask` had been a Phase 1 canned echo this whole time;
this is the first turn the real orchestrator has ever served over live
traffic. Three new entry/exit helpers landed in `orchestrator.py`
(`build_human_message`, `build_initial_state`, `build_agent_response` —
gateway-boundary concerns, deliberately not called by the notebook, which
builds its own state by hand) and `run_agent_turn` landed in `gateway.py`,
consuming `graph.astream(...)` via the exact `(kind, data)` tuple shape
already proven live in `phase3_graph.ipynb`'s own "Run it end to end" cell —
**not** the dict-shaped `chunk["type"]`/`chunk["data"]` pseudocode (and the
`version="v2"` argument) `.claude/rules/gateway.md`'s `run_agent_turn` sketch
shows; both are wrong, confirmed against the real proof, not yet fixed in
that doc. `GATEWAY_TURN_TIMEOUT_SECONDS` (115 — a deliberately thin ~5s
margin under Power Platform's non-adjustable 120s connector ceiling, so a
genuinely slow-but-answerable turn isn't truncated by our own cap) and
`MAX_QUESTION_CHARS` (2000) are new `app/config.py` constants. No live
status/history/approval-resume/cancellation — all explicitly out of scope,
deferred to items 10-12.

Being the first real traffic through the full pipeline surfaced two genuine,
previously-latent bugs — neither new, both just never exercised before:
- **The Dockerfile never copied `context/` into the image, and `.dockerignore`
  explicitly excluded it too.** `model_schema.py`/`context.py` read from it
  eagerly at *import* time, not lazily — harmless before now because
  `gateway.py` never imported `orchestrator.py` (only
  `app.orchestrator.models`, which does no file I/O) until this change. Fixed
  both: `COPY context ./context` added to the `Dockerfile`, `context/` removed
  from `.dockerignore`. Confirmed locally with a real `docker build` +
  `docker run` before redeploying.
- **`ANTHROPIC_API_KEY` was never set anywhere in the real app.** `agent_node`
  constructs `ChatAnthropic()` with no explicit key, relying on the
  environment — the notebook has always set this itself in its own
  "Notebook-only" cell, but nothing in `orchestrator.py`/`gateway.py`/
  `app/main.py` had a production equivalent. Fixed inside `init_orchestrator()`
  (`os.environ["ANTHROPIC_API_KEY"] = get_secret("anthropic-api-key", GCP_PROJECT_ID)`),
  ported into the notebook's copy too — the notebook's own separate line is
  now removed as genuinely redundant, not just stale.

**A third issue, unrelated to this task's own code but found at the same
time:** `agent-sa`'s write grant on `telemetry.agent_telemetry` was gone —
confirmed live (`403 ... Permission bigquery.tables.updateData denied`).
Table-level IAM doesn't survive a table drop+recreate (a new table is a new
resource, no inherited ACL), and the `chart_url` → `chart_urls` migration
(item 8) had done exactly that. Re-granted at the **dataset** level this time,
so future schema-migration recreates don't lose it again. `bq
add-iam-policy-binding` — the same command that originally granted
`agent_safe`'s `dataViewer` in Phase 0 — failed with "this feature requires
allowlisting" when tried against `telemetry`; worked via the classic,
pre-IAM `bigquery.AccessEntry` dataset ACL API instead (`entity_type=
"userByEmail"`, not `"serviceAccount"` — the REST schema has no separate
service-account entity type).

`tests/test_orchestrator.py` gained 3 tests for the new helpers (since moved to `tests/test_entry_exit.py`);
`tests/test_gateway.py`'s `test_post_ask_success` now mocks
`init_orchestrator`/`graph` (a small `_FakeGraph` stand-in) instead of
asserting the old echo text, plus a new test for the `MAX_QUESTION_CHARS`
rejection. Live-verified end to end against the deployed Cloud Run service:
real synthesized answers (not echoes), the question-length guardrail (400),
and the ownership check (404) all confirmed via curl.

**`search_docs` done, 2026-10-01 — item 9's first tool, built local in
`app/orchestrator/tools.py`, not MCP-hosted as originally documented.** The
hosting call was revisited once impersonation entered the picture: MCP's
JSON-only transport can't carry a live `Credentials`/`bigquery.Client`
object, and reaching `run_dax_query`'s level of real reusability would mean
externalizing the tool's entire schema (table, embedding column, embedding
model path, output columns) rather than just connection info — not worth
it for a single-corpus, 66-row tool. Full reasoning in
`.claude/rules/tools.md`'s hosting table and item 9 above.

Built and proven live in `notebooks/phase3_graph.ipynb` first (per the
established notebook-then-port workflow), then ported byte-for-byte into
`app/orchestrator/tools.py` (`get_vector_search_client`, `SearchDocsArgs`,
`search_docs`) and wired into `init_orchestrator()`'s `ALL_TOOLS`. New
`app/config.py` constants: `VECTOR_DB_DATASET`, `VECTOR_DB_TABLE`,
`EMBEDDING_MODEL`, `VECTOR_SEARCH_SA_EMAIL`, `SEARCH_DOCS_TOP_K_DEFAULT` (5),
`SEARCH_DOCS_MAX_TOP_K` (20, a corpus-size-appropriate guardrail cap, not a
tuned value). `top_k` is LLM-provided, not fixed — mirrors how `run_dax_query`
already lets the model steer `TOPN` itself.

First real use of `google.auth.impersonated_credentials` in this codebase —
`get_vector_search_client()` wraps `google.auth.default()`'s own credentials
(resolves to `agent-sa` on Cloud Run, the developer's identity locally, no
code branching) and mints a short-lived token for `vector-search-sa`, the
only identity with read access to `vector_db`. Needed two real IAM grants
neither `agent_safe`'s original setup nor `docs/data-pipeline.md`'s spec had
anticipated as separate steps: `roles/iam.serviceAccountTokenCreator` on
`vector-search-sa` for the calling identity, and `roles/bigquery.connectionUser`
on the `vertex_conn` *connection* (a different resource type than a dataset,
reached only via the BigQuery Connection API's own `getIamPolicy`/
`setIamPolicy` — `bq add-iam-policy-binding` and the Console UI both failed
against it, for reasons not fully root-caused).

**No Layer 1 test for `search_docs`, decided deliberately, not skipped
silently.** Unlike `get_measure_dax` (mocking `MEASURE_DAX` leaves real
control flow — the one-hop expansion walk — independently under test),
mocking `search_docs`'s only dependency (`get_vector_search_client`) mocks
the entire tool: everything left (building two `ScalarQueryParameter`s,
passing a mocked `.result()` through) is pass-through, not logic. The one
real piece of logic, the `top_k` clamp, is two lines already exercised live
in both notebooks. Layer 2 (manual, live) coverage already happened —
proven end to end in `notebooks/phase3_graph.ipynb` and
`notebooks/phase3_orchestrator_e2e.ipynb`.

**`strict=True` tool binding, done and live-verified 2026-10-01** — landed in
`orchestrator.py` first this round (a deviation from the notebook-first
workflow, caught and corrected), then ported into `phase3_graph.ipynb` and
confirmed live in both it and `phase3_orchestrator_e2e.ipynb`. `agent_node`
binds with `bind_tools(BIND_TOOLS_LIST, strict=True)`; `generate_chart`'s
stand-in is pre-converted to a raw Anthropic tool dict (no `strict` kwarg)
in `init_orchestrator()`, which `convert_to_anthropic_tool` passes through
untouched, keeping it non-strict while every other tool gets `strict=True`.
`SearchDocsArgs.top_k` lost its `ge=1` for the same reason `generate_chart`
needed the exception — confirmed `SubmitAnswerArgs.answer_markdown`'s
`min_length=1` survives strict mode fine, no equivalent fix needed there.
This resolves the `submit_answer` malformed-call failure from the live run
earlier the same day, for Anthropic — Phase 6 item 1 is where this gets
proven against OpenAI/Gemini too.

**`get_page_info` done, 2026-10-02 — item 9's second tool, built local in
`app/orchestrator/tools.py`, not MCP-hosted as originally documented.**
Same reusability-gap reasoning as `search_docs` (`.claude/rules/tools.md`).
Also a real simplification over the original design, tested live rather
than assumed: no `active_page` injection at all. `page_name` is a required
`Literal`, derived from the actual files in `context/page_info/`
(`PAGES = tuple(sorted(p.stem for p in PAGE_INFO_DIR.glob("*.txt")))`), and
the model supplies it every turn from `"Current dashboard page: {active_page}"`
— already in every `HumanMessage` — rather than code injecting a fallback.
Confirmed live across same-page and cross-page questions, consistently
correct, including one case where the model correctly refused to cite a
number from `get_page_info`'s content and re-ran a live BigQuery/DAX query
instead, unprompted, since that tool isn't in `NUMERIC_SOURCE_TOOLS`.

`context/page_info/*.txt` is sourced by a new section in
`scripts/build_model_context.py`: the two whole-page-info measures'
Scanner-API expressions are DAX string literals (the expression *is* the
HTML, confirmed, not assumed), so no live `EVALUATE` call is needed — just
unescaping the literal and running it through `unstructured.partition_html`.
Found and fixed one real markup issue along the way: the dashboard's
fraction-style formulas use a CSS `border-bottom` for the bar with no
distinguishing tag, which `partition_html` silently drops — fixed by
inserting a literal `/` before each numerator span's closing tag.

Found and fixed a second real bug, in `app/orchestrator/tools.py` itself:
`get_page_info` originally returned a bare `str`, which LangChain passes
through as raw `ToolMessage` content instead of JSON-encoding it the way
every other tool's `dict`/`list` return is — broke `build_tool_call_record`'s
`json.loads(...)` call immediately. Fixed by returning
`{"page_name": ..., "content": ...}` instead, matching every other tool's
result shape.

**`get_repo_contents` done, 2026-10-02/03 — item 9's third tool, replacing
the originally-planned `list_repo_files`/`read_repo_file` pair with one
merged, MCP-hosted tool** (`app/mcp_server/code_search.py`) — unlike
`search_docs`/`get_page_info`, this one *does* clear the reusability bar:
the hidden args (`owner`, `repo`, `branch`, `access_token`) are pure
identity/connection info, and GitHub's own Contents API already returns a
list or a file from the same endpoint, so one tool covers both without a
discriminated union. `FILE_DESCRIPTIONS` (`context/file_descriptions/
github_file_descriptions.json`) is a hand-written path→description map,
surfaced on every listing so the model doesn't have to guess a path blind.

Three real bugs found live, in order, each only surfacing once the
previous one was fixed:
- **GitHub silently returns no content for files over its 1MB inline
  limit** (`encoding: "none"`, `content: ""`) — `base64.b64decode("")`
  decodes to `""` with no error, so the tool looked like it fetched an
  empty file. Fixed by retrying via the response's own `download_url`
  (confirmed present on every file, not just the >1MB case) — but only for
  `.ipynb` paths specifically, since nothing else has a cleanup path and
  would just fail the length check below anyway.
- **A real notebook (3.6MB, mostly embedded chart PNGs as base64) blew the
  model's context window** (3,388,259 tokens > 1,000,000 max) once the fix
  above actually returned it. Fixed by stripping every cell's
  `outputs`/`execution_count`/`attachments` via `nbformat` before
  returning — confirmed live: 3.6MB → 63KB (98.4% reduction), still valid
  notebook JSON, same cell count. `nbformat` promoted from dev-only to a
  real `pyproject.toml` dependency (not `nbconvert` — checked its actual
  `uv.lock` dependency tree, 11+ packages including an HTML-export stack
  this tool never uses, vs. `nbformat` alone at 4 light deps) since this
  tool runs in production. A regex-based "strip long base64 runs" safety
  net was tried as a second layer, then dropped in favor of one
  deterministic `MAX_REPO_FILE_CONTENT_CHARS` (300,000) final-length cap —
  a hard size boundary, not a content-pattern heuristic, matching every
  other guardrail in this project.
- **A follow-on bug in the fix above**: stripping only ran on the
  >1MB-fallback branch, so a notebook *under* 1MB skipped it entirely.
  Fixed by checking `.ipynb` first and always routing through
  download-then-strip regardless of size, rather than branching on
  content-presence first.
Binary (non-text) files are now caught via `UnicodeDecodeError` rather than
a hardcoded extension list, failing with an actionable `ToolError` instead
of crashing.

**`SubmitAnswerArgs`' fields gained the same claimable/non-claimable split
`get_repo_contents` needed** — `answer_markdown`/`all_prose_numeric_claims`
now state plainly that only `run_bigquery_sql`/`run_dax_query` numbers are
checked at all (an inclusive "any other tool" framing, not an enumerated
list that goes stale when a tool is added), `search_docs` numbers are
banned everywhere (prose or table, including a code snippet quoted from a
doc chunk — `get_repo_contents` is the authoritative source for code), and
hand-rolled approximations/derived values are banned everywhere regardless
of source. `SYSTEM_INSTRUCTIONS`' `GROUNDING` section was trimmed to match
— the detailed version now lives only in `submit_answer`'s own fields,
read at the moment that matters, not duplicated in the static block.

Live-verified repeatedly, including a complex 3-part question (DAX query +
chart + a `search_docs`→`get_repo_contents` code citation) completing
cleanly in 42s with zero retries.

**Layer 1 test added, 2026-10-03, `tests/test_code_search.py` — 4 tests,
each asserting multiple paths, matching `test_chart_tool.py`'s "pack
several concerns per test" style.** Needed one small, behavior-preserving
refactor first: all the real logic lived in a closure (`_run()`) nested
inside the `@mcp.tool()`-decorated function, which FastMCP replaces with a
`FunctionTool` object in the module namespace (confirmed by
`get_repo_contents.handle_validation_error = ...` already relying on that),
so calling it directly would mean going through real MCP dispatch — Layer
2, not Layer 1. Pulled the closure out to a standalone `_fetch_repo_contents`
function instead; `get_repo_contents` is now a thin `asyncio.to_thread(...)`
wrapper around it. `requests.get` is mocked (`monkeypatch`); fixtures under
`tests/fixtures/code_search/` are real, trimmed GitHub API response shapes
plus a real tiny notebook (`nbformat`-built, with a fake chart output,
non-null `execution_count`, and a markdown attachment, to prove stripping
actually clears all three) and a real 67-byte PNG (confirmed to fail UTF-8
decoding on its very first byte, `\x89` — not a hand-picked byte sequence)
for the binary-file case. One real test bug found and fixed along the way:
`nbformat.writes()` can serialize a cell's `source` as a list of lines, not
always a joined string — the assertion needed to handle both.

**Orchestrator split into three modules, 2026-10-04 — file cohesion, not
item 10/14/15 progress.** `AgentState`/`ToolCallRecord`/`TurnError`/`append_list`
moved to new `app/orchestrator/state.py`; `build_human_message`/
`build_initial_state`/`build_agent_response`/`build_updated_history`/
`build_history_messages` (the gateway-facing entry/exit helpers, none of
them called by the graph) moved to new `app/gateway/entry_exit.py`;
`AgentResponse` moved from `app/orchestrator/models.py` (now deleted,
empty) to `app/gateway/models.py`, alongside `AskRequest`/
`ConversationResponse` — it's the gateway's own wire format, nothing in
the graph ever touches it. `entry_exit.py` itself landed under
`app/gateway/`, not `app/orchestrator/`, after the `AgentResponse` move --
keeping it under orchestrator would have meant it reaching backward into
`app.gateway.models` for that import; under `app/gateway/` its `AgentState`
import instead matches the project's existing gateway-depends-on-
orchestrator direction. Confirmed live, not assumed: `entry_exit.py`
alone avoids `langchain_anthropic`/`langchain_mcp_adapters`/`fastapi` (the
real heavy deps) — though `langgraph.graph.StateGraph` unavoidably loads
too, since it lives in the same package as `add_messages`, which
`AgentState` needs; Python always runs a package's `__init__.py` on any
submodule import, no way around it. `build_initial_state` also gained a
`history_messages` parameter, not yet wired to a real Firestore fetch in
`gateway.py` — still a `TODO` there, tracked under item 10.

**Plan added, 2026-10-04 — item 10 split in two, two new tool items added,
five items total now sequenced 10-15.** `combine_results` (item 11,
`docs/combine-tool.md`) lets the model stack or join multiple same-turn
tool results into one new `ref_id` — motivated by a live BigQuery cost-cap
investigation that surfaced a real architectural gap: a wide analysis split
across several cost-capped queries had no way to reach `generate_chart` as
one dataset. `ask_user` (item 12) is relocated here from Phase 7's
deferred-ideas list, design unchanged — building it right after item 10
while `sessions.history_messages`/`build_history_messages()` is fresh,
rather than picking it up cold later, per the user's call. **Neither tool
is built yet — this entry is planning only.**

**Item 10 itself narrowed to chat history only, 2026-10-04 — `live_turns`
split out into its own item 13.** The original item 10 bundled two
Firestore documents with two different lifecycles and two very different
build states: `sessions.history_messages` (read/write, FIFO-trimmed chat
history) was already proven live end to end in
`notebooks/phase3_orchestrator_e2e.ipynb` — real Claude calls, real history
read-back and write-back — and has since been ported into
`app/gateway/gateway.py`'s `run_agent_turn` (item 10 is now complete, see
the status block above). `live_turns` (`status`/`thinking_log`, the
`GET /ask/status` polling endpoint) has no notebook prototype at all and
hasn't been started. Bundling them under one item obscured that gap;
splitting them makes it visible. Sequenced after the two new tool items
(11-12), so the full Phase 3 order is now: 10 (chat history) → 11
(`combine_results`) → 12 (`ask_user`) → 13 (`live_turns`/status polling) →
14 (approval workflow) → 15 (cancellation).

**Doc correction, fixed 2026-10-04:** `.claude/rules/gateway.md`'s `sessions`
schema named this field `recent_messages`; the actual, now-shipped
implementation calls it `history_messages` (matching `app/config.py`'s own
`HISTORY_TURN_COUNT` comment). Code is authoritative — `gateway.md`'s whole
"Conversation history" section was rewritten to match the real, shipped
redaction design (`build_updated_history` replaces every successful
non-`submit_answer` result, `generate_chart` included, with a fixed
stale-result notice) in place of the originally-planned `HISTORY_ROW_CAP`
row-capping, which was never built and is removed from the docs that still
named it (here, `gateway.md` ×2, `orchestrator.md`).

_Last updated: 2026-10-04._ **Phase 0 (2026-09-13): all nine items verified
live against the real project, complete** — see git history for the full
verification detail if ever needed; kept brief here since it's done, not
current.

**Phase 1, all 7 items done.** Items 1+2 (echo `/ask`, `/conversation` +
ownership check) built and Layer 1 tested (`tests/test_gateway.py`,
9 tests). Item 3 (`Dockerfile` + deploy) done and verified live. Item 4
(custom connector) done and verified live — both operations return real
`200`s through real OAuth. Two real deviations from plan, both now reflected
in `docs/auth.md`:
- FastAPI emits OpenAPI 3.1.0, which Power Platform's importer can't parse
  at all — hand-wrote an equivalent Swagger 2.0 spec instead
  (`gateway-swagger2.json`, not committed — a one-off Power Platform import
  artifact, not app code).
- A single self-referencing Entra app (as both OAuth client and resource)
  hit a persistent `AADSTS90008` regardless of permissions/consent — fixed
  by splitting into two apps: `analytics-agent-connector` (resource) and a
  new `analytics-agent-connector-client` (OAuth client). Phase 0's "two
  Entra registrations" is now three.

Also fixed along the way: `validate_entra_token` checked the v2-endpoint
issuer format, but Power Platform's OAuth provider issues v1-format tokens
(`sts.windows.net`, not `login.microsoftonline.com/.../v2.0`) — confirmed
against a real token and corrected in `app/gateway/gateway.py`.

**Items 5+6 done together** — basic canvas app built (connector data source,
`App.OnStart` minting `varConversationId`, `TextInput`/Send/Gallery/New chat),
and a real message round-trips end to end (confirmed against the current
canned `Echo: ...` response — real synthesis is Phase 3). One real deviation
from `docs/frontend.md`, now fixed there too: **both** message roles render
through an HTML text control, not just the agent's — a plain Label couldn't
support the scrollable-long-message fix below, and user questions turned out
not to be reliably short. User text is manually HTML-escaped
(`&`→`&amp;` before `<`/`>`, order matters) before rendering, closing the
markup-injection gap a Label would otherwise have sidestepped. `TextInput1`
uses `TextMode.MultiLine` for wrapping, with typed newlines flattened to
spaces before use (Power Apps ties wrapping and Enter-inserts-newline
together; this decouples them).

**Gallery layout decision changed since this was built (2026-09-25):** the
app above still uses the original fixed-height (`TemplateSize`) gallery with
an internal `max-height` + `overflow-y: auto` scroll div. `docs/frontend.md`
now specifies the Flexible height gallery layout instead — rebuilding the
gallery to match is a real follow-up task, not done yet.

**Item 7 done.** `telemetry.agent_telemetry` created (location `US`, matching
the other three datasets — confirmed via `bq show`, not assumed) with the
full schema from `.claude/rules/telemetry.md`, including the three nested
`RECORD` fields — defined once in `app/telemetry/schema.py`, imported by both
`app/telemetry/create_table.py` (table creation) and `writer.py` (so
`insert_rows`'s type conversion and the table's real shape can't drift
apart). `app/telemetry/writer.py` builds a row and writes it via an
awaited `asyncio.to_thread(insert_rows, ..., selected_fields=SCHEMA)` call —
confirmed this is genuinely not backgrounded (Cloud Run sees the request as
in-flight for the whole duration, same as any other awaited I/O). One Layer 1
test (`tests/test_telemetry.py`), 10 passing overall.

**Deliberately not wired anywhere yet.** `orchestrator.md` already specifies
`finalize` as the real caller, but `finalize` doesn't exist until Phase 3
builds the graph — wiring it into the gateway now would just mean moving it
later. **Phase 3 item 4 (the graph skeleton) needs to call `write_telemetry_row`
from `finalize`, not just build the node fresh** — flagging here so that
isn't missed when this phase starts.

**Phase 1 complete.**

**Phase 2, both items done.** Proven first in `notebooks/phase2_mcp_server.ipynb`
(FastMCP server + trivial `ping` tool, `MultiServerMCPClient` over `transport:
"http"`, `ToolNode`/`tools_condition` — not a hand-rolled router), then moved
to real files: `app/mcp_server/server.py` (the `FastMCP` instance + `ping`,
placeholder until Phase 3 registers real tools) and `app/main.py`.

One real deviation from `.claude/rules/tools.md`: the MCP server runs as
its **own `uvicorn.Server`** alongside the gateway's, both launched via
`asyncio.gather()` in `app/main.py` — not mounted into the gateway's FastAPI
app. Decided to keep the MCP server genuinely separable (closer to a
lift-and-shift if it's ever split into its own Cloud Run service) without
taking on a second Dockerfile now. This raised the real risk it was chosen
to flag: a bare SIGTERM only stops one `uvicorn.Server`, which would leave
Cloud Run unable to scale to 0. Fixed with a watcher coroutine that polls all
servers' `should_exit` and propagates it to the others — public API only,
no private uvicorn methods. Verified with a real `docker build` +
`docker run` + `docker stop`: both servers logged a full independent
shutdown sequence and the container exited 0. `tests/test_main.py` covers
`_propagate_shutdown`'s own mirroring logic with plain fakes (no real
`uvicorn.Server`, no Docker) — it doesn't retest uvicorn's own
signal-to-`should_exit` wiring inside `serve()`, which is public and already
reliable; only the propagation code this project actually wrote.

That same verification pass caught a second bug before anything depended on
it: `app/main.py` called `mcp.http_app(path="/")`, which mounts the MCP
protocol endpoint at `/` instead of FastMCP's default `/mcp` — silently
breaking the client URL documented in `tools.md`
(`http://localhost:PORT/mcp`). Fixed by dropping the explicit `path`
argument; confirmed via `docker exec` that `/mcp` now returns the expected
`406` (endpoint alive, rejecting the plain GET only for missing MCP `Accept`
headers) and `/` no longer resolves.

Next: Phase 3, the tool-calling loop and its guardrails.

**Phase 3, item 1 (data prep) — `agent_safe` sub-part done and verified live,
two tables not three.** Real upstream tables confirmed against the ML repo's
own `.sqlx`
(github.com/Jason-Handerhan/Kaggle-Instacart-Reorder-Engine-Portfolio-Project),
not the placeholder in `docs/data-pipeline.md`'s original example: eight flat
`definitions/sources_*.sqlx` declarations (one file per table — a `.sqlx`
file compiles to exactly one action, confirmed against a real compile
failure when they were first stacked in one file; also flat, not nested in a
`sources/` subfolder, matching the ML repo's own proven layout).
`definitions/agent_safe/product_order_analysis.sqlx` enriches
`base_analytical_table` (already carries `product_name`/`aisle`/`department`,
no dimension re-join needed) with all six `prelim_*` silver feature tables,
52 columns, real descriptions on every one; `definitions/agent_safe/
candidate_reorder_features.sqlx` supplements `final_ml_features_table` with
names only, kept at its native (user, candidate product, anchor order) grain
for feature/label correlation questions — a deliberately different grain
from `product_order_analysis`, not a duplicate. Both tables carry a
table-level `description`, and `candidate_reorder_features` replicates
`final_ml_features_table`'s real `rowConditions` assertions from the ML
repo, extended with three null checks on the new name columns.
`product_order_analysis` gets an equivalent `rowConditions` set (no ML-repo
precedent to replicate) plus a standalone assertion confirming its row count
matches `base_analytical_table` — that pattern only proved correct after a
real compile failure (`SELECT 1` produces an unnamed column; BigQuery's
`CREATE VIEW`, which is what Dataform compiles every assertion into, rejects
that — fixed to `SELECT n`, and the same fix applied to `docs/
data-pipeline.md`'s vector_db assertion template, which had the identical
bug). **Compiled, executed, and all assertions passing in BigQuery Studio.**
`docs/data-pipeline.md`'s Part 1 example updated to match all of this.
**`vector_db.chunks_docs_embedded` — done and verified live, real deviations
from the original design.** Real README source is `context/docs/index.html` (one
file, not several) with `context/orientation/*.txt` holding the
already-manually-extracted Executive Summary/Project Navigator/System
Architecture pieces — `chunk_docs()` excludes that same block from
`index.html` by heading name (not a separate excluded file) before chunking,
confirmed against the real file (85/499 elements excluded, no trace of the
excluded prose survives). `Chunk` schema changed from the doc's original
`source_type`/`symbol_name`/`start_line`/`end_line` shape to
`doc_source`/`section`/`length` — deliberately not a generic
multi-content-type schema (decided 2026-09-19: a hypothetical future code
source would get its own table, not share this one). `chunk_by_title`'s own
chunk ids don't match source element ids (confirmed: 0/73) — fixed via
`orig_elements[0].id`, which does (73/73), not the element-order fallback
the doc originally proposed. `scripts/build_vector_db.py` built and verified
identical to the notebook proof (66 chunks, 0 missing breadcrumbs).
Embedding model is `gemini-embedding-001` (current #1 MTEB retrieval
quality — compared against `text-embedding-005` and `embeddinggemma-300m`
deliberately, not defaulted to the doc's original placeholder), needing
`vertex_conn` recreated in `US` after a real region-mismatch failure
(connection locations are fixed at creation time, same as datasets).
`definitions/vector_db/chunks_docs_embedded.sqlx` and
`definitions/sources_staging_doc_chunks.sqlx` (the missing declaration for
`staging.doc_chunks`, same category of fix as `agent_safe`'s tables) written
with `rowConditions` assertions, matching `agent_safe`'s proven pattern
rather than the doc's original unverified `nonNull`/`uniqueKey`. No vector
index — confirmed unnecessary (BigQuery's IVF minimum is 5,000 rows; the
real build has 66), not deferred. **Compiled, executed, real `VECTOR_SEARCH`
queries and a t-SNE/Plotly embedding-space plot both verified against real
data** (`notebooks/phase3_vector_search_test.ipynb`).

**`context/schema/model_schema.json` — done and verified live.**
`scripts/build_model_context.py` built from `notebooks/
dax_schema_exploration.ipynb`'s proven logic: `executeQueries`
(`INFO.VIEW.*`) for tables/columns/relationships, Scanner API for measure
`Expression` text. Auto-date tables excluded by name (`LocalDateTable_*`/
`DateTableTemplate_*`), same as `agent_safe`'s reasoning — not `IsHidden`,
since parameter tables are legitimately hidden too. Same logic caught a
second universal artifact: every table carries an identical
`RowNumber-<GUID>` index column (confirmed hidden, 25/25 tables), excluded
by exact name. HTML-display measures excluded via a manual list plus a
build-time regex backstop that hard-fails on an unlisted match — this
caught 3 real ones (`HTML_Model_Evaluation_Info`, `HTML_Icon_Author_Credit`,
`HTML_Financial_Impact_Info`) beyond the doc's original single example.
`parameters[]` covers two real kinds, not one: the 5 numeric what-ifs
(`range` read from each parameter table's actual values, not the
unavailable `GENERATESERIES(...)` formula) plus 2 field parameters
(`Evaluation Metric Parameter`, `Ensemble Weight Parameter` — an
`options: [{label, field}]` list, not a numeric range) found via a manual
list, the same pattern as the HTML measures — a full-model structural scan
was tried first and discarded as needless complexity for something this
small and rarely-changing. Final real counts: 23 tables, 65 measures, 8
relationships, 7 parameters.

**Phase 3, item 1 (data prep) is now fully complete — all three data
sources built and verified live.**

**Phase 3, item 2 (dashboard state capture) — complete, with a full
architecture deviation from the original design.** The originally documented
mechanism (Power BI JS SDK `getFilters()`/`getSlicers()`/`getActivePage()`,
called from a report embedded inside Power Apps via a `Power BI tile`
control) never got built — confirmed the tile control has no JS SDK access,
one-way `TileUrl` filter push only, and Microsoft's own docs plus community
reports confirm URL filters don't reach field parameters. Decided
2026-09-19/20 to flip the embedding direction instead: the Power Apps chat
app is embedded **as a Power Apps visual inside the Power BI report**,
gaining `PowerBIIntegration.Data` — a live, read-only Power Fx table
reflecting whatever fields/measures are dragged into the visual's Data well,
configured independently per report page.

Real deviations, all confirmed live:
- `PowerBIIntegration.Data` only populates for a visual instance created via
  **Create new** from inside the report, not an existing app selected in —
  though every documented existing-app workaround (re-editing via the
  visual's own "..." menu, a Gallery bound to `PowerBIIntegration.Data`)
  failed identically on both an existing app and a freshly-recreated one
  before turning out to be a publish-propagation delay, not a hard block: a
  second hard refresh of the **published** (not edit-mode) report resolved
  it both times.
- Field parameters (`Evaluation Metric Parameter`, `Ensemble Weight
  Parameter`) are excluded from capture — any `SELECTEDVALUE()` measure
  referencing a field-parameter table throws "This might be caused by a
  capacity or license issue" when dragged into the visual's Data well,
  confirmed isolated to field-parameter tables specifically (not a
  total-field-count cap, and not reproducible on any other table type). The
  5 numeric what-ifs and 3 dimensional slicers all work fine as ordinary
  `SELECTEDVALUE()` measures in the same well.
- `filter_context` entries are `{filter_column, value}` pairs, reusing
  `model_schema.json`'s `filter_column` naming — but the connector's
  hand-written Swagger 2.0 spec (Phase 1's OpenAPI-3.1 workaround) exposes
  `filter_context: list[dict]` as an array of schemaless objects, so Power
  Apps requires each row wrapped as `{Value: ParseJSON(JSON({filter_column:
  ..., value: ...}))}` — a `Dynamic`-typed column, not the record shape
  directly. `value` is `Text(...)`-coerced on the numeric entries too, since
  a `Table()` literal mixing numeric and text values in one column errors
  ("cannot be converted to a number") rather than widening silently.
- DAX/the semantic model has no concept of report pages, so `active_page`
  can't be captured as a measure like everything else. Captured instead via
  one hardcoded constant measure per page (`Active_Page_ModelEval`,
  `Active_Page_FinancialImpact`), each dragged into only its own page's Data
  well; read with `ParseJSON(JSON(First(PowerBIIntegration.Data)))` for
  duck-typed field access (each page's real `PowerBIIntegration.Data` schema
  differs) and `Coalesce()` to pick whichever one is actually present.
- `varFilterContext`/`varActivePage` are computed fresh in `btnSend.OnSelect`,
  not `App.OnStart` — `OnStart` runs once at launch and would otherwise
  freeze a stale snapshot for the rest of the session.

Real values verified end to end in both the Power Apps editor and the live
Power BI service report. `docs/frontend.md`'s embedding and "Visual
grounding" sections are rewritten to match this real design.

**Phase 3, item 3 (static context bundle) — complete, with real deviations
from the documented seven-component list and its file locations.** Proven
first in `notebooks/phase3_static_context.ipynb`, then moved to
`app/model_schema.py` (`TABLE_REGISTRY`, `MEASURE_REGISTRY`, `RELATIONSHIPS`,
`PARAMETERS`, `MEASURE_DAX`, `MEASURE_NAMES` — all parsed once from the
committed `model_schema.json`) and `app/orchestrator/context.py`
(orientation bundle, `BIGQUERY_SCHEMA`, `SYSTEM_INSTRUCTIONS`, both few-shot
sets, and the final assembly).

- **Neither file lives under `app/gateway/`, where the doc originally showed
  them.** `context.py`'s only consumer is the orchestrator's `agent` node
  (not yet built), so it moved to `app/orchestrator/context.py`.
  `model_schema.py` has two consumers headed to two different future
  services — the `agent` node and the `get_measure_dax` tool
  (`app/mcp_server/`, `.claude/rules/tools.md`) — so it stays a
  top-level, genuinely shared module (`app/model_schema.py`, a sibling to
  `app/config.py`) rather than nested under either. Decided this way
  specifically so a future gateway/orchestrator/MCP-server container split
  (already the documented target shape, `.claude/rules/tools.md`) is a
  lift-and-shift — each service's Dockerfile copies in whatever shared
  top-level modules it needs — rather than a real refactor. The detailed
  "Assembling the prompt"/"Model schema"/"Caching dispatch" content moved
  from `.claude/rules/gateway.md` to `.claude/rules/orchestrator.md` to
  match, so it auto-attaches for the files it actually describes.
- **Component order revised**: `SYSTEM_INSTRUCTIONS` moved first (role and
  behavioral rules established before the model sees any reference
  material — no caching cost either way, since the whole block is one
  static, byte-identical prefix regardless of internal order), and the
  few-shot examples split in two and interleaved with the schema they
  demonstrate — DAX examples right after `PARAMETERS`, BigQuery examples
  right after `BIGQUERY_SCHEMA` — rather than one trailing block.
- **`get_bigquery_schema()`/`get_static_context()` are lazy, `@lru_cache`-decorated
  functions, not the bare module-level constants the doc originally showed.**
  `BIGQUERY_SCHEMA` needs a live `bigquery.Client()` call; eager construction
  at import would make importing `context.py` require live credentials,
  breaking Layer 1 tests — the same reasoning already applied to a Firestore
  client elsewhere in this project. `@lru_cache` still guarantees the
  "computed once, not per turn" byte-identical-prefix requirement; it just
  moves *when* "once" happens from import time to first real use.
- Real deviations found writing the few-shot examples, verified live against
  the real dataset/model before being written down: `evaluation_metrics` has
  one row per model *per split* — a DAX measure summed with no split filter
  silently doubles (roughly) the real value; a measure can't be a filter's
  comparison value directly inside `CALCULATETABLE` (assign it to a `VAR`
  first); `TOPN(..., DESC)` selects the correct rows but `executeQueries`
  does not guarantee they arrive sorted; `power_bi_shap_barplots` only
  covers the four individual base models, never an ensemble. `MEASURE_REGISTRY`
  also excludes the 5 numeric what-ifs' value measures now — `PARAMETERS`
  already covers them fully (name, default, range), so listing them again
  under their own single-measure table header was pure duplication;
  `scripts/build_model_context.py` derives the exclusion set automatically
  from the parameters it just built, no manual list needed.

**Phase 3, item 4 (graph skeleton) — complete.** `app/orchestrator/models.py`
implements `Claim`/`AgentResponse` exactly as specified in
`.claude/rules/orchestrator.md`'s verification contract (from Phase 1).
**`Claim` was later removed (item 6/`submit_answer`'s own fields superseded
it) — `models.py` now has `AgentResponse` only.**
`app/orchestrator/orchestrator.py` has the five nodes, routing functions,
list-form `path_map` graph wiring, and `finalize`'s real
`write_telemetry_row` call — `MCP_TOOLS`/`SYSTEM_MESSAGE` populated lazily by
`init_orchestrator()`, not at import, so the module stays importable with no
live MCP server or BigQuery call (same reasoning as `context.py`'s lazy
`BIGQUERY_SCHEMA`). Verified end to end in
`notebooks/phase3_orchestrator_e2e.ipynb` — real Claude calls, a real local
MCP server, a real telemetry write. `notebooks/phase3_graph.ipynb` stays as
the original prototyping notebook, unchanged.

**No pytest for the graph/nodes themselves — still deliberate, not a gap.**
The only meaningful check is live, against a real MCP server and real Claude
calls (Layer 2). `app/telemetry/writer.py` now has real Layer 1 coverage
(`tests/test_telemetry.py`) since its logic is pure serialization, no live
call needed.

**Phase 3, item 5 (`run_bigquery_sql` + guardrails) — complete.**
`app/orchestrator/tools.py` has the tool, `dry_run`, and a lazily-
constructed `bq_client` (same `@lru_cache` pattern as
`app/telemetry/writer.py`'s `get_bq_client()`, so importing the module needs
no live credentials). Built and verified live: `maximum_bytes_billed` (the
engine-level fail-safe), the 40s timeout with explicit `cancel_job()`, the
dry-run-based `ABSOLUTE_CAP` tier in `call_tool_node` (turn-cumulative, not
per-query), `max_iterations` with partial-answer handling, and the
answer-length check. `call_tool_node` also now builds a `ToolCallRecord`
(with `query_text`, extracted from `args["query"]`/`args["dax"]`) and a
`TurnError` (joined via `tool_call_id`, not index-matching) for every call.
`finalize_node` computes `sources` and `prompt_tokens`/`completion_tokens`
and passes everything real to `build_telemetry_row()`. `agent_telemetry`'s
schema gained `tool_calls.id` and converted `pending_queries`/`deferred_dax`
to `REPEATED RECORD` to match `AgentState`'s `{id, query}`/`{id, dax}` shape
— table dropped and recreated, verified live with a full round-trip insert.
The 1,000-row cap is also built: one `job.result(timeout=..., max_results=
BIGQUERY_ROW_CAP)` call, with `rows.total_rows` (unaffected by `max_results`)
checked against the cap to fail loudly with an actionable `ToolError` rather
than silently handing back a partial result. Verified live for both the
under-cap and over-cap cases, both in the notebook and against the promoted
`app/orchestrator/tools.py`.

**`AgentState` gained `history_messages`** — a stub for conversation
history, kept deliberately separate from `messages` (writing history into
`messages` would make each turn's Firestore entry recursively include every
prior one). `agent_node` already prepends it to the LLM call. Not populated
yet — needs item 10's `build_history_messages()`.

**Keep this block current.** It's the only place that records where we
actually are — everything below is the static plan. When a phase completes,
update the line above and note anything that turned out differently from the
plan (a step that was skipped, a decision that changed). If you finish a step
and this block is stale, say so rather than guessing what's done.

**Also at each phase boundary:** check whether there's now enough real usage
data to revisit the prompt-cache TTL choice (5-minute default vs. 1-hour) —
pending decision, details in memory, not repeated here.

---

One phase at a time, in order. Within a phase, items are dependency-sequenced.

**Tests aren't listed as separate steps — that's deliberate, not an
omission.** Every step ships with its own **Layer 1** tests; a step isn't
finished until they exist and pass (`CLAUDE.md`, "How we work"). Layer 2
needs real credentials and runs manually. `docs/testing.md` is the source of
truth for what each piece needs, and `docs/success-criteria.md` holds both
the coverage matrix and the manual gates.

**Repo layout isn't documented here** — it's whatever actually exists on disk
in the repo, plus the canonical version in `local-dev-environment-setup.md`
Step 6 for setting it up the first time. Look at the filesystem rather than
trusting a static tree that could drift from it.

---

## Phase 0 — Foundations (console/CLI, little code)

1. ~~Licensing: Power Apps Premium needed~~ — **resolved, no purchase
   needed.** The free Power Apps Developer Plan confirmed to fully support
   custom connectors (creation and live use), tested empirically
   (`docs/frontend.md`). Power BI Pro remains sufficient, as originally
   planned.
2. Confirm Instacart reuse — BigQuery project, gold tables, GCS bucket.
3. Enable GCP APIs (`run`, `cloudbuild`, `artifactregistry`, `secretmanager`,
   `bigquery`, `aiplatform`, `cloudscheduler`). Cloud Run services are created
   *by* the first deploy, not provisioned ahead.
4. Create the three BigQuery datasets — `agent_safe`, `vector_db`, `staging`
   (setup guide Step 13). **Empty datasets only**; the tables inside them are
   built as one Dataform pass at the start of Phase 3, where the chunking
   script that feeds `vector_db` also lives. `agent-sa`'s `dataViewer` grant
   below needs the dataset to exist, not its contents.
5. Two Entra app registrations — full walkthrough in the setup guide Step 14:
   - **Power BI service principal** — client secret, security group, two tenant
     settings, **Contributor** on the workspace.
   - **Connector app** — Application ID URI + scope + secret. Its redirect URI
     can't be set until Phase 1 creates the connector; that's expected.
     **Became two apps in Phase 1** — this one stayed the OAuth *resource*;
     a second app (`analytics-agent-connector-client`) had to be added as
     the OAuth *client*, since a single self-referencing app hit a
     persistent `AADSTS90008` (`docs/auth.md`).
6. Create `agent-sa` + IAM (needs #4 first — `dataViewer` targets `agent_safe`).
7. **Secret Manager — all ten, one setup step** (setup guide Step 15),
   even though several aren't needed until later phases: `power-bi-sp-client-id`,
   `power-bi-sp-client-secret`, `azure-tenant-id`, `entra-client-secret`,
   `gateway-api-key`, `anthropic-api-key` (first used in Phase 3's graph
   skeleton), `github-read-token` (Phase 3's code tools),
   `langsmith-api-key` (Phase 3, once there's a loop worth tracing — optional),
   and `gemini-api-key`/`openai-api-key` (alternate providers, for model
   swapability). One creation pass and one grant loop beats revisiting Secret
   Manager every phase.
8. GCP Budget alert (setup guide Step 12), if the reused project doesn't
   already have one.
9. **CI/CD identity plumbing, pulled forward from Phase 6** (setup guide
   Step 16): `github-deployer`, Workload Identity Federation (a pool +
   provider + trust binding scoped to this repo — no JSON key, ever; see
   Step 16's 2026-09-13 decision note), and the `WIF_PROVIDER`/
   `PROD_SERVICE_ACCOUNT`/`PROD_REGION` GitHub repo variables. Doing this now
   means Phase 6 is just "write `ci.yml`," not "write `ci.yml` and also
   untangle IAM."

**Longest-lead item:** the Power BI tenant settings in #5 need Fabric/Power BI
admin rights. If you don't hold them, that's an external request — flag it
early rather than letting it block Phase 1.

## Phase 1 — Live end-to-end pipe

1. Echo gateway: `POST /ask` returns a canned answer — **with real auth
   validation built now**, not stubbed. Proving the auth flow end-to-end is
   this phase's job.
2. `POST /conversation` + the **ownership check** on every endpoint taking a
   `conversation_id` (`.claude/rules/gateway.md`). Build both now: the client
   needs an ID before it can poll anything, and the check is awkward to
   retrofit once four endpoints exist.
3. `Dockerfile` + deploy to Cloud Run with `--service-account=agent-sa
   --allow-unauthenticated`. The real first deployment, not a test.
4. Custom connector from the deployed gateway's OpenAPI; Entra OAuth.
5. Basic Power Apps canvas app wired to the connector — including
   `App.OnStart` calling `POST /conversation` with `IfError` handling, and
   the **New chat** button that re-runs it (`docs/frontend.md`).
6. Confirm a real message round-trips.
7. **`agent_telemetry` table + writer** (`.claude/rules/telemetry.md`) —
   schema defined fully now, populated over time. Landing it here means every
   turn has a row from the first one, and the write path is proven before any
   tool complexity exists. `tool_calls` is simply empty until Phase 3 fills
   it — that's what "defined now, populated later" means in practice.

**Dropped: proving `executeQueries` and a trivial FastMCP server here.**
Decided 2026-09-13. `executeQueries` is already proven — Phase 0's live
`executeQueries` smoke test (setup guide Step 14, done against the real
dataset) covers it, and re-proving it here would just repeat that call.
The trivial-FastMCP-server proof would only duplicate Phase 2 item 2, which
already exists for exactly this purpose; no value in proving the same thing
twice under two different names.

## Phase 2 — The MCP server

1. **Stand up the real FastMCP server** — `streamable_http` transport,
   localhost co-located (`.claude/rules/tools.md`). **Confirm the exact
   `MultiServerMCPClient`/FastMCP parameter names here** — this is the first
   real dependency on them, so don't assume from memory or docs.
2. **Prove it end-to-end with a trivial tool** — something that returns a
   fixed string, registered with `@mcp.tool()` and called through a
   single-node LangGraph over MCP. **Deliberately not a real tool:**
   `run_bigquery_sql` lands in Phase 3 with its full guardrail set, and
   building a half-guardrailed version here means either throwing it away or
   carrying it forward ungated.

## Phase 3 — The tool-calling loop and its guardrails

**Three ordering rules, and each saves real time.** **Build all three data
sources first** — the static context bundle is assembled from what they
produce, so nothing downstream works until they exist. **Stand the graph up before the tools**, so each
tool can be tested against a live LLM the moment it exists rather than after
all eight do. **Build guardrails with the first tool** — tool-scoped ones
where they belong, and the global loop caps too, since `max_iterations` and
the timeouts protect every test run from that point on.

Tools land in priority order: `run_bigquery_sql`, `run_dax_query`,
`generate_chart`. Those three answer the questions this project exists for;
everything after is supporting cast. **The approval pause and cancellation
come last** — both *interrupt* a working loop, and debugging an early-exit
path on top of tools that don't reliably work yet means never knowing which
layer failed.

1. **Data prep — all three sources, before any tool.** Two of the three are
   `.sqlx` in the same Dataform repository, so setting Dataform up twice is
   wasted effort (`docs/data-pipeline.md`):
   - **`agent_safe` enrichment tables** — dimension joins, `type: "table"`,
     with `columns:` descriptions written properly. Those descriptions *are*
     the agent's schema grounding, read live via `get_bigquery_schema()`
     (`client.list_tables`/`client.get_table`, not `INFORMATION_SCHEMA` —
     `.claude/rules/tools.md`).
   - **`vector_db.chunks_docs`** — run `scripts/build_vector_db.py` locally
     first to populate `staging.doc_chunks`, then execute the `vector_db`
     tag. **Always that order:** the model reads a staging table the script
     creates, and a stale one builds a stale index with no error. **One
     table**, docs only. **Exclude** the orientation content — it's served
     statically, so indexing it wastes a retrieval slot. Also excluded:
     page-info HTML (`get_page_info` reads it whole) and code (search is
     agentic, `docs/code-search.md`).
   - **`context/schema/model_schema.json`** — run
     `scripts/build_model_context.py` and **commit the result**. Not a
     BigQuery table and not embedded: it's built by querying live Power BI
     (`executeQueries`/`INFO.VIEW.*` for tables, columns, measure names,
     relationships; the Scanner API for DAX expressions and parameter
     detection — decided 2026-09-17, replacing an earlier `.pbip`/TMDL-parsing
     design, `docs/data-pipeline.md`) and read into static context at
     startup. This is what makes the semantic model deterministic to query
     rather than retrieved. The table-count mismatch between the two APIs is
     resolved (Power BI's auto-generated date tables — excluded by name), and
     so is the what-if parameter `range` gap (read directly from each
     parameter table's real values via `executeQueries`, not the calculated
     table's formula) (`docs/data-pipeline.md`).

   **This has to precede the static context bundle**, not just the tools:
   four of its seven components (`TABLE_REGISTRY`, `MEASURE_REGISTRY`,
   `RELATIONSHIPS`/`PARAMETERS`,
   `bigquery_schema`) are built from what lands here.

2. **Everything the connector sends about dashboard state, in one pass —
   not just the obvious filters.** This is **authoritative for dashboard
   state** (`docs/frontend.md`), so `run_dax_query` answers semantic-model
   questions *against the user's current filters*. Building and testing the
   DAX tool against an incomplete capture means re-verifying later anyway.
   Three things, and the first two are easy to build partially without
   noticing:
   - **`report.getFilters()` + `page.getFilters()`** — report- and
     page-level filters.
   - **Slicer state — its own call, not covered by the above.**
     `getFilters()` does not return slicer selections at any scope.
     `page.getSlicers()` + `getSlicerState()` per slicer, or a sensitivity
     slider silently vanishes: not a wrong value, an *absent* one, and the
     agent answers against the wrong scenario with nothing to signal it.
   - **`report.getActivePage().displayName`, as its own field — not part of
     `filter_context`.** A page name isn't a filter; it feeds `get_page_info`,
     not `run_dax_query`.

   The **screenshot** half of the multimodal prompt stays in Phase 4 — it's
   layout/attention only, never load-bearing for correctness.
3. **Assemble the static context bundle** (`.claude/rules/gateway.md`) — the
   always-present block sent every turn, never vector-searched. **Seven
   components**, in a fixed order (a cache hit needs a byte-identical
   prefix), of which the first is itself a bundle of three:
   1. **Orientation bundle** — one file, one-time manual extraction, not a
      script. Three pieces inside it: Executive Summary + Project
      Navigator + the architecture diagram. **Draw a simple ASCII version of
      the diagram** — the styled HTML in the README stays there, for humans.
      Static context holds semantic content, never presentation: the model
      needs the boxes and arrows, and `style='...'` attributes are pure token
      cost.
   2. **`TABLE_REGISTRY`** — every table with columns, types, and
      descriptions, rendered from step 1's `model_schema.json`. Covers
      disconnected tables that relationships can't.
   3. **`MEASURE_REGISTRY`** — every measure's name and description, same
      source. **Names and descriptions only, never the DAX bodies.**
   4. **`RELATIONSHIPS` + `PARAMETERS`** — join paths and what-if/field
      parameters, from the same artifact. Structural facts needed on nearly
      every DAX composition; never retrieved.
   5. **`get_bigquery_schema()`** — lazy, `@lru_cache`d on first real use, not
      a bare module constant and not per-request either
      (`.claude/rules/tools.md`).
   6. **System instructions.**
   7. **The few-shot examples** (`.claude/rules/gateway.md`).

   Components 2 and 3 are what make the semantic model *deterministic* to
   query — the model can't fail to find a measure, or invent one. The
   `_render_*` split is in `.claude/rules/gateway.md`.

   Then wire `build_static_context(model, static_text)` — the caching
   dispatch (`.claude/rules/gateway.md`). It takes the **model name**, so a
   model swap stays a one-line config change.
4. **The graph skeleton, before any tool exists.** Five nodes — `agent`,
   `call_tool`, `check_length`, `verify`, `finalize` — wired with an empty
   tool list (`.claude/rules/orchestrator.md`). `agent` writes
   `answer_markdown` itself whenever it has no tool calls; no separate
   structured-output call. `route_entry` is approval-specific and lands
   with #11 (as a conditional edge off `START`, not a node — see item 14's
   entry above for how this actually landed).
   **Consume with `astream`, not `ainvoke`, from the start** — status
   updates depend on it, and switching invocation style later means touching
   every call site (`.claude/rules/gateway.md`). Status *strings* land in
   Phase 4; the *plumbing* has to be right now. Standing this up first is
   what lets every tool below be tested end-to-end the moment it's written.
   **`finalize` must call `app/telemetry/writer.py`'s `write_telemetry_row`**
   (built and tested in Phase 1, deliberately left unwired until this node
   exists) — not a new write path, just the first real caller.
5. **`run_bigquery_sql` with every guardrail — tool-scoped *and* global — in
   the same step.** Schema grounding is already in place (`get_bigquery_schema()`,
   part of item 3's static context bundle — not a separate MCP resource,
   `.claude/rules/tools.md`), so this item is just the tool itself. Debugging
   a tool-calling loop against ungated BigQuery is how you generate an
   expensive surprise, and the loop-level caps protect every test run from
   here on, not just this tool:
   - **Tool-scoped:** the dry-run cost gate and three tiers,
     `maximum_bytes_billed`, the 1,000-row cap, the 40s timeout with explicit
     `client.cancel_job()`.
   - **Global, and cheap to build now** (`.claude/rules/orchestrator.md`):
     `max_iterations` with partial-answer handling — the one that stops a
     runaway loop burning tokens — plus the ~90s gateway timeout and the
     answer-length check. All simple; none benefits from waiting.
6. **Verification** (`.claude/rules/orchestrator.md`) — `verify_response`'s
   pooled numeric matching, and the honest-decline path. Global rather than
   tool-scoped, but it exists *because* of numeric answers, so it lands as
   soon as the first tool that produces them does.
7. **`run_dax_query` with its guardrails**, plus `get_measure_dax`
   (`.claude/rules/tools.md`). Grounding is already structural — the
   registries landed in step 3 — so there's no search-before-query pairing
   to build. DAX's own guardrails land here: `TOPN` steering plus the
   deterministic post-fetch row check, and best-effort cancellation.
8. **`generate_chart`** — the third critical tool, and it must come after
   both query tools since it renders their output.
   **`bar`, `line`, and `concentration` only** (`docs/chart-tool.md`); the
   remaining specs are cheap to add later and none is on the critical path.
   The **batch-ordering guard** lands here — it only becomes testable once a
   query tool and a chart tool both exist.
9. **The remaining tools, in descending criticality.** `search_docs` first,
   and with it **`vector-search-sa` + impersonation** — it's the only tool
   that reads `vector_db`, and this is what keeps `run_bigquery_sql` scoped
   to `agent_safe` (`docs/data-pipeline.md`). **Built as a local tool in
   `app/orchestrator/tools.py`, not MCP-hosted** — a deliberate deviation
   from how this item was originally planned, decided once a real
   implementation made the reusability gap concrete: achieving it would mean
   externalizing the tool's entire schema (table, embedding column, model
   path, output columns), not hiding connection info the way `run_dax_query`
   does (`.claude/rules/tools.md`, 2026-10-01). **Then `get_page_info` —
   also built local, not MCP-hosted, same reusability gap as `search_docs`,
   and simplified past its original design** (2026-10-02): no `active_page`
   injection at all. `page_name` is a required field; the model picks it
   every time from the "Current dashboard page" text already in the
   `HumanMessage`, confirmed live across both same-page and cross-page
   questions. `context/page_info/*.txt` is sourced by a new section in
   `scripts/build_model_context.py`, reading the two whole-page HTML
   measures' DAX string literals directly (no live `EVALUATE` needed — the
   expression *is* the HTML) and converting to plain text via
   `unstructured.partition_html`. Then `list_repo_files` / `read_repo_file`
   with the `github-read-token` secret and the hand-written
   `FILE_DESCRIPTIONS` map (`docs/code-search.md`). The code tools are
   genuinely least critical — build them last.
10. **Firestore chat history** (`.claude/rules/gateway.md`) — the `sessions`
   document's `history_messages` field: last `HISTORY_TURN_COUNT` turns with
   their queries and filter context, `user_id`, `last_activity_at`. Wires
   short-term chat history into the prompt — read back at turn start
   (`build_history_messages`), written back at turn end
   (`build_updated_history`), both in `app/gateway/entry_exit.py`.
   **Not an optimization — an in-memory dict breaks under Cloud Run's
   ordinary multi-instance scaling.**
11. **`combine_results` tool** (`docs/combine-tool.md`) — lets the model
   stack or join several same-turn `run_bigquery_sql`/`run_dax_query` (or
   another `combine_results`) results into one new `ref_id`, so a wide
   question that had to be split across several cost-capped queries can
   still reach `generate_chart` as one dataset. MCP-hosted, with no hidden
   args at all — purely a data reshape, the most portable tool in the
   inventory. Model picks `stack` vs. `join`; guardrails in the real tool
   catch a mismatched choice rather than silently returning garbage. Stays
   out of `NUMERIC_SOURCE_TOOLS` — it only recombines numbers already
   verified via their original tool call, never computes a new one, so the
   verification contract needs no change. Full design, including the
   motivating cost-cap investigation: `docs/combine-tool.md`.
12. **`ask_user` tool — a second way to end the turn, alongside
   `submit_answer`.** Needs none of `pending_approval`'s persistence trick.
   That trick exists only because `pending_queries`/`deferred_dax` are real
   unexecuted work that must survive the request boundary intact — even
   `pending_approval` doesn't literally pause a running process across an
   HTTP round-trip, it genuinely ends the turn and starts a new
   `graph.astream()` on resume, just fed the cached state so it *feels*
   continuous. `ask_user` has no equivalent unexecuted work: once called,
   there's nothing left pending, so the turn can just genuinely end —
   `sessions.history_messages`/`agent_telemetry` written normally, `live_turns`
   cleared normally, no new Firestore field, no resume endpoint. Mechanically:
   recognized in `call_tool_node` before the normal batch-dispatch path (sets
   `question_asked=True` + `clarifying_question` from args), routes straight
   to `finalize` from `route_after_call_tool` (bypassing `check_length`/
   `verify` — nothing to verify, same reasoning as `needs_approval`/
   `cancelled` already skipping them), and rides a normal telemetry row like
   `iteration_cap_hit` does today rather than `needs_approval`'s special
   pause row. The user's reply is just the next ordinary `POST /ask` —
   same `route_entry` → `agent` path any follow-up takes, with the
   clarifying exchange already in `history_messages` via the normal
   `sessions.history_messages` read-back. One honest trade-off: the tool
   calls made before the question demote from this turn's live messages to
   history one turn earlier than they otherwise would, so their results get
   redacted to the stale-result notice a turn sooner than they otherwise
   would — minor, already an accepted property of history elsewhere. Real
   motivating case (2026-09-30 transcript): asked for "training vs. test,"
   the model discovered no training split exists, silently substituted
   validation vs. test, and explained the substitution only after already
   computing and charting it — a clarifying question up front would have
   been the better UX. Open question: whether it counts against
   `MAX_ITERATIONS` (leaning no, same exemption as `submit_answer`).
   **A `generate_chart` `ref_id` from before the question won't resolve
   after it** — `state["tool_calls"]` resets fresh next turn like any other
   turn boundary, so a chart referencing data fetched pre-question fails the
   same way a stale `source_tool_call_id` replayed from history already does
   (`.claude/rules/gateway.md`) — an existing, already-actionable `ToolError`,
   not a new failure mode, just a re-fetch. Likely rare in practice (the
   model usually asks *because* it doesn't have the data yet), but verify
   live once built, not just assumed from this reasoning.
   **Built right after item 10, not deferred to Phase 7** (2026-10-04) —
   its reply path depends on the exact same
   `sessions.history_messages`/`build_history_messages()` plumbing item 10
   builds, so implementing it immediately after, while that machinery is
   fresh, costs less than picking it up cold later as a standalone item.
13. **`live_turns` — live status polling** (`.claude/rules/gateway.md`) — **built 2026-10-06** (the names below, `set_status`/`append_thinking`, became `consume_graph`/`log_thinking`) —
   the `live_turns` document's `status` and `thinking_log` fields only;
   `cancel_requested` and `pending_approval` are items 14 and 15's own
   concern, written there, not here. `set_status`/`append_thinking` calls
   threaded through `run_agent_turn`'s `astream` loop, the
   `GET /ask/status/{conversation_id}` polling endpoint, and clearing
   `live_turns` on every exit path (success, timeout, error — not just the
   happy path). **Split out of the original item 10, 2026-10-04** — no
   notebook prototype exists for any of this yet, unlike item 10's chat
   history half.
14. **The approval workflow** (`docs/approval-workflow.md`) — **built
   2026-10-08** (see the status block above for what actually landed: no
   separate respond endpoint — `approval_decision` on `AskRequest` instead
   — and no `execute_approved` node; resume replays the dangling
   `AIMessage.tool_calls` through the ordinary `call_tool_node`) — the
   pause, `PendingApproval` caching, `route_entry`, and the hard-decline
   tier. Depended on #5's cost tiers and #10's/#13's Firestore state, as
   planned. The UI half (approve/reject buttons) still lands in Phase 4;
   verified directly via the real `/ask` route in the meantime, same
   pattern as Phase 0's `executeQueries` smoke test.
15. **User cancellation** — `POST /ask/cancel/{conversation_id}` writing the
   `cancel_requested` flag, and the node-level checks that route to
   `finalize`. The per-tool cancellation mechanism already shipped with
   `run_bigquery_sql` in #5; this is the turn-level path on top of it. The
   Power Apps button lands in Phase 4.

   **When this lands, also make `run_agent_turn`'s own turn-timeout path
   (`app/gateway/gateway.py`) reuse it.** Today, `asyncio.wait_for`'s timeout
   only cancels the `asyncio` task locally, which does *not* stop a BigQuery
   query running inside `run_bigquery_sql`'s `asyncio.to_thread()` call --
   `asyncio` cancellation can't reach a thread. On timeout, write
   `cancel_requested: true` to `live_turns/{conversation_id}` (the same flag
   `POST /ask/cancel` writes) before returning the fallback response, so the
   real per-tool `cancel_job()` mechanism from #5 actually fires instead of
   leaving an orphaned query running server-side with nothing watching it.

## Phase 4 — Multimodal grounding & response formatting

1. Screenshot upload through the connector (filters, slicers, and active page
   already landed in Phase 3). The image is layout/attention only — **never** a source of
   numeric values.
2. Response formatting: HTML, source badges, approval cards (incl. the
   approve/reject buttons for Phase 3's workflow), chart display.
   **Decide before building chart display**: native `Image` control bound to
   `chart_url` vs. inline `<img>` embedded in `answer_markdown` (both
   confirmed to work; the inline option needs `MAX_ANSWER_CHARS` 6000 → 7000
   and a mistune renderer change) — full write-up in `docs/frontend.md`.
3. **The cancel button** — a Power Apps button firing
   `POST /ask/cancel/{conversation_id}` (`docs/frontend.md`). The endpoint and
   cancellation path shipped in Phase 3; this is the UI half, alongside the
   approve/reject buttons above.
4. Progress indication, sample questions, follow-up chips — including the
   `thinking_log`/`append_thinking` design (`.claude/rules/gateway.md`,
   `docs/frontend.md`): a new `live_turns` field plus a gallery row per
   summarized-thinking round, styled distinctly from a real answer.
5. The remaining `generate_chart` spec types beyond Phase 3's three
   (`docs/chart-tool.md`) — add as real questions call for them.

## Phase 5 — Skipped

XMLA is a documented backup only. REST is the plan.

## Phase 6 — Evaluation & polish

1. **Model swappability — prove it, don't just design for it.** Deliberately
   placed *before* the judge (#2): the plan is to use the judge itself to
   compare providers, so swapping has to actually work first, not just be
   designed to work. Real gap found live (2026-10-01): `get_static_context`/
   `build_static_context` already branch correctly per provider (Claude/
   OpenAI/Gemini caching, `.claude/rules/orchestrator.md`), but `agent_node`
   — the actual LLM call driving the tool loop — hardcodes
   `ChatAnthropic(model=MODEL, thinking={...})` directly, not
   `init_chat_model(MODEL)`, and `thinking` is Anthropic-only syntax.
   Changing `MODEL` in `app/config.py` today would not swap the agent to
   GPT or Gemini; it would just break. Scope:
   - Switch `agent_node` to `init_chat_model(MODEL)`, with the `thinking`
     param made conditional/provider-gated rather than unconditional.
   - Confirm OpenAI's and Gemini's real `strict=True` equivalents **live**,
     not assumed (`.claude/rules/orchestrator.md`'s "needs verifying, not
     assuming" note) — this is also where the `submit_answer` malformed-call
     fix from the 2026-10-01 live failure gets resolved, since whatever fix
     is chosen there has to survive this test, not just work for Anthropic.
     A fix tying `submit_answer` to Anthropic's own raw tool-dict shape
     (considered, not applied) would fail this item outright.
   - One real end-to-end turn run against each of the three providers,
     confirming a correct, verified answer comes back from all three —
     not just that the call doesn't error.
   - **`append_thinking` (`app/gateway/gateway.py`) also needs a per-provider
     answer, not just `agent_node`.** It reads `response.content` for a
     `{"type": "thinking", ...}` block — Anthropic's own content-block
     shape, produced by `agent_node`'s `thinking=` param. OpenAI's reasoning
     models don't expose raw reasoning content the same way via API, and
     Gemini's "thought" format differs too, so `thinking_log`/
     `GET /ask/status` would silently go blank (not error) on a non-Anthropic
     provider unless this is handled explicitly — confirm live per provider,
     don't assume it degrades gracefully just because it doesn't crash.
   - `gemini-api-key`/`openai-api-key` secrets already exist (Phase 0 item
     9), provisioned for exactly this and unused until now.
2. Judge Cloud Run Job + Cloud Scheduler (`docs/llm-judge.md`) — consumes
   the `agent_telemetry` table already logging since Phase 1. Depends on #1:
   comparing providers' faithfulness scores needs provider-swapping to
   actually work.
3. Golden dataset regression suite (`docs/golden-dataset.md`) — a separate,
   event-triggered mechanism, **not** a subset of #2. Mostly hand-curation;
   Claude Code's part is `scripts/run_golden_tests.py`, the comparison
   runner.
4. CI/CD workflow: `github-deployer`, WIF, and the repo variables are already
   done (Phase 0 item 9) — this is just writing
   `.github/workflows/ci.yml` with a `test` job and a `deploy` job gated by
   `needs: test`. **The deploy job is two steps** — build a
   `${{ github.sha }}`-tagged image, then deploy *that image*
   (`docs/ci-cd.md`), not the `--source .` command used manually through
   Phases 1–5. Its `auth@v2` step uses `workload_identity_provider`/
   `service_account`, not `credentials_json` — no key exists to use.
5. Share the Power Apps app — the one auth step that's neither code nor IAM.
6. Demo video + business-first README.

## Phase 7 — Optional (no strong ordering)

- **Scheduled model-schema rebuild, replacing the manual script.** A periodic
  Cloud Scheduler + Cloud Run Job (same pattern as the Phase 6 judge) reruns
  the live-API build from `docs/data-pipeline.md` Part 2 and writes the
  artifact somewhere the gateway reads at startup — staleness stops being
  possible at all, rather than just less fragile to detect. The better
  production answer; deferred because it's more infrastructure than this
  portfolio project's current scope justifies. Decided 2026-09-17 alongside
  the TMDL-parsing → live-API swap.
- User-forced tool choice (dropdown, `forced_tools` field, ~30–45 min).
- Preset layout modes (2–3 fixed width ratios, ~20 min). Skip drag-to-resize.
- **Replace LangSmith with Cloud Trace** — keeps tool inputs/outputs and real
  `agent_safe` results inside GCP instead of a third-party cloud (component
  reference §3, an accepted trade-off if never built). Scoped to LangSmith's
  role only — `agent_telemetry` is untouched. Optional; the trade-off stands
  if not built. Full plan, verified endpoint/IAM facts, and a real fidelity
  test already run: `docs/cloud-trace-migration.md`.
- `run_projection` — a plain deterministic tool (no LLM or sandbox inside),
  `is_projection` response field, distinct "unverified" card. Default stays
  "decline forecasts outright."
- **A compute tool for genuinely cross-domain derivations** — a difference,
  ratio, or rank comparison between a BigQuery value and a DAX value, which
  no single query can produce since they're separate engines. Deferred: the
  domain split (`.claude/rules/tools.md`) already keeps this rare, and
  same-domain derivations are handled by pushing the computation into the
  query itself (`.claude/rules/orchestrator.md`). Revisit if testing shows
  a bigger need than expected. Full design: `docs/cross-domain-compute.md`.
- **A `search_semantic_model_schema`-style discovery tool on the MCP
  server.** Not needed by this project's own agent — the full table/measure/
  relationship registries already sit in static context
  (`.claude/rules/tools.md`, "Composing DAX"), which is better for latency
  and keeps the agent always aware of relationships than a per-call search
  would be. But `run_dax_query` alone gives a *different* MCP client no way
  to discover a dataset's schema before querying it — noted for the
  "genuinely shareable MCP server" story (2026-09-27 reusability
  discussion), not because this project needs it.
- **Narrate the sensitivity assumption** — have the answer state the
  field-parameter value it used (*"$5M at a 10% adoption assumption"*)
  rather than applying it silently. A bare number reads as more certain
  than a scenario output is. Core behavior already returns the right
  number (`docs/frontend.md`); this is presentation only.
- **DLP image pre-check** — scan uploaded images for PII *before* they reach
  the LLM. Redact everything flagged; on scan failure drop the **image**,
  not the turn. Full design and its honest limits are in the component reference.
- **User-requested assumption override** — let *"what if adoption were 15%
  instead?"* build a query with a different parameter value than the one
  on screen. Genuinely more than a display change: the agent would be
  choosing a value rather than mirroring dashboard state, so it needs its
  own design pass (how the value is validated, how the answer signals it
  diverges from what's displayed).
**`ask_user` relocated to Phase 3 item 12, 2026-10-04** — it depends on the
same `sessions.history_messages`/`build_history_messages()` plumbing item 10
builds, so it's sequenced right after that instead of sitting here as a
deferred idea. See item 12 above, not here.
