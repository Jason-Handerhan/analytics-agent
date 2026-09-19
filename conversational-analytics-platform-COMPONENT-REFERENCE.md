# Conversational Analytics Platform — Component Build Reference

A condensed, requirements-doc-style companion to the phase-ordered build guide.
Organized by **build component**, not by phase — useful when you're heads-down
on one part of the system and want everything relevant to it in one place.
Each component section includes: what it must do, the key decisions and why,
licensing, trade-offs, and its build steps.

**This is the authoritative design document.** Its companions:
`CLAUDE.md` (the working contract for building with Claude Code),
`.claude/rules/*.md` (path-scoped rules that auto-attach while coding),
`docs/*.md` (implementation detail, read on demand), and
`local-dev-environment-setup.md` (machine setup). Where any of those
disagree with this document on *why* a decision was made, this one is
correct.

---

## 1. Project Summary

A conversational analytics agent embedded in a Power BI dashboard, built as
the AI-engineering piece of a broader portfolio and as practice for a
near-identical system being co-developed with a data science team at FedEx.
Business users ask natural-language questions from inside a Power Apps chat
panel next to a live Power BI report; a LangGraph agent runs a governed
tool-calling loop — querying BigQuery, the Power BI semantic model, project
documentation, and code, in whatever combination a question needs — verifies
every numeric claim traces to a live tool result rather than static docs, and
returns a grounded, auditable answer. The Instacart dashboard is a stand-in
for the real pattern: a governed, cost-controlled, model-swappable agent
answering questions a dashboard's visuals don't directly answer, without
generating a ticket to the data team.

---

## 2. Architecture Overview

```
Power Apps canvas app
 ├─ Embedded Power BI report
 ├─ Chat panel (TextInput + Gallery)
 └─ On send: optional uploaded screenshot (base64) + filter context
        │  custom connector (Entra ID / OAuth2) → POST /ask
        ▼
FastAPI gateway · Cloud Run
 authN/authZ · request validation · rate limiting
 assembles the multimodal prompt (question + image + filter context + user id)
        ▼
LangGraph orchestrator (StateGraph, typed state)
 [agent node: sees all tools, calls what it needs, loops until done]
 [verify: citation check + coverage check]
 [assemble AgentResponse]
        │  MCP (MultiServerMCPClient, streamable_http → localhost)
        ▼
MCP tool server · FastMCP
 run_dax_query · get_measure_dax · run_bigquery_sql · list_repo_files ·
 read_repo_file · search_docs · get_page_info · generate_chart ·
 bigquery_schema (resource)
        ▼
Data + governance layer
 BigQuery (gold tables + 6 vector tables) · Power BI semantic model ·
 GCS (charts) · async telemetry → BigQuery + LLM-judge eval
```

**Why not native Power BI Copilot:** not approved for use at FedEx — the real,
primary reason. Also structurally couldn't do this even if approved: no
tool-calling surface outside the semantic model (no BigQuery, no docs, no
custom orchestration), Microsoft's own docs admit non-deterministic outputs
from the same prompt (the exact failure mode this project's verification step
prevents deterministically), and it requires PPU or Fabric F64+ capacity
(~$5K+/month) — a bigger cost floor than this design.

---

## 3. Pragmatic Trade-offs

State each as "in production I'd do X; for this build I did Y because Z."

