# Data Pipeline: `agent_safe` + Vector Index

Two build efforts landing in BigQuery. Do `agent_safe` first — it establishes
the Dataform project the vector index model needs.

## The three pipelines at a glance

**Three pipelines, three shapes.** Two are Dataform; one is a local parse to a
committed file. Only project docs are chunked and embedded.

| # | Source | Built by | Output | Read by |
|---|---|---|---|---|
| **1** | Instacart bronze tables | Dataform (`agent_safe` tag) | `agent_safe.*` tables | `run_bigquery_sql`, `bigquery_schema` |
| **2** | Live Power BI (`executeQueries` + Scanner API) | `scripts/build_model_context.py` (local, **no Dataform**) | `context/schema/model_schema.json`, committed | static context + `get_measure_dax` |
| **3** | `context/docs/*.md`, `*.html` | `scripts/build_vector_db.py` → Dataform (`vector_db` tag) | `vector_db.chunks_docs_embedded` | `search_docs` |

**They refresh independently.** A measure rename touches only #2; a docs edit
only #3; new source data only #1. Each part below ends with its own refresh
sequence.

**Why the model schema isn't embedded.** Small, fully enumerable, and needed
on nearly every DAX composition — the conditions where retrieval is the wrong
tool. It also fails exactly where it matters: `Recall@5` and `Recall@10` embed
almost identically.

**Not in any pipeline:** `context/page_info/` (read whole by `get_page_info`)
and the ML repo's Python and SQLX (read live via
`list_repo_files`/`read_repo_file`).

**Pipeline 3 is two steps, and order matters.** `build_vector_db.py` runs
locally and writes `staging.doc_chunks`; then Dataform's `vector_db` tag reads
that staging table, calls `ML.GENERATE_EMBEDDING`, and writes the output.
**No embedding API call in Python** — it happens inside BigQuery, which is why
nothing in `app/` needs an embedding client.

Run the script *before* executing the tag. An *empty* staging table is caught
(`assert_chunks_docs_not_empty` fails the run); a *stale* one is not — every
assertion passes and you get a cleanly-built index of outdated content, with
no error anywhere.

Uses `ML.GENERATE_EMBEDDING` with `gemini-embedding-001` — see Part 3,
"Embedding model + index," for why it isn't the built-in `embeddinggemma-300m`.

## Deployment: how `.sqlx` reaches BigQuery

**Writing a `.sqlx` never creates anything by itself** — there's always a
separate compile-then-execute step. **Decision: GitHub-linked hosted
Dataform** (not the standalone CLI, not console pasting):

1. Write `.sqlx` into `definitions/` — normal code.
2. Push to GitHub — normal commit.
3. **One-time (console):** BigQuery Studio → Dataform → create repository →
   link to this GitHub repo.
4. Pull into a Dataform workspace.
5. **Compile, then execute** — a separate step from pulling.

**`workflow_settings.yaml` and `definitions/` sit at the repo root**, beside
`app/` and `docs/` — not nested in a `dataform/` folder. Dataform expects
them at the root and can't be pointed at a subdirectory (an open feature
request, not an option). It only compiles `definitions/`, so the other
top-level folders are invisible to it — they'll just appear in the Dataform
workspace's file browser, which is cosmetic.

Steps 1–2 repeat like any code change; 3 is one-time; 4–5 repeat per deploy.

**Split by who does them.** Claude Code writes the `.sqlx` and commits (1–2).
Steps 3–5 happen in the BigQuery Studio UI and are **yours to click through**
— Claude Code should not attempt to drive console flows, and should say so
rather than guessing at a UI it can't see.

## The two Dataform pipelines share one definitions folder

**Dataform compiles one graph** — no per-subfolder boundary; subfolders are
organization only. **But execution runs a selected subset**, and the two
pipelines share **zero `${ref(...)}` edges**, so they're disconnected islands
in that graph. Selecting a tag runs exactly those nodes because no path
connects them.

```
definitions/
├── sources_*.sqlx    declaration blocks only — no tag, flat, one file per table
├── agent_safe/       tags: ["agent_safe"]
└── vector_db/        tags: ["vector_db"]
```

**Declarations are flat files directly under `definitions/`, not a `sources/`
subfolder** — matches the ML repo's own proven layout, and required either
way: a `.sqlx` file compiles to exactly one action, so multiple declarations
can't be stacked in one file (confirmed against a real compile failure, not
assumed).

