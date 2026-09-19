# Conversational Analytics Agent

A governed conversational analytics agent embedded beside a Power BI dashboard.
Power Apps chat → FastAPI gateway (Cloud Run) → LangGraph tool-calling loop over
MCP tools (BigQuery, Power BI DAX, docs/code search, measure lookup, charting) →
deterministic verification that every number traces to a live tool call →
grounded answer.

**The three things that make this non-trivial. Don't let them erode:**
1. Numbers come only from live tool calls this turn — enforced by deterministic
   verification, never by prompting.
2. The agent can't reach data it shouldn't — enforced by IAM on `agent_safe`,
   never by prompting.
3. Cost, latency, and result size are bounded — dry-run, byte caps, row caps,
   iteration caps, answer-length checks, timeouts with real job cancellation.

---

## How we work — read before writing any code

This is a **collaborative build and a learning exercise.** I'm building this to
understand it, not just to have it exist.

**Session start: check `docs/build-order.md`'s status block** before proposing
next steps; say so if it looks stale. **When a step or phase finishes, update
that block yourself in the same turn** — don't wait to be asked. Nothing tracks
progress automatically.

- **One step at a time, then stop.** A step is ~one file or one coherent
  function — not a phase, not a subsystem. Check in before the next; don't
  chain them.
- **Don't write the whole thing and hand it over.** Asked for the verification
  module? Write that and its tests — not the graph that calls it, or the next
  three modules. Scope is *one unit of work*, not *one file*.
- **Tests ship with the code, not in a later pass.** A step isn't done until
  its Layer 1 tests exist and pass (`docs/testing.md`). If something can't be
  tested without credentials, say so — that's Layer 2, run manually, not a
  reason to skip it silently.
- **Explain non-obvious choices briefly as you go** — a LangGraph pattern, an
  async construct, a Pydantic behavior. The explanation is part of the
  deliverable.
- **Doc and rule edits: concise, not exhaustive.** State the decision, and
  only if it's genuinely non-obvious, one line of why. Skip restating
  context already established elsewhere, hedging, or narrating the fix.
- **I write and modify code too.** Re-read files before editing.
- **Correct me directly when I'm wrong**, not quietly around it.
- **Offer to let me try first** on self-contained pieces.

## Code examples in these docs are a starting point, not a spec

Much of it was written against docs rather than a running environment.

- **If it doesn't run, fix it** — don't contort surrounding code to preserve a
  snippet as written.
- **Know a better approach? Say so before implementing**, in a sentence.
- **If reality contradicts a doc, flag it** — a silent workaround leaves the
  doc wrong for next time.
- **Not flexible:** the verification contract, the `agent_safe` boundary, and
  the auth model — decisions, not suggestions. Changing how they *work* is
  a conversation; changing the code implementing them is fine.

## Scope discipline

- Build only what the step asks; flag if it needs a later phase.
- Don't refactor, rename, or clean up outside the current task without asking.
- Disagree with a documented design? Say so before implementing, not instead.

## When you're unsure

- **Missing context** (a table/measure name, a schema field) → **ask**. A
  plausible-looking wrong name is worse than a `TODO`.
- **Ambiguous requirement** → state your reading, proceed, flag it.
- **Multiple valid approaches** → name the trade-off, pick one, move on.

## Code standards

- Python 3.12, type hints on signatures, Pydantic for anything crossing a
  boundary (API, tool call, response).
- `async def` for all I/O (BigQuery, Power BI, LLM, MCP).
- Errors must be actionable — the agent reads tool errors and retries from
  them. `"Field 'x' not in result. Available: [a, b, c]"` is useful;
  `"invalid input"` is not.
- No secrets in code. `get_secret()` from Secret Manager — which holds the
  Microsoft credentials too; there is no Azure Key Vault here.
- No hardcoded project/dataset/table names outside `app/config.py`.
- New code goes in the module owning that concern. If it fits nowhere, ask
  rather than creating a new top-level directory.
- **Concise over defensive.** Don't guard against things Pydantic/the type
  system already rules out, add a try/except with nothing useful to do in
  the except, or write a comment that restates the code. This does **not**
  apply to the guardrails this project deliberately specifies (row caps,
  cost tiers, actionable tool errors, verification) — those are required,
  not defensive bloat. If unsure which side something's on, ask.