| Area | Enterprise approach | Portfolio approach |
|---|---|---|
| Compute scale | Autoscaling, multi-instance | Single Cloud Run instance, scale-to-zero. Cold-start cost mitigated with a warm-up ping before a demo, not paid min-instances |
| Model hosting | Dedicated/private | Pay-per-call hosted models; MCP makes the model swappable anyway |
| Caching | Managed cache tier (Redis) | In-memory / cheap store — proves the pattern, not the infra bill |
| CI/CD | Full test + SQL-compile + gated deploy | Representative subset |
| Secrets | Secret Manager + auto-rotation | Secret Manager **is built**; rotation is the stated gap |
| Observability | Distributed tracing + alerting | Telemetry → BigQuery + LLM-judge eval |
| Agent tracing | **Cloud Trace** (GCP-native, nothing leaves the boundary), or the managed **Agent Engine** runtime inside the **Gemini Enterprise Agent Platform** — Google's Cloud Next 2026 rebrand of Vertex AI — which traces at the runtime level | **LangSmith, left on in production.** The trace tree is the clearest view of the tool-calling loop actually working, and being able to show it is worth more here than the data-residency purity a real deployment needs. Accepted cost: tool inputs and outputs — real `agent_safe` results — reach a third-party cloud, through a path the IAM design doesn't cover. `LANGCHAIN_CALLBACKS_BACKGROUND=false` is still required either way; that one's a correctness bug, not a preference |
| Data freshness | Orchestrated ingestion (Composer/Airflow) | Static loads reused from Instacart |
| Vector index refresh | CI-triggered on merge | Manual/on-demand |
| Infrastructure as code | Terraform | `bq`/`gcloud` CLI + console, documented — same choice as the Instacart ML project |
| Power Apps licensing | Org-wide Premium seats | Free Developer Plan for solo build/test |
| Power BI licensing | Fabric/Premium capacity | PPU, one seat, only if XMLA is ever pulled off the shelf as a backup (not planned — see §8) |
| Per-user data access | Per-user RLS/authorization | Single shared service account; access gated at the dashboard level (`.claude/rules/gateway.md`) |
| Chart image delivery | Private bucket + signed URLs with expiry | **Public bucket**, unguessable `uuid4()` filenames — anyone with the URL can view it permanently. Fine for public Kaggle data; not for real operational data (`.claude/rules/mcp-tools.md`) |
| Frontend platform | React (or equivalent SPA) + `powerbi-client-react`, calling this same gateway unchanged | **Power Apps** — deliberate despite a real scaling ceiling: custom connectors are always premium, no exception (~$20/user/month; the cheaper per-app tier ended Jan 2026). Accepted because it's fully built and the primary target roles evaluate this surface directly. React was seriously evaluated — interactivity, filter capture, and auth all verified workable — and is planned as **Phase 2** of this project (§6) — a future rebuild, not an open gap |
| Uploaded-image content | DLP pre-check: OCR for PII, redact flagged regions before the LLM sees the image | **No scanning.** Internal users attaching dashboard screenshots; the provider's own baseline safety classification is the only filter. Real gap in a production/public deployment — designed and deferred to Phase 7 (§6) |
| Uploaded image retention | **Whether to retain at all, and for how long, is a governance/audit policy decision, not an engineering one** — implementation follows from that: redacted image (never the raw original), private signed-URL bucket, tiered Standard→Archive, correlated via a new `uploaded_image_uri` telemetry field | **Not built** — out of scope for a portfolio project |
| Resume-across-deploy correctness | `code_version` on `PendingApproval` — detect a mismatch and decline the resume with a clear message, rather than run it against possibly-incompatible logic | **No check at all.** A pause sits open for minutes, not days; deploys are solo and infrequent, not a multi-team system where someone else might ship mid-approval unannounced. **Detection isn't correction anyway** — comparing version strings only tells you a mismatch happened, it doesn't reconcile changed field semantics. Actually fixing it means routing the resume to the *old* container image (the git-SHA-tagged image from CI, `docs/ci-cd.md`, is what makes that image addressable at all) — real versioned-routing infrastructure, the same category of complexity already declined for the checkpointer itself (§9.2a). Given the narrow exposure window, accepted as-is rather than built around |
| Approval pause/resume | LangGraph `interrupt()` + `Command(resume=...)` with a persistent checkpointer — the canonical human-in-the-loop pattern | **`PendingApproval` in Firestore** — a minimal checkpointer scoped to the one point that pauses. Resume-from-anywhere matters for long-running agents where losing position is expensive; a turn here is seconds long, pauses in exactly one place, and — like most chat products — nothing is expected to survive leaving the app. Twelve known fields resume the one case that exists (`.claude/rules/orchestrator.md`). **The canonical path was evaluated in depth and is a real upgrade, not a rejected idea** — see §9.2a for the specific package, why vendor it, and what it would retire |

| Power BI query quota | **On-behalf-of token exchange** — swap the user's Entra token for a per-user Power BI token, so each user gets their own quota | **One shared service principal.** `executeQueries` allows 120 requests/minute *per user*, and every query here runs as the same SP — so all users share one bucket. OBO would mean per-user consent, per-user token caching, and a Power BI license for every user: real work for a ceiling this project won't approach. Revisit only under genuine concurrent load |
| Abandoned approvals | Scheduled sweep logging an `incomplete` outcome | **Findable without a sweep** — a pause row with no matching response row *is* the record (`.claude/rules/telemetry.md`). Only the timing of abandonment is unrecorded, bounded by the pause timestamp and the conversation TTL |
| Conversation state (status, approvals, chat history, cancel) | Firestore — shared across Cloud Run instances | **Same: Firestore.** Not a portfolio shortcut — an in-memory dict genuinely breaks under Cloud Run's ordinary multi-instance scaling, and Firestore needs no VPC connector and costs ~$0 at this scale (`docs/frontend.md`) |