**Pipeline 2 has nothing here** — it produces a committed JSON file, not a
BigQuery table, so there's no `.sqlx` and no tag to execute.

**Execute by tag, never "run all."** Running everything re-embeds the whole
corpus — a real Vertex AI bill — every time an unrelated model changes.

**Breaks only if** a `.sqlx` in one pipeline ever `${ref(...)}`s the other's
output; the islands merge and `includeDependencies`/`includeDependents` start
to matter. Don't create that edge.

**This repo has its own Dataform repository** — not an extension of the
Instacart ML pipeline. Reference its tables with `type: "declaration"`
blocks; never copy its `.sqlx` files here.

```javascript
// definitions/sources_base_analytical_table.sqlx — one file, one declaration
config {
  type: "declaration",
  database: "YOUR_PROJECT",
  schema: "building_blocks",
  name: "base_analytical_table",
  description: "Order-line grain: one row per order x product, names already joined in."
}
```

---

## Part 1 — `agent_safe` (Phase 3)

**Why a curated dataset at all.** Two lineages answering the same question
differently is a non-determinism risk, not flexibility. `agent_safe` fixes one
canonical table per concept **at design time**, and granting access to nothing
else makes that a security boundary rather than a prompt instruction.

**Everything here is a `type: "table"` enrichment** — dimension joins,
precomputed results. If a candidate model needs no enrichment, it doesn't
belong in `agent_safe` at all.

Materializing keeps the dry-run estimate small and fixed, which matters for
the cost gate; keeps the correctness surface small for ad hoc follow-ups; and
costs nothing in freshness, since product/aisle/department names are static.

```bash
# 1. Create the datasets — one-time, outside Dataform. All three are needed:
#    agent_safe  — enriched tables the agent queries
#    vector_db   — embedded chunks that search_docs reads
#    staging     — doc_chunks (build_vector_db.py writes here) + embedding_model
bq mk --dataset YOUR_PROJECT:agent_safe
bq mk --dataset YOUR_PROJECT:vector_db
bq mk --dataset YOUR_PROJECT:staging
```

```javascript
// definitions/agent_safe/product_order_analysis.sqlx — real shape, confirmed
// against the ML repo's actual .sqlx (github.com/Jason-Handerhan/
// Kaggle-Instacart-Reorder-Engine-Portfolio-Project), not a placeholder.
config {
  type: "table",
  schema: "agent_safe",
  name: "product_order_analysis",
  tags: ["agent_safe"],
  columns: {
    product_id: "Unique product identifier.",
    product_name: "Human-readable product name.",
    aisle: "Human-readable aisle name.",
    department: "Human-readable department name.",
    user_reorder_ratio: "Share of this user's cumulative product line items that were reorders.",
    // ...49 more columns, one real description each — full list in the file.
  }
}
// Grain: one row per order x product — base_analytical_table's own grain.
// Every join key below is unique in the child table for that grain, so this
// is a 1:1 enrichment, never a fan-out.
SELECT b.*, o.*, upf.*, a.*, d.*, dow.*, tod.*
FROM ${ref("base_analytical_table")} b
LEFT JOIN ${ref("prelim_user_order_features")} o
  ON b.user_id = o.user_id AND b.order_id = o.order_id
LEFT JOIN ${ref("prelim_user_product_features")} upf
  ON b.user_id = upf.user_id AND b.order_id = upf.order_id AND b.product_id = upf.product_id
LEFT JOIN ${ref("prelim_user_aisle_features")} a
  ON b.user_id = a.user_id AND b.order_id = a.order_id AND b.aisle_id = a.aisle_id
LEFT JOIN ${ref("prelim_user_department_features")} d
  ON b.user_id = d.user_id AND b.order_id = d.order_id AND b.department_id = d.department_id
LEFT JOIN ${ref("prelim_user_order_dow_wide")} dow
  ON b.user_id = dow.user_id AND b.order_id = dow.order_id AND b.product_id = dow.product_id
LEFT JOIN ${ref("prelim_user_order_tod_wide")} tod
  ON b.user_id = tod.user_id AND b.order_id = tod.order_id AND b.product_id = tod.product_id
```

**`base_analytical_table` already carries `product_name`/`aisle`/`department`**
— it's the ML repo's own order-line fact table, not a bare-IDs gold table, so
no dimension re-join is needed. The real enrichment is the ML repo's own
**silver-layer feature tables** (`prelim_user_order_features`,
`prelim_user_product_features`, `prelim_user_aisle_features`,
`prelim_user_department_features`, and the day-of-week/time-of-day wide
variants) — each already at a grain that joins onto `base_analytical_table`
1:1 with no aggregation needed. This is what makes the agent's job easier:
reorder ratios, cumulative purchase counts, and day/time affinities are
already computed, not something the agent has to derive with window
functions per question.

