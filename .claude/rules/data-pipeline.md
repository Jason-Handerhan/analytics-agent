---
paths:
  - 'definitions/**'
  - 'scripts/build_vector_db.py'
  - 'scripts/build_model_context.py'
  - 'scripts/dump_schemas.sh'
  - 'tests/test_static_context.py'
  - 'tests/test_model_schema_build.py'
---

# Data pipelines: Dataform, model schema, vector index

> **Deep dive:** `docs/data-pipeline.md` — the extraction script, the chunker,
> the embedding SQL, and the search query. Read the relevant Part before
> writing any of them.

**Three pipelines, refreshed independently.** Two are Dataform; one is a local
parse to a committed file.

| # | Built by | Output | Refresh trigger |
|---|---|---|---|
| **1** `agent_safe` | Dataform (`agent_safe` tag) | BigQuery tables | Source data changed |
| **2** Model schema | `scripts/build_model_context.py` — **no Dataform** | `context/schema/model_schema.json`, committed | Semantic model changed |
| **3** Docs index | `scripts/build_vector_db.py` → Dataform (`vector_db` tag) | `vector_db.chunks_docs` | Docs changed |

A measure rename touches only #2. A docs edit only #3. Never re-run all three
out of habit — #3 re-embeds the corpus and bills Vertex AI for it.

## The two Dataform pipelines, one definitions folder

```
definitions/
├── sources_*.sqlx    declaration blocks only — no tag, flat, one file per table
├── agent_safe/       tags: ["agent_safe"]
└── vector_db/        tags: ["vector_db"]
```

Dataform compiles **one graph**, but execution runs a **selected subset**. The
two share **zero `${ref(...)}` edges**, so they're disconnected islands and a
tag runs exactly those nodes. **Never `${ref()}` across them** — that merges
the islands and breaks tag-based execution.

**Always execute by tag, never "run all."**

**This project has its own Dataform repository.** Not an extension of the
Instacart ML pipeline. Reference its tables with `type: "declaration"`
blocks, one flat file per table directly under `definitions/`; **never copy
its `.sqlx` files here.**

## Pipeline 1 — `agent_safe` holds tables

**Always `type: "table"`.** Everything in `agent_safe` is an *enrichment* —
dimension joins and precomputed results. If a candidate model needs no
enrichment, it doesn't belong in `agent_safe` at all.

Authorization is just `dataViewer` on `agent_safe`. Tables need a refresh
trigger: execute the `agent_safe` tag when source data changes.

**`columns:` descriptions are not documentation.** They're the agent's schema
grounding — `bigquery_schema` reads them live from `INFORMATION_SCHEMA`, and
that's how `run_bigquery_sql` knows what a column means. Write them as if
explaining to a new analyst; never restate the column name. **A table without
descriptions is one the agent will query badly.**

## Pipeline 2 — model schema, queried live not parsed

**Decided 2026-09-17, replacing an earlier `.pbip`/TMDL-parsing design**
(`docs/data-pipeline.md` has the full rationale and the two still-open gaps).
`scripts/build_model_context.py` queries live Power BI and writes **one
committed JSON** with four keys: `tables`, `measures`, `parameters`,
`relationships`. The gateway reads it at startup and splits it into
static-context registries plus a DAX lookup (`.claude/rules/gateway.md`).

**Two APIs, not one file format:**
- `executeQueries` (`INFO.VIEW.TABLES/COLUMNS/MEASURES/RELATIONSHIPS()`) —
  tables, columns, measure names + descriptions, relationships. No Premium,
  works on Contributor.
- Scanner API (`admin/workspaces/getInfo?datasetExpressions=true`, async
  submit→poll→fetch) — the actual DAX `Expression` text, which
  `executeQueries` returns `null` for regardless of role. Needs its own
  tenant-setting grant, separate from workspace roles
  (`local-dev-environment-setup.md` Step 14, A7).

**Parameter detection is a text pattern, not a flag.** A measure whose entire
`Expression` is exactly `SELECTEDVALUE('Table'[Column], default)` is a
parameter's value measure — table, column, and default read straight out of
the call. A broader search for the same pattern *inside* a larger expression
also exists but is **not** a clean signal alone — it also matches ordinary
measures reading a plain data column, unrelated to any parameter.

**Auto date tables (`LocalDateTable_*`/`DateTableTemplate_*`) still need
filtering** from both tables and relationships — a model-level artifact, not
a parsing quirk, so it shows up in `INFO.VIEW.*` results the same as it did
in TMDL.

**HTML-display measures are still excluded** the same way — manual list as
the mechanism of record, a `<[a-z]` check on the `Expression` text as backstop.

**Two open gaps, not yet resolved (`docs/data-pipeline.md`):** a table-count
mismatch between the two APIs, and no confirmed source for a what-if
parameter's `range` — the Scanner API shows zero calculated tables for this
dataset, so the old TMDL parser's `GENERATESERIES(...)` capture has no
equivalent yet.

**Committed, so a schema change is a reviewable diff — and needs a redeploy**,
since it's read once at import.

## Pipeline 3 — the chunking script runs locally, first

`ML.GENERATE_EMBEDDING` reads `staging.doc_chunks`, which only exists after
`scripts/build_vector_db.py` runs. Executing the `vector_db` tag against an
empty or stale staging table yields an empty or stale index — **silently, no
error**. Order is always: chunking script → then execute the tag.

Chunk IDs are content hashes so reloads are idempotent. Loading uses
`WRITE_TRUNCATE`. **Call `load_chunks()` once, on the full combined list** —
per-file calls each truncate the last, leaving only the final file's chunks.
Won't show up on a single-file test.

**Docs only.** Page-info HTML is not chunked (`get_page_info` returns it
whole); the semantic model goes through pipeline 2; code is read live
(`docs/code-search.md`).

- `chunk_by_title` + **heading breadcrumb prepended to `chunk_text`**. Only
  `chunk_text` is embedded — a metadata column would be invisible to
  retrieval. Build the breadcrumb from the *pre-chunked* elements.
- `overlap=150` so a thought spanning a section boundary keeps its lead-in.
- **One output table, no `source_type` filter.** A second embedded source
  would get its own table, not a `WHERE` — a filter on a vector search may
  apply *after* the nearest-neighbour scan, silently returning fewer than
  `top_k`.

**Assertions** (build-time, before the app queries anything): `nonNull` on
`chunk_id`/`chunk_text`/`embedding` and `uniqueKey` on `chunk_id` in the
model's `config`, plus a non-empty assertion in its own file — an empty table
returns nothing at runtime with no error. Confirm assertion syntax at build
time.

## Ask before assuming

- Whether bronze already has `products` / `aisles` / `departments`.