**Kept at enterprise level, non-negotiable:** LangGraph governed orchestration,
MCP tool decoupling, cost-guard + human-approval flow, the no-numbers-from-docs
rule enforced by verification, restricted table access via a curated dataset,
visual grounding, the evaluation loop.

---

## 6. Frontend — what was considered and deferred

Build spec — controls, filter capture, response rendering — is in
`docs/frontend.md`. What stays here is the reasoning behind what that
frontend *doesn't* do, and the platform choice itself.

### Considered, deferred: scanning uploads for PII before the LLM sees them

**The risk this addresses is real in a production deployment**, even though
it's out of scope here: a user attaches something they shouldn't — not a
dashboard screenshot but a photo of a document, a form, an ID. Deferred to
Phase 7, with the design worked out so it isn't re-derived later.

**This is a different concern from "the screenshot is layout/attention
only, never a source of numeric values" — don't assume that note covers
it.** That constraint governs *what the image can influence in an answer*
(a correctness guarantee). This governs *whether the image's bytes leave
our infrastructure to a third-party model provider at all* (a privacy
one). An image can be completely irrelevant to the numeric answer and
still be a serious data-exposure problem. **Same underlying question as
why LangSmith is dev-only (`local-dev-environment-setup.md` Step 17)** — does this data need to leave the
boundary, and was that decided deliberately — applied to a different data
type.

**Constraining the input mechanism doesn't work here — recorded so it
isn't re-explored.** The intuitive fix is "capture the embedded report
instead of accepting arbitrary uploads," which would prevent the problem
by construction. It runs into a real wall: embedded Power BI content is a
**cross-origin iframe**, and the Same-Origin Policy deliberately blocks
reading its pixels from the host page — the same protection that stops a
page screen-scraping an embedded bank login. Two paths remain, both
expensive: the browser's `getDisplayMedia` (works, but forces a permission
prompt *every* use, needs a custom PCF component, and behaves differently
on the native mobile player), or Power BI's own export APIs (sidesteps
cross-origin cleanly, but it's unconfirmed whether they capture the user's
*current interactive state* rather than a static page — if the latter,
they may not serve the purpose at all). **A standard file upload plus
detection is the practical answer**, which makes detection the *primary*
defense rather than a backstop.

**Mechanism: Cloud DLP (Sensitive Data Protection), already GCP-native.**
It OCRs text out of an image, matches it against built-in infoTypes
(SSN, credit card, email, person name, 150+ others), and either reports
findings or returns a **new image with the matches covered by opaque
rectangles** — synchronously, PNG/JPEG/BMP.

**Its limitation is structural, not a tuning problem — state it plainly
rather than implying the gap away.** It is **not** a general "is this
image sensitive" classifier. It finds *legible text*. A face with no
text in frame, a physical object, a whiteboard diagram, a document
photographed at an angle bad enough to defeat OCR — all pass through
undetected. With detection as the primary defense, that residual is the
standing accepted risk, and no second layer quietly closes it. Beyond
this, it becomes a policy problem too (acceptable-use terms, audit
logging of what was uploaded so a slip is at least detectable after the
fact), not purely a technical one.

**Cost is not a factor.** Billing is per GB inspected, not per call — a
screenshot is a few hundred KB, so a single scan lands sub-cent, and
portfolio-level volume plausibly never leaves the free tier. Confirm
current rates rather than trusting this figure.

**Where the actual work is — decisions, not code.** The call itself is one
client-library invocation with the same ADC auth as every other GCP client
here. Scope is comparable to the row-cap guardrails (`.claude/rules/orchestrator.md`), not to the
approval workflow. Two decisions are already made above (redact-everything;
drop-image-not-turn); what remains is which infoTypes to check for, and
Layer 2 testing against real or synthetic-PII images, since mocked unit
tests can't validate it.

**Persistence:** don't force a re-upload every turn. Store the attached image
in app state, show a "Screenshot attached ✓ (tap to replace)" indicator, and
let it carry across follow-ups. Most questions should work with **no image at
all** — that's the normal case, not an edge case.

**Grounding rule:** the image is for layout/attention only, **never** a source
of numeric values — the filter-context block is authoritative for state. This
is what makes the automatic-capture-to-manual-upload pivot a small change: the
image was never load-bearing for correctness.