`final_ml_features_table` (ML's own gold training matrix) is a **separate**
`agent_safe` table, kept at its native (user, candidate product, anchor
order) grain — supplemented with `product_name`/`aisle`/`department` only.
It answers a different question shape (correlating features against the
reorder label), so collapsing it to `product_order_analysis`'s grain would
destroy the history that question needs.

**Those `columns:` descriptions are the agent's schema grounding, not
documentation.** The `bigquery_schema` MCP resource reads them live from
`INFORMATION_SCHEMA` — that's how `run_bigquery_sql` knows what columns mean.
Write them as if explaining to a new analyst; don't restate the column name.

```bash
# 3. IAM — project-level jobUser + dataViewer scoped to agent_safe only
```

The `dataViewer` grant on `agent_safe` is all `agent-sa` needs — no
source-dataset authorization, because a materialized table carries its own
data rather than reading through to `silver`.

**Tables need a refresh trigger.** Execute the `agent_safe` tag when source
data changes.

---

## Part 2 — Model schema (Phase 3)

**A pipeline, but not a Dataform one.** Query live Power BI → one committed
JSON → read into static context at startup. No SQL, no BigQuery, no
embeddings. It shares the manual-refresh discipline with the others, which is
why it lives here.

**Decided 2026-09-17, replacing an earlier `.pbip`/TMDL-parsing design** —
removes a fragile hand-written regex parser and an undetectable staleness
risk, at the cost of two APIs instead of one file. **Artifact shape and the
manual refresh discipline are unchanged**; this is a data-source swap, not a
pipeline redesign.

### Two APIs, split by what each can see

**`executeQueries`** (already used by `run_dax_query`, Contributor role, no
Premium) runs `INFO.VIEW.TABLES()` / `INFO.VIEW.COLUMNS()` /
`INFO.VIEW.MEASURES()` / `INFO.VIEW.RELATIONSHIPS()` as plain DAX queries.
Covers tables, columns, measure names + descriptions, and relationships
(including cardinality and cross-filter direction) — but **the actual DAX
`Expression` text comes back `null`, confirmed empirically up to and
including Admin workspace role.** Microsoft's own docs say `Expression` only
shows for callers with "write permission on the semantic model" — that
turned out to mean something else entirely (next paragraph), not a role you
can grant on a workspace.

**The Scanner API** (`POST admin/workspaces/getInfo?datasetExpressions=true`
→ poll `scanStatus` → fetch `scanResult`) is what actually returns
`Expression`. It needs its own, separate authorization: the tenant setting
**"Allow service principals to use read-only admin APIs"**, plus **"...
detailed metadata"** and **"...DAX and mashup expressions"**, all three
scoped to the same security group `power-bi-sp-client-id` already belongs to
(`local-dev-environment-setup.md` Step 14, A7). This is a genuinely separate
permission gate from workspace roles, not a stronger version of Contributor —
confirmed by Admin role alone doing nothing for `Expression` visibility.
**Read-only by nature** (no write operation exists on this API), but its
scope is tenant-wide — it can see every workspace's metadata, not just this
one — which is why it's the one meaningfully broader grant in this project's
auth model, and worth knowing about if this dashboard ever isn't the only
thing in the tenant.

### Auto date tables still need filtering

Power BI's hidden auto-date tables (named `LocalDateTable_...` or
`DateTableTemplate_...`) show up in `INFO.VIEW.TABLES()` and
`INFO.VIEW.RELATIONSHIPS()` the same way they showed up in TMDL — they're a
model-level artifact, not a parsing quirk. Still exclude them by name from
both `tables[]` and `relationships[]`: join paths the agent should never
take, same reasoning as before, just checked against `[Name]` instead of a
filename.

### Parameter detection — a text pattern, not a flag

Neither API exposes an `IsParameterTable` property. What-if and field
parameter tables are just calculated tables under the hood, and their
auto-generated "value measure" has a distinctive, reliable shape:

```
SELECTEDVALUE('Conversion Rate'[Conversion Rate], 0.06)
```

