---
paths:
  - 'definitions/**'
  - 'scripts/build_vector_db.py'
  - 'scripts/build_model_context.py'
  - 'scripts/dump_schemas.sh'
  - 'tests/test_static_context.py'
  - 'tests/test_model_schema_parser.py'
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
├── sources/          declare() blocks only — no tag
├── agent_safe/       tags: ["agent_safe"]
└── vector_db/        tags: ["vector_db"]
```

Dataform compiles **one graph**, but execution runs a **selected subset**. The
two share **zero `${ref(...)}` edges**, so they're disconnected islands and a
tag runs exactly those nodes. **Never `${ref()}` across them** — that merges
the islands and breaks tag-based execution.

**Always execute by tag, never "run all."**

**This project has its own Dataform repository.** Not an extension of the
Instacart ML pipeline. Reference its tables with `declare()` in
`definitions/sources/`; **never copy its `.sqlx` files here.**

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

## Pipeline 2 — model schema, parsed not embedded

`scripts/build_model_context.py` reads the ML repo's `.pbip` via GitHub
(`github-read-token`) and writes **one committed JSON** with four keys:
`tables`, `measures`, `parameters`, `relationships`. The gateway reads it at
startup and splits it into static-context registries plus a DAX lookup
(`.claude/rules/gateway.md`).

**Two globs.** `**/definition/tables/*.tmdl` and
`**/definition/relationships.tmdl` — the latter sits one level above.

**Every table lives in the same folder. Classify on structure, never naming:**

| Kind | Signal | Contributes |
|---|---|---|
| Data table | none of the below | `tables[]` |
| Measure holder | `Binary.Decompress` placeholder partition | `measures[]` only — drop its dummy column |
| Parameter | `extendedProperty ParameterMetadata` | `parameters[]`, whole |
| Auto date table | `__PBI_LocalDateTable` or `LocalDateTable_`/`DateTableTemplate_` name | nothing — skip, in tables *and* relationships |

**"Has measures → measure table" is wrong** — parameters have measures *and* a
meaningful column.

**Parsing rules that matter:**

- **Strip trailing `formatString` / `lineageTag` / `annotation` lines — this
  is what keeps DAX bodies clean.** The cut runs to the end of the capture,
  so it also removes any following `column`/`partition`, which every measure
  table has. Don't narrow it to single lines.
- **Measure bodies also stop at the next top-level keyword** — defence-in-depth
  for a measure with no trailing metadata, which the strip above can't reach.
- **Strip triple-backtick fences.** Desktop wraps long measures in them;
  they aren't DAX, and they break markdown rendering downstream.
- **Anchor descriptions on `measure`, walk backward for `///`. Never anchor
  on `///`** — that silently skips every undescribed measure.
- **`dataType` is often absent** on calculated columns. `"unknown"` is
  accurate, not a parse failure.
- **HTML-display measures are excluded** by a `<[a-z]` check on the DAX only —
  a description merely *mentioning* html must not exclude a real measure.
  Manual list is the mechanism of record; the regex is a backstop.
- **Parameters keep `filter_column`** — that's what appears in
  `filter_context`. Their `value_measure` stays **out** of `measures[]`: its
  DAX is boilerplate `SELECTEDVALUE(...)`, nothing to look up.
- **Relationships parse to `table.column → table.column`**, GUIDs dropped.
- **Refuse to write if tables, measures, or relationships are empty.** A glob
  typo would otherwise leave the agent silently ungrounded.

**Changing any parse rule means regenerating the snapshot** (`docs/testing.md`)
and reading the diff — the only place a subtle capture change is visible.

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

- Which repo and `.pbip` paths to walk.
- Whether the TMDL regex matches the real file format (it's tab-indented; the
  leading `\s*` is required).
- Whether bronze already has `products` / `aisles` / `departments`.