### 6.4 Frontend platform — Power Apps now, React as a future rebuild

**Power Apps has a hard scaling ceiling, and it's a licensing one, not a
technical one.** Calling this gateway requires a custom connector, and
**custom connectors are always premium — no exceptions**, regardless of how
simple the API is. That makes every user of the app require Power Apps
Premium, **~$20/user/month** (the cheaper per-app tier was discontinued in
January 2026, so that's the floor). For a solo portfolio build that's one
license; for a real multi-user deployment it's a per-seat tax on every viewer.

**Why it's still the right choice here.** It's fully designed and built, and
the primary target roles (senior data analyst) evaluate this surface directly
— a working Power Apps + Power BI integration is a stronger signal for those
roles than a React app. The scaling ceiling is real but doesn't bind at
portfolio scale.

**React is the production answer, and it was seriously evaluated — not
assumed.** The relevant pieces were each checked rather than hand-waved:

| Concern | Finding |
|---|---|
| Report interactivity when embedded | Full parity — slicers, cross-filtering, drill-through all work natively. *Persistent filters* and *personal bookmarks* even carry state across Power BI Service, mobile, and embedded |
| Filter/field-parameter capture | Real documented API: `report.getActivePage()` → `page.getSlicers()` → `slicer.getSlicerState()`. Field-parameter sliders are just slicers on a synthetic table, so they need no special handling |
| Auth | Standard MSAL.js Authorization Code + PKCE. **The gateway needs zero changes** — `validate_entra_token` checks signature/issuer/audience/expiry and has no idea which flow produced the token |
| Cost | *Cheaper* than today: React removes the ~$20/user/month Power Apps license entirely. The Power BI viewing license is unchanged either way |

**Why React over Streamlit specifically.** Streamlit was a real candidate —
Python-native, purpose-built chat primitives (`st.chat_message`), and
`st.fragment(run_every=...)` handles the status polling cleanly. It loses on
the two pieces that matter most here:
- **Embedding** — no native component, so it drops to raw JS inside
  `st.components.v1.html()`. React has a first-party `powerbi-client-react`.
- **Filter capture — the decisive one.** `st.components.v1.html()` is
  *one-way*; reading slicer state back into Python needs Streamlit's
  bidirectional custom-component framework, which carries its own build step
  — ironic, since avoiding JS tooling was Streamlit's main draw. React has no
  boundary to cross: `getEmbeddedComponent` hands back a live object.

**Planned as a future rebuild of this same project, not a separate one.**
**Deliberately unnumbered** — "Phase 2" already means the first-real-tool
phase in the build order, and reusing that label here would collide. The
backend is a REST API with no knowledge of its caller
(`.claude/rules/gateway.md`), so the rebuild reuses the gateway,
orchestrator, tools, guardrails, and Firestore state **unchanged** — it is
strictly a frontend swap. Two things follow:
- The repo would grow a `frontend/` directory alongside `app/` and
  `definitions/`, each with its own tooling — the same separation-by-directory
  pattern already in use. A new frontend rule file scoped to `frontend/**`
  would slot into the existing path-scoped rule mechanism with no new
  machinery.
- **React is a static build** (`npm run build`), so it needs static hosting,
  not a second Cloud Run container. Streamlit *would* have needed one, being
  a live Python process.

**This rebuild is also the natural point to adopt the checkpointer upgrade
(§9.2a).** Removing Power Apps frees the premium-connector cost that makes
Cloud SQL affordable, and removes the reason polling exists — so SSE and
`interrupt()`'s in-place resume become the better fit at the same moment.

---

## 8. Backend services — platform choices and the XMLA backup

Build spec is in the rules: `.claude/rules/gateway.md` (endpoints, auth,
timeout, Markdown→HTML) and `.claude/rules/mcp-tools.md` (`executeQueries`,
response shape, quota, the DAX-vs-BigQuery split). What stays here is why the
platform and layering are what they are, and the fallback that was designed
but not built.

**Why Cloud Run, not Vertex AI Agent Platform Runtime** (Google's managed
agent runtime, formerly Agent Engine — a real option, weighed and declined):
Power Apps' Entra auth doesn't
bridge to its GCP-IAM-native surface without a proxy anyway; would mean
re-deriving already-built gateway control in its idioms; adds new per-event
billing for state already tracked for free; reduces the "I designed X"
portfolio story; and it's mid-rebrand as of 2026 (now under the Gemini
Enterprise Agent Platform).