**A measure whose entire `Expression` is exactly one bare `SELECTEDVALUE(...)`
call is a parameter's value measure** — the table and column names, and the
default, are read directly out of the call. Confirmed against this project's
real dashboard: cleanly found all 5 what-if parameters this way
(`notebooks/dax_schema_exploration.ipynb`). A broader pass — `SELECTEDVALUE`
appearing *anywhere* in an expression, not just as the whole thing — also
catches field-parameter usage inside larger measures, but is **not** a clean
signal on its own: it also matched two ordinary measures reading a plain data
column with `SELECTEDVALUE`, unrelated to any parameter. Use the strict form
for `parameters[]`; treat the broad form as a lead to inspect, not a fact.

### Excluding HTML-display measures from the registry

Some Power BI measures return a rendered HTML/markdown string for a visual
rather than an aggregation — confirmed against the real dashboard:
`Financial_Assumptions_HTML`, `HTML_Model_Evaluation_Info`,
`HTML_Icon_Author_Credit`, `HTML_Financial_Impact_Info`
(`scripts/build_model_context.py`'s `HTML_DISPLAY_MEASURES`).
**They must not reach the measure registry.** It's what the model picks
measure names from; a display measure is never referenced in a
`SUMMARIZECOLUMNS` the way `[Recall at 5]` is, so listing it spends attention
on something the model should never choose — and `strict=True` would accept
it as a valid `get_measure_dax` argument.

**Primary mechanism: manual exclusion** — a small list of measure names to
skip. Only a handful exist and they change rarely.

**Backstop: check the Scanner API's `expression` text** for
`re.search(r"<[a-z]+[ >]", expression, re.I)`, same regex as before, just
against this field instead of TMDL-parsed DAX. Not a replacement for the
list — a cheap guard so a *newly added* HTML measure doesn't silently slip in
before anyone updates it.

**Their content isn't lost** — the same page-info HTML lives in
`context/page_info/`, returned whole by `get_page_info`.

### Table count mismatch — resolved

`INFO.VIEW.TABLES()` returns 25 tables for the real dashboard; the Scanner
API's `scanResult` returns 23. The two extras are Power BI's own
auto-generated date tables (`LocalDateTable_<GUID>`,
`DateTableTemplate_<GUID>`) — the Scanner API excludes them on its own, so
its 23-table list is already the correct set. `build_model_context.py`
excludes any `INFO.VIEW.TABLES()` row matching those two name prefixes
before building `TABLE_REGISTRY`, rather than filtering by `IsHidden` — some
legitimate tables (parameter tables) are also hidden, so a hidden-flag
filter would wrongly drop those too.

### Parameter range — resolved, via the table's real values

Neither API exposes a calculated table's defining formula (the old TMDL
parser read `source = GENERATESERIES(min, max, step)` directly; Scanner
API's DAX-expression capability covers measures, not calculated-table
source). But the formula isn't actually needed — a what-if parameter's
calculated table is a real, materialized table, so its values can be read
directly with the same `executeQueries` call used for everything else:

```python
values = sorted(round(v, 9) for v in run_dax(f"EVALUATE VALUES('{table}'[{column}])"))
step = round(values[1] - values[0], 9) if len(values) > 1 else None
range_ = {"min": values[0], "max": values[-1], "step": step}
```

Confirmed against all 5 real parameter tables. `round(v, 9)` strips the
floating-point noise `GENERATESERIES`'s repeated addition leaves behind
(`0.030000000000000002`, `0.009000000000000001`) — float64 noise from
repeated addition shows up around the 15th-17th digit, so 9 decimal places
comfortably clears it while preserving real precision far finer than any
what-if parameter here actually uses.

### Refreshing the artifact

```bash
# Semantic model changed (measure renamed, column added, description edited):
python scripts/build_model_context.py   # rewrites context/schema/model_schema.json
git add context/schema/model_schema.json && git commit    # the app reads THIS
# then redeploy — it's read once at startup, not the live APIs
```

**No embedding, no Dataform, no BigQuery.** `scripts/build_model_context.py`
itself is not yet rewritten to this design — the two API calls, the
`SELECTEDVALUE` extraction, and the artifact assembly are proven in
`notebooks/dax_schema_exploration.ipynb`; turning that into the actual script
is later work, not done here.

**Deferred, not rejected: a scheduled rebuild job instead of a manual
script.** The ideal version of this pipeline needs no human to remember
anything — a periodic job (same Cloud Scheduler + Cloud Run Job pattern as
the Phase 6 judge) reruns the live-API build and writes the artifact
somewhere the gateway reads at startup, so staleness stops being possible at
all rather than just less fragile to detect. Not built now — more
infrastructure than this portfolio project's current scope justifies —
tracked in `docs/build-order.md` Phase 7.