---

## Where the details live

**1. Path-scoped rules load automatically** when you touch a matching path —
you don't need to open these:

| Rule | Attaches when editing |
|---|---|
| `orchestrator.md` | `app/orchestrator/**`, `app/exceptions.py`, verification + guardrail tests |
| `gateway.md` | `app/gateway/**`, `app/config.py`, gateway tests |
| `mcp-tools.md` | `app/mcp_server/**`, tool + row-cap tests |
| `telemetry.md` | `app/telemetry/**`, the judge + golden-runner scripts |
| `data-pipeline.md` | `definitions/**`, `scripts/build_vector_db.py`, `scripts/build_model_context.py` |

**2. These do NOT load automatically — open them yourself** before working in
that area, rather than reconstructing the design from memory:

| File | Read when |
|---|---|
| `docs/testing.md` | Writing any test, or deciding if something is testable |
| `docs/data-pipeline.md` | All three pipelines: `agent_safe`, the model-schema artifact, the docs vector index |
| `docs/chart-tool.md` | Anything touching `generate_chart` or chart specs |
| `docs/code-search.md` | The GitHub code tools — why agentic, not vector |
| `docs/approval-workflow.md` | Cost tiers, the approval pause, `/ask/respond` |
| `docs/auth.md` | Any identity boundary — the request-time chain, connector OAuth, service accounts |
| `docs/ci-cd.md` | The GitHub Actions workflow, deploy gate, `github-deployer` |
| `docs/frontend.md` | Power Apps controls, filter capture, response rendering |
| `docs/llm-judge.md` | The scheduled eval job scoring faithfulness |
| `docs/golden-dataset.md` | Hand-verified correctness regression tests |
| `docs/success-criteria.md` | What proves a phase works — read before building, not just at the end |
| `docs/build-order.md` | What phase we're in and what's next |
| `docs/cloud-trace-migration.md` | Phase 7's LangSmith → Cloud Trace swap — read before building it |
| `conversational-analytics-platform-COMPONENT-REFERENCE.md` | The *why* behind any decision; full design reasoning |
| `local-dev-environment-setup.md` | Environment, credentials, GCP/Entra setup |

Each rule points to its deep-dive doc — follow it when the rule is thinner
than the task needs.

---

## Ask me — don't decide alone

- Which results tables get excluded from `agent_safe`.
- Real measure/table names from the live semantic model — documented DAX
  examples use placeholders.
- Anything that changes the verification contract.
- **The `extract_numeric_tokens` rule** — year/version/index vs. real value.
  Propose a rule with accept/reject examples; confirm before locking a test.
- Whether Instacart bronze already has `products`/`aisles`/`departments` with
  id→name mappings — check before assuming new ingestion is needed.
- **The three cost thresholds**: `BIG_QUERY_THRESHOLD` (per *batch*, not per
  query — the summed dry-run bytes of one dispatch), `ABSOLUTE_CAP` (the
  turn's cumulative `bytes_consumed`, and also `maximum_bytes_billed` on
  every job), and the per-TB display rate. Decisions in bytes; dollars
  display only.
- Adding a dependency not in `pyproject.toml`.
- Adding a new top-level directory or module.
- Anything making a number reachable without a live tool call — including
  `run_projection` or `is_projection: true`. The build declines forecasts.
- Switching chart delivery from the public bucket to signed URLs — that's a
  documented trade-off, not an oversight.

## Known-unverified — flag if you depend on these

- Power Platform's sync-call ceiling (~120s for Power Automate; unconfirmed for
  a direct canvas-app connector call).
- Whether aborting an `executeQueries` REST connection stops the server-side
  query (no confirmed REST equivalent to `cancel_job`).
- Which HTML tags the Power Apps HTML text control renders reliably.
  **Confirmed it does NOT render `<iframe>`** (tested directly, even with a
  plain non-Power-BI URL — `docs/frontend.md`); still open for the actual
  answer-rendering tags this control needs to handle (headers, lists, code
  blocks, tables).
