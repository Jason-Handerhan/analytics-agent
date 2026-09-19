# Build Order

## Current status — update this as we go

**Phase: 2 complete — Phase 3 in progress (item 1, data prep)**

_Last updated: 2026-09-18._ **Phase 0 (2026-09-13): all nine items verified
live against the real project, complete** — see git history for the full
verification detail if ever needed; kept brief here since it's done, not
current.

**Phase 1, all 7 items done.** Items 1+2 (echo `/ask`, `/conversation` +
ownership check) built and Layer 1 tested (`tests/test_gateway_auth.py`,
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
markup-injection gap a Label would otherwise have sidestepped. Also: gallery
rows are fixed-height (`TemplateSize`), so both message types wrap long
content in a `max-height` + `overflow-y: auto` div rather than growing the
row — and `TextInput1` uses `TextMode.MultiLine` for wrapping, with typed
newlines flattened to spaces before use (Power Apps ties wrapping and
Enter-inserts-newline together; this decouples them).

**Item 7 done.** `telemetry.agent_telemetry` created (location `US`, matching
the other three datasets — confirmed via `bq show`, not assumed) with the
full schema from `.claude/rules/telemetry.md`, including the three nested
`RECORD` fields. `app/telemetry/writer.py` builds a row and writes it via an
awaited `asyncio.to_thread(insert_rows_json, ...)` call — confirmed this is
genuinely not backgrounded (Cloud Run sees the request as in-flight for the
whole duration, same as any other awaited I/O). One Layer 1 test
(`tests/test_telemetry.py`), 10 passing overall.

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

One real deviation from `.claude/rules/mcp-tools.md`: the MCP server runs as
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
breaking the client URL documented in `mcp-tools.md`
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
**`vector_db.chunks_docs_embedded` — in progress, real deviations from the
original design.** Real README source is `context/docs/index.html` (one
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
real build has 66), not deferred. **Not yet confirmed compiled/executed in
BigQuery Studio** — in progress as of this writing.
`context/schema/model_schema.json` — the third data source item 1 needs —
not started.

**Keep this block current.** It's the only place that records where we
actually are — everything below is the static plan. When a phase completes,
update the line above and note anything that turned out differently from the
plan (a step that was skipped, a decision that changed). If you finish a step
and this block is stale, say so rather than guessing what's done.

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
   localhost co-located (`.claude/rules/mcp-tools.md`). **Confirm the exact
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
     the agent's schema grounding, read live from `INFORMATION_SCHEMA`.
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
     rather than retrieved. **Two open gaps, not yet resolved:** a table-count
     mismatch between the two APIs, and no confirmed source for a what-if
     parameter's `range` (`docs/data-pipeline.md`).

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
   5. **`bigquery_schema`** — read once at startup into a module constant,
      not per-request (`.claude/rules/mcp-tools.md`).
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
   tool list (`.claude/rules/orchestrator.md`). `route_entry` and
   `execute_approved` are approval-specific and land with #11.
   **Consume with `astream`, not `ainvoke`, from the start** — status
   updates depend on it, and switching invocation style later means touching
   every call site (`.claude/rules/gateway.md`). Status *strings* land in
   Phase 4; the *plumbing* has to be right now. Standing this up first is
   what lets every tool below be tested end-to-end the moment it's written.
   **`finalize` must call `app/telemetry/writer.py`'s `write_telemetry_row`**
   (built and tested in Phase 1, deliberately left unwired until this node
   exists) — not a new write path, just the first real caller.
5. **`bigquery_schema` MCP resource + TTL cache**, then **`run_bigquery_sql`
   with every guardrail — tool-scoped *and* global — in the same step.**
   Debugging a tool-calling loop against ungated BigQuery is how you generate
   an expensive surprise, and the loop-level caps protect every test run from
   here on, not just this tool:
   - **Tool-scoped:** the dry-run cost gate and three tiers,
     `maximum_bytes_billed`, the 1,000-row cap, the 40s timeout with explicit
     `client.cancel_job()`.
   - **Global, and cheap to build now** (`.claude/rules/orchestrator.md`):
     `max_iterations` with partial-answer handling — the one that stops a
     runaway loop burning tokens — plus the ~90s gateway timeout, the
     answer-length check, and tool-output trimming between turns. All simple;
     none benefits from waiting.

   The schema resource comes first because it's what grounds the SQL.
6. **Verification** (`.claude/rules/orchestrator.md`) —
   `citation_check`, `coverage_check`, `verify_claim_value`, and the
   honest-decline path. Global rather than tool-scoped, but it exists
   *because* of numeric claims, so it lands as soon as the first tool that
   produces them does.
7. **`run_dax_query` with its guardrails**, plus `get_measure_dax`
   (`.claude/rules/mcp-tools.md`). Grounding is already structural — the
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
   to `agent_safe` (`docs/data-pipeline.md`). Then `get_page_info`, then
   `list_repo_files` / `read_repo_file` with the `github-read-token` secret
   and the hand-written `FILE_DESCRIPTIONS` map (`docs/code-search.md`). The
   code tools are genuinely least critical — build them last.
10. **Firestore conversation state** (`.claude/rules/gateway.md`) — the
   `live_turns` document (status, cancel flag, pending approval) and the
   `sessions` document (last 5 turns with their queries and filter context,
   `user_id`, `last_activity_at`).
   **Not an optimization — an in-memory dict breaks under Cloud Run's
   ordinary multi-instance scaling.** Also wire short-term chat history into
   the prompt here.
11. **The approval workflow** (`docs/approval-workflow.md`) — the pause,
   `PendingApproval` caching, `POST /ask/respond`, `route_entry` and
   `execute_approved`, and the hard-decline tier. Depends on #5's cost tiers
   and #10's Firestore state. The UI half (approve/reject buttons) lands in
   Phase 4. **Verify it here anyway, via curl/script against
   `/ask/respond`** — same pattern as Phase 0's `executeQueries` smoke test.
   The resume logic shouldn't sit a whole phase untested just because its
   buttons don't exist yet.
12. **User cancellation** — `POST /ask/cancel/{conversation_id}` writing the
   `cancel_requested` flag, and the node-level checks that route to
   `finalize`. The per-tool cancellation mechanism already shipped with
   `run_bigquery_sql` in #5; this is the turn-level path on top of it. The
   Power Apps button lands in Phase 4.

## Phase 4 — Multimodal grounding & response formatting

1. Screenshot upload through the connector (filters, slicers, and active page
   already landed in Phase 3). The image is layout/attention only — **never** a source of
   numeric values.
2. Response formatting: HTML, source badges, approval cards (incl. the
   approve/reject buttons for Phase 3's workflow), chart display.
3. **The cancel button** — a Power Apps button firing
   `POST /ask/cancel/{conversation_id}` (`docs/frontend.md`). The endpoint and
   cancellation path shipped in Phase 3; this is the UI half, alongside the
   approve/reject buttons above.
4. Progress indication, sample questions, follow-up chips.
5. The remaining `generate_chart` spec types beyond Phase 3's three
   (`docs/chart-tool.md`) — add as real questions call for them.

## Phase 5 — Skipped

XMLA is a documented backup only. REST is the plan.

## Phase 6 — Evaluation & polish

1. Judge Cloud Run Job + Cloud Scheduler (`docs/llm-judge.md`) — consumes
   the `agent_telemetry` table already logging since Phase 1.
2. Golden dataset regression suite (`docs/golden-dataset.md`) — a separate,
   event-triggered mechanism, **not** a subset of #1. Mostly hand-curation;
   Claude Code's part is `scripts/run_golden_tests.py`, the comparison
   runner.
3. CI/CD workflow: `github-deployer`, WIF, and the repo variables are already
   done (Phase 0 item 9) — this is just writing
   `.github/workflows/ci.yml` with a `test` job and a `deploy` job gated by
   `needs: test`. **The deploy job is two steps** — build a
   `${{ github.sha }}`-tagged image, then deploy *that image*
   (`docs/ci-cd.md`), not the `--source .` command used manually through
   Phases 1–5. Its `auth@v2` step uses `workload_identity_provider`/
   `service_account`, not `credentials_json` — no key exists to use.
4. Share the Power Apps app — the one auth step that's neither code nor IAM.
5. Demo video + business-first README.

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