## Part 3 — Docs vector index (Phase 3)

### Two identities: the dataset boundary is IAM, not application code

`run_bigquery_sql` lets the model write arbitrary SQL. Search tools run a
fixed `VECTOR_SEARCH` template against `vector_db`. **Those datasets are kept
apart by separate service accounts**, so the agent cannot hand-write a query
against the vector tables:

```bash
# agent-sa (Cloud Run's identity) — warehouse only
gcloud projects add-iam-policy-binding YOUR_PROJECT \
  --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer" --condition=...   # scope to agent_safe

# vector-search-sa — vector_db only
gcloud iam service-accounts create vector-search-sa

# agent-sa may mint short-lived tokens AS vector-search-sa
gcloud iam service-accounts add-iam-policy-binding \
  vector-search-sa@YOUR_PROJECT.iam.gserviceaccount.com \
  --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

```python
from google.auth import default, impersonated_credentials

base_creds, _ = default()
bq_warehouse = bigquery.Client()                    # agent-sa -> agent_safe

vector_creds = impersonated_credentials.Credentials(
    source_credentials=base_creds,
    target_principal="vector-search-sa@YOUR_PROJECT.iam.gserviceaccount.com",
    target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
)
bq_vector = bigquery.Client(credentials=vector_creds)   # -> vector_db
```

**Why impersonation and not a "run as" parameter.** `bigquery.Client()`
doesn't authenticate by naming an account — it presents a token, and on Cloud
Run the metadata server only issues tokens for the *one* account the revision
deployed with. Impersonation is how you obtain a token for a different
account, and it works only because of the explicit `serviceAccountTokenCreator`
binding above. The alternative — downloading a key file for
`vector-search-sa` — means a long-lived credential to store and rotate, which
this project avoids everywhere else.

**Why not a tool-layer allow-list instead.** It would mean correctly
extracting table references from arbitrary SQL — CTEs, subqueries, aliases,
nested joins — and a parsing bug is a boundary you *believe* is enforced but
isn't. BigQuery enforcing IAM has no such failure mode.

**The tradeoff:** IAM's error is `Access Denied`, less actionable for a retry
than *"use `agent_safe.X` instead"*. Mitigated by `bigquery_schema` only
exposing `agent_safe` schemas — the agent shouldn't be composing queries
against tables it can't see. IAM is the backstop, not the guide.

### Chunk schema

The staging table stays a single `WRITE_TRUNCATE` load.

```python
class Chunk(BaseModel):
    chunk_id: str          # content hash — makes reloads idempotent
    doc_source: str        # which document this came from, e.g. "readme"
    file_path: str
    section: str | None    # heading breadcrumb, e.g. "Environment Setup &
                           # Provisioning > 2. IAM Security"
    length: int             # len(chunk_text), in characters
    chunk_text: str
```

No `start_line`/`end_line` — meaningful for code, not prose, and this schema
isn't shared across content types: a hypothetical future code source would
get its own table, not a column that's always `None` here.

| `doc_source` | Table | Sources | Feeds |
|---|---|---|---|
| `readme` | `vector_db.chunks_docs_embedded` | Project methodology — the README (`context/docs/index.html`), minus the orientation-bundle block, which is always in static context. Heading breadcrumb prepended | `search_docs` |

**One output table, not one-with-a-filter.** A second document gets its own
`doc_source` value in the same table, not a new table or a `WHERE` clause —
a filter on a vector search may apply *after* the nearest-neighbor scan
rather than pushed into it, so `top_k => 5` could return fewer than 5.

**Too small for `CREATE VECTOR INDEX`.** BigQuery's IVF index has a
5,000-row minimum; this table has 66. `VECTOR_SEARCH` runs a brute-force
exact scan with no index at all — the correct outcome at this scale, not a
fallback.

**Excludes the orientation-bundle content** (Executive Summary, Project
Navigator, System Architecture — always in static context already, so
indexing it wastes a retrieval slot) **by heading name, from inside the one
HTML file** — `index.html` contains both the orientation content and
everything else in one document, so `chunk_docs` skips every element between
the "Executive Summary" and "Environment Setup & Provisioning" depth-0
titles before chunking. A heading-name rule, not a line range, so it
survives future edits to the document.

### Chunkers

```python
from unstructured.partition.html import partition_html
from unstructured.partition.md import partition_md
from unstructured.chunking.title import chunk_by_title
import hashlib, pathlib