**Layering rationale (why gateway → orchestrator → MCP server, not another
order):** MCP is a client-server protocol — the orchestrator is the client
(decides to call tools), the MCP server exposes tools (doesn't decide). This
order matches: HTTP/auth boundary outermost, the reasoning loop next (must sit
above tools since it does the deciding and looping), tools as leaf operations.
The reverse (gateway → MCP server → orchestrator) inverts client/server and
gives the middle box no coherent job.

### The DAX path — REST only, XMLA documented as a backup

**Status: REST is the plan, full stop — not a contingent-but-expected
upgrade.** XMLA is documented below as a fully-designed fallback, kept ready
in case some unknown future reason makes it necessary, but it's not a planned
build step (Phase 5 is skipped). Rationale: the Execute Queries REST API's
120 req/min per-user rate cap is effectively unreachable for a conversational
agent (each turn takes 5–15s plus human think time), and XMLA's remaining
real advantages (very large result sets, DMV/MDX metadata access) don't apply
to this use case.

**License: Pro is sufficient for REST.** PPU ($24/user/month, one seat) would
only be needed if XMLA is ever actually pulled off the shelf — includes XMLA
endpoint access, no Premium/Fabric capacity required. Per-user, not
capacity-based, so there's nothing to size or tear down on a meter.

**Option B — XMLA endpoint (backup only, not planned — build only if some future need arises):**
- Pros: broadest capability, unthrottled connection, optimized binary
  protocol, full metadata access. Also supports genuine server-side query
  cancellation (`AdomdCommand.Cancel()`) — unlike the REST path, where
  cancellation is unconfirmed (`.claude/rules/orchestrator.md`, §11). A minor point in XMLA's favor, not
  on its own a reason to build it.
- **Real cost is plumbing, not the protocol** — XMLA is .NET-native
  (ADOMD.NET). Three Python routes, all with a tax: `pyadomd`/`pythonnet`
  (needs .NET runtime + AS client libs in your container — known Linux
  container pain point); a .NET sidecar/proxy (cleaner, second service/
  language); or do the DAX-executing piece entirely in .NET. This is
  engineering complexity, not a license cost — .NET itself is free.
- **De-risking move:** Power BI Desktop exposes a local XMLA endpoint on
  `localhost`, no license required. Prove the connection code there before
  ever touching the Service endpoint.

**Because the MCP tool signature (`run_dax_query(dax) -> rows`) is identical
either way, the caller never changes — only the tool's internals do.** Wrap
either implementation with DAX validation, a row cap, and audit logging at the
MCP layer.

## 9. Orchestrator — what was evaluated and deferred

Build spec lives in the rules: `.claude/rules/orchestrator.md` (the loop,
verification, guardrails), `.claude/rules/mcp-tools.md` (the eight tools),
and `docs/chart-tool.md` (chart specs). What stays here is what was
considered and not built.

### 9.2a Checkpointer-based resume — evaluated, specified, not built

The approval flow uses `PendingApproval` (`docs/approval-workflow.md`). The canonical LangGraph path
is `interrupt()` + `Command(resume=...)`, which needs a persistent
checkpointer. This is the **specified upgrade path**, not a vague deferral —
enough diligence was done that adopting it later is a scoped change rather
than a redesign.

**What it would retire:** `PendingApproval`'s twelve fields; the carry-vs-reset
rules for `iteration_count`/`bytes_consumed`; the `execute_approved` and
`route_entry` nodes; and the "resume is a fresh graph run" reasoning.
`interrupt()` resumes *in place* — nothing to re-seed, no loop to re-enter.

**Backend: vendor `langgraph-checkpoint-firestore`, don't depend on it.**
Uses Firestore (already in the project), ADC auth (same as every other GCP
client), Native mode (already required), subcollections per thread, sync and
async. MIT-licensed and ~7 KB.

Vendor rather than pin because it sits on the **money path**: 0.x versioning
permits breaking minor releases, it's single-maintainer, and two competing
packages exist. MIT requires only preserving the copyright notice. Copy it to
`app/checkpointer/`, keep the license header, note the source in the README.

**Validate with `langgraph-checkpoint-conformance`** — an official suite with
a `@checkpointer_test` decorator and `validate()` reporting per-capability
pass/fail. That turns vendored code into *verified* vendored code, and is a
better story than either blind dependency or from-scratch implementation.

**Two things to handle:**
- **Exclude the screenshot from checkpoints.** `image_base64` is the only
  genuinely large thing in state. Drop the dedicated field (the gateway can
  build the multimodal `HumanMessage` directly) and use the package's
  `MessageReducer` to prune images before serialization. Firestore's 1 MB
  per-document limit is otherwise not a real constraint here — with the
  1,000-row tool caps, a typical checkpoint is tens of KB.
- **Read `aput_writes` before trusting it.** Pending writes are what make
  resume-after-a-partial-superstep correct, and it's subtler than the
  read/write pair. The sibling package documents that `list()` doesn't
  support metadata filters — irrelevant here, since resume is a direct
  lookup by `thread_id` (latest) or `thread_id` + `checkpoint_id` (exact),
  never a filtered search.

**Why not now:** `PendingApproval` is already designed and understood.
Switching means reading, trusting, and conformance-testing someone else's
code on the most consequential path in the build, plus restructuring
`call_tool` around `interrupt()`. Real work, comparable to what it saves —
worth doing *after* the build works, not instead of starting it.

### `run_projection` (Phase 7, optional) — forecasting, structurally separated from everything else

**The initial build declines these outright (`.claude/rules/orchestrator.md`) — this section is the
optional upgrade to disclosed estimates, not something built by default.**

**Design: neither a sub-agent nor an agent-wrapped-as-tool — a plain
deterministic tool, no LLM call inside it at all.** Once the growth
methodology is bounded (a documented default rate, or a rate the user names
explicitly — never one the model invents), what's left is pure arithmetic:
`base_value * (1 + rate) ** year_offset`. That doesn't need a reasoning loop
or a sandbox — a one-line deterministic function, same shape as
`generate_chart`. (Correction from an earlier draft, which framed this as
needing provider code execution — more machinery than a bounded, already-
decided methodology actually requires.) A richer set of projection styles
later (linear / compound / seasonal-naive) is still just a small, fixed set
of classes keyed by type — the same `ChartSpecBase`-style polymorphic pattern
above (each type owns its own `required_fields()`/`render()`-equivalent
methods), never open-ended code.

**This directly resolves the performance concern, rather than trading it
off:** a sub-agent or an internal LLM call both add real round-trips for a
computation that has exactly one right answer once inputs are fixed. A plain
deterministic tool costs exactly what any other tool call already costs —
one call from the orchestrator to invoke it, nothing internal.

**Natural extension, not yet designed:** the same ABC-based, one-class-per-
type pattern used for chart specs generalizes cleanly to other projected
quantities (order growth, not just financial impact) — one class + one
deterministic growth method per type, same enforcement benefit
(`@abstractmethod` won't let a new projection type forget its own
methodology). Worth revisiting once `run_projection` is actually being
built, not before.

**Why a caveat sentence on a normal answer isn't enough here specifically:**
every other number this agent produces passed deterministic verification —
that's the entire point, so users don't have to independently check it. A
disclaimed, non-deterministic forecast sitting in the *same visual style* as
verified answers asks a user to reliably tell them apart under time pressure
— exactly the failure mode already flagged as a reason to avoid native Power
BI Copilot ("can return different results from the same prompt"). The fix
has to be structural, not textual — reusing patterns already built, not new
ones:

- **`is_projection: bool` — added to `AgentResponse` *with this feature*, not
  carried in the schema before it exists** —
  explicitly exempt from the citation/coverage checks, not because it slipped
  past them, but because it was never meant to go through them. Everything
  else keeps its "fully verified" guarantee intact.
- **A visually distinct card, reusing the `needs_approval` pattern** — a
  "Projection — AI-generated, unverified" card, structurally different from
  the normal source-badge UI, not a caveat a fast-moving user can skim past.
- **System prompt for this tool specifically:** *"State the growth assumption
  or methodology explicitly. Label it clearly as an unverified AI-generated
  projection. Never present it with the same confidence as verified data."*

---

## 9a. Evaluation — two separate mechanisms, moved to their own docs

Both were extracted from this document so each can be worked on without
loading the other, or this whole file. **They are deliberately not the same
thing, and shouldn't be merged:**

| | `docs/llm-judge.md` | `docs/golden-dataset.md` |
|---|---|---|
| Checks | **Faithfulness** — does the answer match its own cited tool result | **Correctness** — is the number actually right |
| Trigger | Scheduled (nightly Cloud Run Job) | Event-based (prompt/schema/model change) |
| Curation | Automated, judged by an LLM | Hand-verified once, ~20–30 questions |
| Blind spot it has | Can't tell if the *query* was the right one | Doesn't scale past its fixed question set |

The gap between them is the point: a subtly wrong SQL query still executes and
returns a real, *faithful* number that answers the wrong question. The judge
would pass it; the golden dataset catches it.

## 9c. CI/CD — what was deliberately not built

Operational detail — the workflow, the quality gate, the two service accounts
— is in `docs/ci-cd.md`. What stays here is the reasoning for what that
workflow *doesn't* do.

### Scaling to a second environment — designed, not built

**Why not built now:** a second environment isn't just a second Cloud Run
service — for it to mean anything, it needs its *own* Firestore database and
its own BigQuery dataset, so a staging test can't silently read or
corrupt production data. That's real, ongoing infrastructure in service of a
problem this project doesn't have: no second person's work to protect
production from, no PR review gate, no one but the one person approving a
promotion. Same category of decision as DLP and rate limiting — fully
designed, deliberately deferred, not a gap.

**What it would take, concretely, if it were ever needed:**
- A `deploy-staging` job added *before* `deploy` in the pipeline, using
  `vars.STAGING_SERVICE_ACCOUNT`/`vars.STAGING_REGION` and its own Cloud Run
  service name (`analytics-gateway-staging`) — deploying the exact same
  image tag `deploy` uses, never a separate build.
- A second Firestore database and a second BigQuery dataset (or a
  clearly-separated schema within one), so staging traffic never touches
  production's `agent_safe` tables or `sessions`/`live_turns` documents.
- A manual approval gate between the two jobs — GitHub Environments'
  built-in required-reviewers feature covers this without custom code.
- The pipeline becomes `build → deploy-staging → (approval) → deploy` —
  staging inserted *before* the environment that already exists, which
  keeps its name and role unchanged rather than being relabeled.

**This is a genuine enterprise-vs-portfolio trade-off worth stating out loud,
in the same shape as the others in §3** — and the migration itself is a good
interview answer: *"I started key-based to learn the mechanics, then moved to
Workload Identity Federation to remove the long-lived credential."*

## 10. Recruiter-Accessibility Plan

- Short demo video/GIF of the chat-in-Power-Apps flow — including a cost-gated
  approval actually triggering, and a "why is this bar high?" question using
  screenshot + filter context.
- Screenshot-heavy README walkthrough, business-framing first.
- Read-only Power BI link if feasible.
- Deep technical detail in the README/architecture doc for those who want it —
  nothing a first-pass reviewer needs requires provisioning anything.
- Structure like the Instacart README: business value first, technical depth
  second. For a translator positioning, leading with business framing *is*
  demonstrating the core skill.

---

## 11. Open Items / To Learn

- Mechanics of BigQuery dry-run + `maximum_bytes_billed`, wired into a
  LangGraph conditional edge.
- Additional query-safety at the MCP tool layer (validation, allow-listing,
  statement-type restriction) — row/scan limits are now designed (`.claude/rules/orchestrator.md`),
  not a placeholder anymore.
- Exact `query_job.result()` parameter name for capping fetched rows —
  designed as `max_results`, confirm against the current client library.
- Whether `executeQueries`' JSON response nests rows exactly as
  `response["results"][0]["tables"][0]["rows"]` — confirm the real shape
  before trusting the row-count check against it.
- Exact Power Apps control/property names for the upload-to-base64 flow.
- Empirically confirm the Power BI service principal's **Contributor** role
  (`docs/auth.md`) actually clears `executeQueries`' "Read + Build" requirement in
  practice — the role is decided, this is verifying it against a real call.
- XMLA plumbing: `pyadomd`/`pythonnet` + AS client libs, or a .NET sidecar —
  prove locally against Power BI Desktop first, if it's ever actually needed.
- Current Vertex AI text-embedding model for `ML.GENERATE_EMBEDDING` — check
  availability at build time.
- Model schema now built from live Power BI, not `.pbip`/TMDL parsing
  (decided 2026-09-17, `docs/data-pipeline.md`) — proven against the real
  dashboard in a notebook, but `scripts/build_model_context.py` itself isn't
  rewritten to this design yet. Two open gaps: a table-count mismatch between
  `executeQueries` and the Scanner API, and no confirmed source for a
  what-if parameter's `range`.
- Which HTML tags the Power Apps HTML text control renders reliably — test
  before locking the system prompt's formatting instruction.
- Whether monospace rendering (inline `font-family` + `white-space:
  pre-wrap`) actually looks right in the real control — mechanism confirmed
  in principle (`docs/frontend.md`), not yet visually. **Covers both `pending_query` and
  agent-written code fences**, since they share the same renderer override;
  verifying one effectively verifies the other.
- Whether the installed FastMCP version preserves `Field(discriminator=...)`
  when a discriminated union is a field on a wrapping model (`.claude/rules/mcp-tools.md`'s
  `GenerateChartArgs`). The wrapping fix is the correct pattern
  regardless; this confirms it's sufficient.
- **Strict-mode / constrained-decoding parameter surface per provider.**
  OpenAI's `strict: true` schema constraints are documented; Gemini's
  equivalent through the LangChain wrapper is not confirmed here. Also
  unverified: whether any current schema needs reshaping to be
  strict-compliant (`.claude/rules/orchestrator.md`).
- Current prompt-caching API specifics for whichever provider(s) end up in
  use — fast-moving area, figures in `.claude/rules/gateway.md` are directional.
- Exact Power Platform synchronous-call timeout for a direct canvas-app
  connector call — the ~120s figure is documented for Power Automate flows
  specifically.
- Confirm whether closing/aborting the Execute Queries REST connection
  actually stops the query executing server-side, or whether it keeps running
  regardless (`.claude/rules/orchestrator.md`) — unlike BigQuery's `cancel_job`, there's no confirmed
  equivalent for the REST path.
- Confirm the Power Apps Developer Plan actually permits custom connectors in
  practice — sources disagree; cheap to verify.
- Real measure/table names from the live semantic model, to replace the
  illustrative DAX few-shot examples in `.claude/rules/gateway.md`.

---

## 12. Interview Soundbites

- "I check correctness two different ways, on purpose. An offline judge
  scores internal consistency — does the answer match its own cited data —
  across all real traffic. A separate golden-dataset suite, triggered by
  prompt/schema changes, checks something the judge can't: whether the
  number is actually right, not just consistent with itself."
- "The agent can call multiple tools per question, not just one — but that
  makes the safety rule *harder*, so it's enforced by an explicit
  verification step: numbers never come from the docs or the screenshot, only
  from live tools this turn."
- "I flagged a real risk during design — capturing an embedded Power BI report
  from Power Apps isn't a first-class operation — before I ever hit it. The
  fix was already designed in: the image was never load-bearing for
  correctness, so switching to manual upload was a small UI change."
- "LangGraph does real work here: a tool-calling loop, a verification step,
  and a cost-gated human-approval edge with a per-turn aggregate budget."
- "The tool-calling loop has a hard iteration cap, not just a retry cap on
  verification failures — one guard that bounds worst-case latency, cost, and
  reliability. A project-level budget alert sits under all of it as a
  last-resort net that doesn't depend on my own code being bug-free."
- "I don't wait for the Power Platform to time this out for me — the gateway
  has its own deadline well under that ceiling, failing into the exact same
  response schema Power Apps already renders, so a graceful timeout needed
  zero new UI code. It also cancels the in-flight query, not just the wait."
- "Power Apps classifies every custom connector as Premium, unconditionally —
  a licensing gate on the very first UI slice, checked in Phase 0 before
  writing UI code, not after."
- "Why not native Power BI Copilot? Beyond it not being approved at FedEx —
  Copilot has no tool-calling surface outside the semantic model, and
  Microsoft's own docs admit it can return different results from the same
  prompt, which is exactly the failure mode my verification step prevents
  deterministically."
- "The Execute Queries REST API is capable at essentially any realistic user
  scale — a single agent turn takes 5–15 seconds, so the rate cap is
  unreachable in practice. XMLA stays fully designed as a documented backup,
  not a planned second phase — I scoped it out rather than leave it as an
  open contingency with no decision behind it."
- "Prompt caching isn't one mechanism — Claude needs an explicit marker,
  OpenAI is automatic, Gemini needs 32K tokens before its explicit path even
  applies. I isolated that behind one adapter function so switching models
  doesn't mean rewriting the caching logic three different ways."
- "The agent can't silently pick between a raw bronze table and a cleaned
  silver one for the same question — I curated a single restricted
  dataset so there's exactly one canonical table per concept, and IAM
  enforces it."
- "Access is controlled at the dashboard level, not the tool level — one
  shared service account, one identity to grant and audit. Simpler than
  per-user RLS, and correct for how this system is actually used."
- "I considered splitting BigQuery and DAX generation into separate sub-agents
  — a real pattern, not a niche idea. Declined it for a user-specific reason:
  field operations staff need fast answers, and sub-agents add LLM round-trips
  for a specialization benefit this tool count doesn't need yet."
- "Here's where I ran enterprise-grade and here's where I took the cheaper
  portfolio route — and why."

---