- Whether `CUSTOMDATA()`/`customData` ever ships on `executeQueries` — absent
  today (Premium/Fabric-only API). Not needed: the model writes the DAX.
- Prompt-caching minimums are **model-specific and move** — Sonnet is 1,024
  tokens, newer Opus/Haiku 4,096. Re-check on any model change (`.claude/rules/gateway.md`).
- **LangGraph** — whether breaking out of `astream` propagates cancellation
  into a running node or only at the next node boundary, and the custom
  reducer replacing `add_messages` for `TimestampedMessage`. (Confirmed and
  already applied: `Command`'s shape, and the `astream` `StreamPart` format
  with `stream_mode="updates", version="v2"`.)
- **`ML.GENERATE_EMBEDDING` vs the newer `AI.GENERATE_EMBEDDING`** — Google's
  docs now show both. Check which is current before building the pipeline,
  and whether the built-in `embeddinggemma-300m` model fits: it keeps data in
  BigQuery with no Vertex AI call and no Vertex AI charges, unlike an
  endpoint-based model such as `text-embedding-005`.

---

## Commands

```bash
# Local dev — uv run finds .venv automatically, no activation needed
uv run uvicorn app.gateway.gateway:app --reload --port 8000

# Tests — Layer 1 only, no credentials needed. A test needing real
# credentials is Layer 2: run it manually, don't put it here.
uv run pytest tests/ -v
uv run pytest tests/test_guardrails.py -v
uv run pytest -k "guardrail" -v

# Add a dependency (updates pyproject.toml + uv.lock together)
uv add some-package
uv add --dev some-dev-only-package

# Manual/local deploy only — creates the Cloud Run service on first run, or
# for a quick ad-hoc test. NOT how production updates after CI/CD exists —
# that path builds a tagged image first (`docs/ci-cd.md`); running
# this against the live service bypasses that and leaves an untagged,
# unreproducible build in place.
gcloud run deploy analytics-gateway \
  --source . \
  --service-account=agent-sa@YOUR_PROJECT.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --region=YOUR_REGION
```

**No `--set-env-vars`, no `--set-secrets`.** Every non-secret config value
(`GCP_PROJECT_ID`, `TENANT_ID`, `EXPECTED_AUDIENCE`, `GCS_CHART_BUCKET`,
`POWER_BI_DATASET_ID`, `POWER_BI_WORKSPACE_ID`, `LANGSMITH_TRACING`,
`LANGCHAIN_CALLBACKS_BACKGROUND`) is a **hardcoded literal in
`app/config.py`**, not read from an env var — confirmed live (2026-09-16),
this deploy command. `LANGSMITH_API_KEY` is fetched via `get_secret()` at
startup exactly like every other secret — no Cloud-Run-native secret
mounting needed for it specifically. One remaining wrinkle: the LangSmith SDK
itself only reads its three settings off `os.environ`, so app startup pushes
them there once, before any LangChain/LangGraph import
(`local-dev-environment-setup.md` Step 17).

**Deployed gateway URL:** `https://analytics-gateway-551802026956.us-central1.run.app`
— not app config, `config.py` has no reason to know its own address; this is
purely for the Power Apps connector's Host setting and manual curl tests.
Stable across redeploys (Cloud Run URLs are per-service, not per-revision).

**Config values I provide — never invent these:** `GCP_PROJECT_ID`,
`TENANT_ID`, `EXPECTED_AUDIENCE`, `GCS_CHART_BUCKET`, `POWER_BI_DATASET_ID`,
`POWER_BI_WORKSPACE_ID`. All non-secret, all hardcoded literals in
`app/config.py` (not env vars — see Commands above). `GCP_REGION` doesn't
exist there today; it's only used as the `--region` deploy flag, never read
by app code — flag it if something in a later phase (e.g. the Vertex AI
connection) actually needs it as a config constant rather than a deploy-time
value. Secrets live in Secret Manager: `power-bi-sp-client-id`,
`power-bi-sp-client-secret`, `azure-tenant-id`, `entra-client-secret`,
`gateway-api-key`, `github-read-token`, `anthropic-api-key`,
`langsmith-api-key`, `gemini-api-key`, `openai-api-key`.