EXCLUDE_START_TITLE = "Executive Summary"
EXCLUDE_END_TITLE = "Environment Setup & Provisioning"

def _chunk_id(file_path: str, text: str) -> str:
    """Content-addressed → identical ids for unchanged content, so reloads
    are idempotent instead of duplicating rows."""
    return hashlib.sha256(f"{file_path}:{text}".encode()).hexdigest()[:16]

def chunk_docs(path: pathlib.Path, doc_source: str) -> list[Chunk]:
    """Structure-aware — respects headings so a chunk is a coherent section.

    Prepends the heading breadcrumb to chunk_text. A chunk under
    "### CI/CD Authentication" reading "we use Workload Identity Federation
    instead" is meaningless once separated from its parent "## Trade-offs".
    """
    partition = partition_html if path.suffix == ".html" else partition_md
    elements = partition(filename=str(path))

    filtered, skipping = [], False
    for el in elements:
        is_top_title = el.category == "Title" and el.metadata.category_depth == 0
        if is_top_title and str(el) == EXCLUDE_START_TITLE:
            skipping = True
        if is_top_title and str(el) == EXCLUDE_END_TITLE:
            skipping = False
        if not skipping:
            filtered.append(el)

    # Build a heading stack from the PRE-chunked, FILTERED elements.
    # chunk_by_title decides *where* to break; it does not hand back a
    # breadcrumb — the library treats chunking as a downstream consumer of
    # elements, so the stack has to be tracked here.
    breadcrumbs, stack = {}, []
    for el in filtered:
        depth = el.metadata.category_depth or 0
        if el.category == "Title":
            stack[depth:] = [str(el)]          # push, truncating deeper levels
        breadcrumbs[el.id] = " > ".join(stack)

    chunks = []
    for c in chunk_by_title(filtered, max_characters=1500, overlap=150):
        # chunk_by_title's own chunk ids don't match any source element id.
        # Use the chunk's first ORIGINAL element instead, which does — a
        # direct reference, not a guess by ordinal position.
        first_orig = c.metadata.orig_elements[0]
        crumb = breadcrumbs.get(first_orig.id, "")
        text = f"[{crumb}]\n{c}" if crumb else str(c)
        chunks.append(Chunk(
            # .as_posix(), not str(path): str() renders OS-native separators,
            # which would fold a backslash into the hash on Windows and give
            # a DIFFERENT chunk_id for identical content run from a
            # different OS later (e.g. a Linux CI job) — breaking the
            # "identical ids for unchanged content" guarantee _chunk_id
            # exists for.
            chunk_id=_chunk_id(path.as_posix(), text), doc_source=doc_source,
            file_path=path.as_posix(), section=crumb or None,
            length=len(text), chunk_text=text))
    return chunks
```

**Why the breadcrumb is prepended rather than stored in a column:** only
`chunk_text` reaches `ML.GENERATE_EMBEDDING` (`file_path`/`section` ride
along as columns but are invisible to the model). This is the same
"contextual retrieval" idea — attach the context where it actually affects
the embedding.

**Not splitting `search_docs` further by section.** The tables/measures split
was justified by a real correctness risk (post-filter recall loss degrading
DAX grounding). Prose has no equivalent argument — one coherent corpus with
semantic search is already correct, and fragmenting it would add complexity
without the justification.

**Confirm the `///` capture against a real export before trusting it.**
Multi-line description spacing, and whether Desktop emits `///` immediately
above the declaration with no blank line between, are the details most likely
to differ from the assumption above. A mismatch **fails silently** — the
description sits in the file, never reaches `chunk_text`, and nothing errors.

### Load to staging

```python
def load_chunks(chunks: list[Chunk], table: str = "staging.doc_chunks") -> None:
    """Writes every chunk to the ONE staging table Dataform reads.

    CALL THIS ONCE, with the full combined list across all files. Calling it
    per-file means each call WRITE_TRUNCATEs over the last, leaving only the
    final file's chunks in staging. Invisible on a single-file test run;
    surfaces only once context/ holds more than one file.
    """
    client = bigquery.Client()
    client.load_table_from_json(
        [c.model_dump() for c in chunks], table,
        job_config=bigquery.LoadJobConfig(
            write_disposition="WRITE_TRUNCATE",   # full rebuild, not append
            schema=[
                bigquery.SchemaField("chunk_id", "STRING"),
                bigquery.SchemaField("doc_source", "STRING"),
                bigquery.SchemaField("file_path", "STRING"),
                bigquery.SchemaField("section", "STRING"),
                bigquery.SchemaField("length", "INTEGER"),
                bigquery.SchemaField("chunk_text", "STRING"),
            ])).result()
```

`WRITE_TRUNCATE` = full rebuild every run, which is correct given the refresh
is manual.

### Putting it together — the actual entrypoint

The chunkers above each handle **one file**. Nothing yet walks the `context/`
directories, picks the right chunker per file, and gets the combined result
into BigQuery — that's the missing piece, and it's what `scripts/
build_vector_db.py` actually runs:

```python
# scripts/build_vector_db.py
import pathlib

CONTEXT_ROOT = pathlib.Path("context")

# Which document each known file represents. Explicit rather than inferred
# from the filename — there's one file today, and a naming-inference scheme
# for files that don't exist yet is exactly the premature generality this
# project avoids. Add a line here when a second doc lands.
DOC_SOURCES = {
    "index.html": "readme",
}

# NOT here, and why:
#   Semantic model      -> scripts/build_model_context.py instead, queried
#                          live from Power BI. Small and fully enumerable, so
#                          it goes in static context, not behind a retrieval
#                          call. Relationships too — structural, needed on
#                          nearly every DAX composition, never retrieved.
#   page_info HTML      -> served whole by get_page_info.
#   ML repo .py / .sqlx -> read live via list_repo_files / read_repo_file.

def main() -> None:
    all_chunks: list[Chunk] = []
    for filename, doc_source in DOC_SOURCES.items():
        path = CONTEXT_ROOT / "docs" / filename
        all_chunks.extend(chunk_docs(path, doc_source=doc_source))

    # Empty means chunking silently produced nothing — which would build an
    # EMPTY chunks_docs_embedded with no error anywhere, and search_docs
    # would silently return nothing on every call.
    if not all_chunks:
        raise SystemExit("Refusing to load: 0 chunks. Check context/docs/.")

    print(f"Chunked {len(all_chunks)} pieces from context/docs/.")
    load_chunks(all_chunks)   # one call, one WRITE_TRUNCATE — see above
    print("Loaded to staging.doc_chunks. Now execute the 'vector_db' tag in Dataform.")

if __name__ == "__main__":
    main()
```

`python scripts/build_vector_db.py` is the exact command referenced
throughout this doc's refresh sequence below.

### Embedding model + index

**`gemini-embedding-001`** — Google's current #1 MTEB retrieval-quality
model, ahead of `text-embedding-005` (legacy) and the built-in
`embeddinggemma-300m` (a lighter open model built for
on-device/resource-constrained use, not for best-in-class quality).

```sql
-- one-time: BigQuery → Vertex AI connection
-- bq mk --connection --location=US --connection_type=CLOUD_RESOURCE vertex_conn
-- then grant that connection's service account roles/aiplatform.user
--   (shown in the IAM console/newer docs as "Agent Platform User" — same role)
--
-- The connection's location must exactly match the dataset's location
-- (US) — connection locations are fixed at creation time, same as
-- datasets, so a mismatch means deleting and recreating, not a query fix.

CREATE OR REPLACE MODEL `YOUR_PROJECT.staging.embedding_model`
REMOTE WITH CONNECTION `YOUR_PROJECT.US.vertex_conn`
OPTIONS (ENDPOINT = 'gemini-embedding-001');
```

**One model.** It embeds everything in staging — there's a single source now,
so no `doc_source` filter is needed at build time or search time.

```sql
-- definitions/vector_db/chunks_docs_embedded.sqlx
config {
  type: "table", schema: "vector_db", name: "chunks_docs_embedded",
  tags: ["vector_db"],
  assertions: {
    rowConditions: [
      "chunk_id IS NOT NULL",
      "chunk_text IS NOT NULL",
      "embedding IS NOT NULL"
    ]
  }
}

SELECT chunk_id, doc_source, file_path, section, length, chunk_text,
       ml_generate_embedding_result AS embedding
FROM ML.GENERATE_EMBEDDING(
  MODEL `YOUR_PROJECT.staging.embedding_model`,
  (SELECT *, chunk_text AS content FROM ${ref("doc_chunks")}),
  STRUCT(TRUE AS flatten_json_output))
```

**`rowConditions`, not `nonNull`/`uniqueKey`** — same pattern as `agent_safe`'s
assertions: proven against the ML repo's own real `.sqlx`, unlike
`nonNull`/`uniqueKey`.

**`${ref("doc_chunks")}`, single-arg** — `staging.doc_chunks` needs its own
declaration for this to resolve at all, since it's built by
`scripts/build_vector_db.py` entirely outside Dataform (same reason
`base_analytical_table` needed one):

```sql
-- definitions/sources_staging_doc_chunks.sqlx
config {
  type: "declaration", database: "YOUR_PROJECT", schema: "staging",
  name: "doc_chunks",
  description: "README chunks loaded by scripts/build_vector_db.py -- built outside Dataform, WRITE_TRUNCATE on every run."
}
```

Once declared, `ref("doc_chunks")` (no schema argument) resolves correctly —
Dataform looks up the schema from the declaration itself, the same reason
`ref("base_analytical_table")` needs no schema either.

It carries the `vector_db` tag, so the refresh workflow below is unchanged.

### Assertions — catch bad builds before the app ever queries them

**Build-time safety net, not a runtime guardrail.** A failed assertion fails
the Dataform run; it doesn't stop the app querying a stale table from an
earlier good build. Complementary to the manual gates in
`docs/success-criteria.md`, which only fire when someone runs a search.

`rowConditions` above covers null-checking; row-count parity or uniqueness
checks (if ever needed) would follow the same standalone-file pattern below,
same as `agent_safe`'s row-count-matches-source assertion.

Emptiness needs its own file — no `rowConditions`/`nonNull` check can catch
it, since there are no rows to evaluate a per-row condition against:

```sql
-- definitions/vector_db/assert_chunks_docs_embedded_not_empty.sqlx
-- An empty table fails SILENTLY at runtime: search_docs just returns
-- nothing, no error anywhere.
config { type: "assertion", tags: ["vector_db"] }
SELECT n FROM (SELECT COUNT(*) AS n FROM ${ref("chunks_docs_embedded")})
WHERE n = 0
```

**`SELECT n`, not `SELECT 1`** — Dataform compiles every assertion into a
`CREATE VIEW`, and BigQuery rejects a view with an unnamed column. A bare `1`
has no name; reusing the already-named `n` does.

**No vector index** — BigQuery's IVF index has a 5,000-row minimum; this
table has 66. `VECTOR_SEARCH` runs a brute-force exact scan with no index at
all, which is strictly better here: exact results, zero index-maintenance
cost. If the corpus ever legitimately grows past that threshold:

```sql
CREATE VECTOR INDEX chunks_docs_embedded_idx
ON `YOUR_PROJECT.vector_db.chunks_docs_embedded`(embedding)
OPTIONS(index_type = 'IVF', distance_type = 'COSINE');
```

### What `search_docs` runs

One parameterized query against the single table. **No `WHERE` clause** —
there's one content type.

```python
SEARCH_SQL = """
SELECT base.chunk_text, base.file_path, base.section, distance
FROM VECTOR_SEARCH(
  TABLE `{project}.vector_db.chunks_docs_embedded`, 'embedding',
  (SELECT ml_generate_embedding_result AS embedding
   FROM ML.GENERATE_EMBEDDING(
     MODEL `{project}.staging.embedding_model`,
     (SELECT @query AS content),
     STRUCT(TRUE AS flatten_json_output))),
  top_k => @top_k, distance_type => 'COSINE')
"""
```

**Must use the same model as the corpus embedding** — `staging.embedding_model`
(`gemini-embedding-001`), not a different one. Query and corpus vectors from
different models live in incompatible spaces; this isn't a style preference,
it's a hard requirement for `VECTOR_SEARCH` to return anything meaningful.

**`search_docs` is the only vector-search tool.** Schema comes from static
context and `get_measure_dax` (`.claude/rules/tools.md`), code search is
agentic (`docs/code-search.md`), and `get_page_info` reads files directly.

### Refreshing the index

```bash
# Docs changed (new/changed README content, no .sqlx change):
python scripts/build_vector_db.py       # chunk + load staging — FIRST
# then execute the "vector_db" TAG in the Dataform UI (not all actions)

# .sqlx logic change (different SELECT):
# full push → pull → execute cycle
```

Re-executing the *same* `.sqlx` against fresh staging is what picks up new doc
content — no GitHub round-trip for a routine refresh. `CREATE VECTOR INDEX`
only runs on first build or a full rebuild.
