# Data Pipeline: `agent_safe` + Vector Index

Two build efforts landing in BigQuery. Do `agent_safe` first — it establishes
the Dataform project the vector index model needs.

## The three pipelines at a glance

**Three pipelines, three shapes.** Two are Dataform; one is a local parse to a
committed file. Only project docs are chunked and embedded.

| # | Source | Built by | Output | Read by |
|---|---|---|---|---|
| **1** | Instacart bronze tables | Dataform (`agent_safe` tag) | `agent_safe.*` tables | `run_bigquery_sql`, `bigquery_schema` |
| **2** | ML repo `.pbip` → `*.tmdl` | `scripts/build_model_context.py` (local, **no Dataform**) | `context/schema/model_schema.json`, committed | static context + `get_measure_dax` |
| **3** | `context/docs/*.md`, `*.html` | `scripts/build_vector_db.py` → Dataform (`vector_db` tag) | `vector_db.chunks_docs` | `search_docs` |

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

**Check the function name and model before building** — Google's docs show
both `ML.GENERATE_EMBEDDING` and a newer `AI.GENERATE_EMBEDDING`. Prefer the
built-in `embeddinggemma-300m`: it keeps data inside BigQuery with no Vertex
AI call or charge, where `text-embedding-005` sends data out and bills
separately.

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
├── sources/          declare() blocks only — no tag
├── agent_safe/       tags: ["agent_safe"]
└── vector_db/        tags: ["vector_db"]
```

**Pipeline 2 has nothing here** — it produces a committed JSON file, not a
BigQuery table, so there's no `.sqlx` and no tag to execute.

**Execute by tag, never "run all."** Running everything re-embeds the whole
corpus — a real Vertex AI bill — every time an unrelated model changes.

**Breaks only if** a `.sqlx` in one pipeline ever `${ref(...)}`s the other's
output; the islands merge and `includeDependencies`/`includeDependents` start
to matter. Don't create that edge.

**This repo has its own Dataform repository** — not an extension of the
Instacart ML pipeline. Reference its tables with `declare()`; never copy its
`.sqlx` files here.

```javascript
// definitions/sources/instacart_declarations.sqlx
declare({ database: "YOUR_PROJECT", schema: "gold",   name: "base_analytical_table" });
declare({ database: "YOUR_PROJECT", schema: "bronze", name: "products" });
declare({ database: "YOUR_PROJECT", schema: "bronze", name: "aisles" });
declare({ database: "YOUR_PROJECT", schema: "bronze", name: "departments" });
```

---

## Part 1 — `agent_safe` (Phase 0)

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
// 2. definitions/agent_safe/product_order_analysis.sqlx
config {
  type: "table",                      // always a table in agent_safe
  schema: "agent_safe",
  name: "product_order_analysis",
  tags: ["agent_safe"],
  columns: {
    product_id: "Unique product identifier.",
    product_name: "Human-readable product name — join precomputed so the agent never needs to know the dimension tables exist.",
    aisle: "Human-readable aisle name.",
    department: "Human-readable department name.",
    penetration_ratio: "Share of a user's orders containing this product, out of all their orders."
  }
}
SELECT b.*, p.product_name, a.aisle, d.department
FROM ${ref("base_analytical_table")} b
JOIN ${ref("products")}    p USING (product_id)
JOIN ${ref("aisles")}      a USING (aisle_id)
JOIN ${ref("departments")} d USING (department_id)
```

**The ML gold tables carry IDs, not names** — correct for model training, wrong
for answering *"which product has the most orders?"*. The id→name mappings
almost certainly already exist as bronze tables; this is **a new join, not new
ingestion**. Check bronze before assuming otherwise.

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

**A pipeline, but not a Dataform one.** Parse `.pbip` → one committed JSON →
read into static context at startup. No SQL, no BigQuery, no embeddings. It
shares the `.pbip` source and the manual-refresh discipline with the others,
which is why it lives here.

### The extraction script — `scripts/build_model_context.py`

Parses the same `.pbip` the chunkers used to read, into **one committed
JSON**. Rationale is in the docstring.

```python
# scripts/build_model_context.py
"""Parse the semantic model's TMDL into ONE committed json artifact.

Runs LOCALLY. Committing it keeps a parse failure off Cloud Run startup and
makes a schema change a reviewable diff.

Re-run whenever the semantic model changes, then commit the result.
"""
import json
import pathlib
import re

OUT_PATH = pathlib.Path("context/schema/model_schema.json")

# The .pbip lives in the ML repo, read via `github-read-token` — the same
# secret the code tools use. No local copy, no drift, nothing to gitignore.
# relationships.tmdl sits one level ABOVE tables/ in definition/.
TABLES_GLOB = "**/definition/tables/*.tmdl"
RELATIONSHIPS_GLOB = "**/definition/relationships.tmdl"

# Two mechanisms keep a measure's DAX clean; TRAIL is the load-bearing one.
#
# TRAIL cuts from the first metadata line to the END of the capture (`.*`
# under DOTALL). Since every real measure carries a lineageTag, that removes
# any following column/partition as a side effect — which is what actually
# stops a measure table's trailing dummy `column` landing in the last
# measure's DAX.
#
# MEASURE_RE's top-level lookahead is defence-in-depth for the case TRAIL
# can't reach: a measure with NO trailing metadata at all. Keep both.
MEASURE_RE = (r"^\s*measure\s+'?([^'\n=]+?)'?\s*=\s*(.*?)"
              r"(?=^\s*(?:measure|column|partition|hierarchy|annotation"
              r"|extendedProperty)\s|\Z)")
TRAIL = (r"\n\s*(?:formatString|lineageTag|displayFolder|isHidden|annotation"
         r"|extendedProperty)\b.*")

# Power BI's hidden auto date tables. Two signals because a file may carry
# either. Excluded from tables AND relationships — they're join paths the
# agent should never take.
SKIP_TABLE = r"__PBI_LocalDateTable|^\s*table\s+'?(?:LocalDateTable_|DateTableTemplate_)"


def fetch_tmdl_from_github(glob: str) -> list[pathlib.Path]:
    """Matching .tmdl files, written to a temp dir and returned as paths.

    Trees API for the listing, Contents API per file — same two endpoints as
    list_repo_files / read_repo_file (docs/code-search.md), and never the
    Search API, which is rate-limited and indexes lazily.
    """
    raise NotImplementedError("Trees API listing + Contents API per match.")


def _description_above(lines: list[str], start_line: int) -> str | None:
    """Walk backward over consecutive /// lines.

    TMDL's native description syntax — written in Desktop's Properties pane
    or TMDL view. Optional: an undescribed object still parses, it just gives
    the model less to go on.
    """
    i = start_line
    while i > 0 and lines[i - 1].strip().startswith("///"):
        i -= 1
    if i == start_line:
        return None
    return " ".join(l.strip().lstrip("/").strip() for l in lines[i:start_line])


def _measures(text: str, lines: list[str]) -> list[dict]:
    out = []
    for m in re.finditer(MEASURE_RE, text, re.MULTILINE | re.DOTALL):
        dax = re.sub(TRAIL, "", m.group(2), flags=re.DOTALL).strip()
        # Desktop wraps long measures in ``` fences. Not DAX, and leaving
        # them in breaks markdown rendering of the returned body.
        if dax.startswith("```"):
            dax = dax[3:].removesuffix("```").strip()
        # HTML-display measures render a string for a visual. Never
        # referenced in a SUMMARIZECOLUMNS, so listing them spends the
        # model's attention on something it should never pick. Check the DAX
        # only — a description that merely MENTIONS html must not exclude a
        # real measure.
        if re.search(r"<[a-z]+[ >]", dax, re.I):
            continue
        out.append({
            "name": m.group(1).strip(),
            "description": _description_above(lines, text[:m.start()].count("\n")),
            "dax": dax,
        })
    return out


def _columns(text: str, lines: list[str]) -> list[dict]:
    out = []
    for m in re.finditer(r"^\s*column\s+'?([^'\n]+?)'?\s*$", text, re.MULTILINE):
        blk = text[m.end():]
        nxt = re.search(r"^\s*(?:column|measure|partition)\s", blk, re.MULTILINE)
        blk = blk[:nxt.start()] if nxt else blk
        dt = re.search(r"^\s*dataType:\s*(\w+)", blk, re.MULTILINE)
        out.append({
            "name": m.group(1).strip(),
            # Calculated columns often omit dataType — "unknown" is accurate,
            # not a parse failure.
            "type": dt.group(1) if dt else "unknown",
            "description": _description_above(lines, text[:m.start()].count("\n")),
        })
    return out


def parse_table_file(path: pathlib.Path) -> tuple[str, dict | None]:
    """-> (kind, payload). kind: 'skip' | 'table' | 'measures' | 'parameter'.

    Classification keys on STRUCTURE, never on naming convention:
      - `extendedProperty ParameterMetadata` -> a what-if / field parameter
      - a `Binary.Decompress` placeholder partition -> a measure holder, whose
        lone dummy column exists only because measures need a table to live on
      - anything else -> a real data table

    "Has measures -> it's a measure table" would be wrong: parameters have
    measures AND a meaningful column.
    """
    text = path.read_text()
    lines = text.split("\n")
    if re.search(SKIP_TABLE, text, re.MULTILINE):
        return "skip", None
    tm = re.search(r"^\s*table\s+'?([^'\n]+?)'?\s*$", text, re.MULTILINE)
    if not tm:
        return "skip", None

    name = tm.group(1).strip()
    tdesc = _description_above(lines, text[:tm.start()].count("\n"))
    measures = _measures(text, lines)
    columns = _columns(text, lines)

    if re.search(r"extendedProperty ParameterMetadata", text):
        rng = re.search(r"source = (GENERATESERIES\(.*?\))\s*$", text,
                        re.MULTILINE | re.DOTALL)
        return "parameter", {
            "name": name,
            "description": tdesc,
            # The column the slicer filters — this is what appears in
            # filter_context, so without it the agent can't connect an
            # incoming filter to the parameter it represents.
            "filter_column": columns[0]["name"] if columns else None,
            "range": " ".join(rng.group(1).split()) if rng else None,
            # Its DAX is boilerplate SELECTEDVALUE — nothing to look up, so
            # this measure stays OUT of the measures list.
            "value_measure": measures[0]["name"] if measures else None,
        }

    if measures and re.search(r"Binary\.Decompress", text):
        return "measures", {"measures": measures}      # dummy columns dropped

    return "table", {
        "table": {"name": name, "description": tdesc, "columns": columns},
        "measures": measures,
    }


def parse_relationships(path: pathlib.Path) -> list[dict]:
    """from -> to, as table.column pairs. GUIDs dropped; they carry nothing.

    LocalDateTable_* relationships are excluded for the same reason their
    tables are.
    """
    out = []
    for m in re.finditer(r"fromColumn:\s*(\S+)\s*\n\s*toColumn:\s*(\S+)",
                         path.read_text()):
        frm, to = m.group(1), m.group(2)
        if "LocalDateTable_" in frm or "LocalDateTable_" in to:
            continue
        out.append({"from": frm, "to": to})
    return out


def build_artifact(table_paths: list[pathlib.Path],
                   relationships_path: pathlib.Path) -> dict:
    """Paths in, artifact out. No network, no file writing — so the test can
    call it against fixtures and cover the real assembly, not a copy of it.

    Iterate table_paths SORTED: measure order follows file order, and the
    committed artifact has to be byte-stable or every rebuild is a diff.
    """
    tables, measures, parameters = [], [], []
    for path in sorted(table_paths):
        kind, payload = parse_table_file(path)
        if kind == "table":
            tables.append(payload["table"])
            measures.extend(payload["measures"])
        elif kind == "measures":
            measures.extend(payload["measures"])
        elif kind == "parameter":
            parameters.append(payload)

    return {
        "tables": tables,
        "measures": measures,
        "parameters": parameters,
        "relationships": parse_relationships(relationships_path),
    }


def main() -> None:
    table_paths = fetch_tmdl_from_github(TABLES_GLOB)
    rel_paths = fetch_tmdl_from_github(RELATIONSHIPS_GLOB)
    artifact = build_artifact(table_paths, rel_paths[0])

    # Any of these empty means a glob matched nothing — which would otherwise
    # write a valid artifact and leave the agent silently ungrounded.
    if not all(artifact[k] for k in ("tables", "measures", "relationships")):
        raise SystemExit(
            f"Refusing to write: {len(artifact['tables'])} tables, "
            f"{len(artifact['measures'])} measures, "
            f"{len(artifact['relationships'])} relationships. Check the globs.")

    undescribed = [m["name"] for m in artifact["measures"] if not m["description"]]
    if undescribed:
        # Not fatal — but these are the measures the model is most likely to
        # misuse, since the name is all it has to go on.
        print(f"WARNING: {len(undescribed)} measures have no /// description: "
              f"{', '.join(undescribed[:5])}{'...' if len(undescribed) > 5 else ''}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(artifact, indent=2, sort_keys=True))

    cols = sum(len(t["columns"]) for t in artifact["tables"])
    print(f"Wrote {OUT_PATH}: {len(artifact['tables'])} tables ({cols} columns), "
          f"{len(artifact['measures'])} measures, "
          f"{len(artifact['parameters'])} parameters, "
          f"{len(artifact['relationships'])} relationships.")
    print("Commit it — the gateway reads this at startup, not the .pbip.")


if __name__ == "__main__":
    main()
```

**`build_artifact` takes paths instead of fetching and guarantees sort order**,
so the snapshot test can point the same assembly at fixtures and get a
byte-stable result (`docs/testing.md`).

**Every `.tmdl` lives in one folder** (`definition/tables/`), with
`relationships.tmdl` one level above. There's no tables-vs-measures split to
rely on — and don't create one by reorganizing the `.pbip`, which Desktop
regenerates.

**Classify on structure, never on naming.** Four kinds come out of that
folder:

| Kind | Signal | Contributes |
|---|---|---|
| Data table | none of the below | `tables[]` (name, description, columns) |
| Measure holder | `Binary.Decompress` placeholder partition | `measures[]` only — its lone dummy column is dropped |
| Parameter | `extendedProperty ParameterMetadata` on a column | `parameters[]`, whole |
| Auto date table | `__PBI_LocalDateTable`, or a `LocalDateTable_` / `DateTableTemplate_` name | nothing — skipped |

**"Has measures → it's a measure table" would be wrong.** Parameters have
measures *and* a meaningful column; that rule would destroy them.

**Output shape:**

```json
{
  "tables":   [{"name": "evaluation_metrics", "description": "...",
                "columns": [{"name": "Recall_at_5", "type": "double",
                             "description": "..."}]}],
  "measures": [{"name": "Recall", "description": "Out of the total reorders...",
                "dax": "SUM(evaluation_metrics[Recall_at_5])"}],
  "parameters": [{"name": "AOV Lift per 10% Recall",
                  "filter_column": "AOV Lift per 10% Recall",
                  "range": "GENERATESERIES(CURRENCY(0.00), CURRENCY(0.26), CURRENCY(0.01))",
                  "value_measure": "AOV Lift per 10% Recall Value"}],
  "relationships": [{"from": "evaluation_metrics.Model",
                     "to": "models_dimension.Model"}]
}
```

**Parameters go into context whole, not split across registries.**
`filter_column` is what appears in `filter_context` — without it the agent
can't connect an incoming filter to its parameter. `range` supplies valid
bounds, which matters because these tables carry no `///` descriptions. Their
`value_measure` stays **out** of `measures[]`: boilerplate
`SELECTEDVALUE(...)`, nothing to look up.

**Relationships are parsed, not copied** — the raw file is mostly GUIDs plus
auto-generated `LocalDateTable_*` joins the agent should never take. This
replaces the old manual copy into `context/tmdl/`, the one piece of
semantic-model context that wasn't script-produced.

**How the gateway splits it** — registries plus a DAX lookup, all from one
parse at startup (`.claude/rules/gateway.md`). The `dax` field is the only
part *not* in static context; it's returned solely by `get_measure_dax`.

### Excluding HTML-display measures from the registry

Some Power BI measures return a rendered HTML/markdown string for a visual
rather than an aggregation. **They must not reach the measure registry.** The
registry is what the model picks measure names from; a display measure is
never referenced in a `SUMMARIZECOLUMNS` the way `[Recall at 5]` is, so
listing it spends attention on something the model should never choose — and
`strict=True` would accept it as a valid `get_measure_dax` argument.

**Primary mechanism: manual exclusion.** Keep a small list of measure names to
skip, or simply don't include those `.tmdl` files in the walked paths. Only a
handful exist and they change rarely.

```python
EXCLUDED_MEASURES = {"Page Info HTML", "..."}   # confirm real names from the .pbip
```

**Backstop: the `re.search(r"<[a-z]+[ >]", ...)` check in `parse_measures`**
above. Not a replacement for the list — a cheap guard so a *newly added* HTML
measure doesn't silently slip in before anyone updates it. If the regex ever
fires on a measure that isn't display-only, drop the check rather than
contorting it; the manual list is the mechanism of record.

**Their content isn't lost** — the same page-info HTML lives in
`context/page_info/`, returned whole by `get_page_info`.

**Ask before assuming:** which repo/`.pbip` paths to walk, and whether the TMDL
regex matches the real file format.

### Refreshing the artifact

```bash
# Semantic model changed (measure renamed, column added, description edited):
python scripts/build_model_context.py   # rewrites context/schema/model_schema.json
git add context/schema/model_schema.json && git commit    # the app reads THIS
# then redeploy — it's read once at startup, not per request
```

**No embedding, no Dataform, no BigQuery.**

### Staleness has no signal

Rename a measure in Desktop, forget to rerun the script, and the registry
advertises a name `get_measure_dax` will reject. Loud rather than silent, but
still wrong.

**Fix: stamp the build and check it.**

```python
# Written once per successful build, in the same script.
build_metadata = {"model_schema_last_built": datetime.now(timezone.utc)}
```

Print it at the top of every run against the `.pbip`'s last-modified time — if
the model changed more recently, say so before proceeding.

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
    source_type: str       # "docs" — kept as a field because the schema is
                           # shared with future sources, not because it filters
    file_path: str
    symbol_name: str | None
    start_line: int | None
    end_line: int | None
    chunk_text: str
```

| `source_type` | Table | Sources | Feeds |
|---|---|---|---|
| `docs` | `vector_db.chunks_docs` | Project methodology — the README, minus what's in static context. Heading breadcrumb prepended | `search_docs` |

**One output table, not one-with-a-filter.** Should a second embedded source
ever be added, give it its own table rather than a `WHERE source_type = ...`:
a `WHERE` on a vector search may be applied *after* the nearest-neighbor scan
rather than pushed into it, so `top_k => 5` could return fewer than 5 — good
matches discarded because the unfiltered top-5 happened to be other content
types. Separate tables sidestep the question entirely.

**`chunks_docs` is small enough to skip `CREATE VECTOR INDEX`
entirely.** `VECTOR_SEARCH` works without an index — a brute-force exact scan.
IVF indexes exist to make that fast at scale and have minimum row counts below
which they aren't usable. The project README chunks to tens of rows, not
thousands. **Check actual row counts before building any index**; brute force
is the correct default here, not a fallback.

**Exclude the orientation content** (Executive Summary, Project Navigator,
architecture diagram) from `docs` chunking — it's always in context already, so
indexing it wastes a retrieval slot.

### Chunkers

```python
from unstructured.partition.html import partition_html
from unstructured.partition.md import partition_md
from unstructured.chunking.title import chunk_by_title
import hashlib, pathlib, re

def _chunk_id(file_path: str, text: str) -> str:
    """Content-addressed → identical ids for unchanged content, so reloads
    are idempotent instead of duplicating rows."""
    return hashlib.sha256(f"{file_path}:{text}".encode()).hexdigest()[:16]

def chunk_docs(path: pathlib.Path, source_type: str = "docs") -> list[Chunk]:
    """Structure-aware — respects headings so a chunk is a coherent section.
    Same logic for both doc corpora; source_type decides the target table.

    Prepends the heading breadcrumb to chunk_text. A chunk under
    "### CI/CD Authentication" reading "we use Workload Identity Federation
    instead" is meaningless once separated from its parent "## Trade-offs".
    """
    partition = partition_html if path.suffix == ".html" else partition_md
    elements = partition(filename=str(path))

    # Build a heading stack from the PRE-chunked elements. chunk_by_title
    # decides *where* to break; it does not hand back a breadcrumb — the
    # library treats chunking as a downstream consumer of elements, so the
    # stack has to be tracked here. parent_id/category_depth are most
    # reliable on HTML (native heading structure).
    breadcrumbs, stack = {}, []
    for el in elements:
        depth = (el.metadata.category_depth or 0)
        if el.category == "Title":
            stack[depth:] = [el.text]          # push, truncating deeper levels
        breadcrumbs[el.id] = " > ".join(stack)

    chunks = []
    for c in chunk_by_title(elements, max_characters=1500, overlap=150):
        # Breadcrumb goes in chunk_text, NOT a metadata column — only
        # chunk_text reaches ML.GENERATE_EMBEDDING. A column would be
        # invisible to retrieval.
        crumb = breadcrumbs.get(getattr(c, "id", None), "")
        text = f"[{crumb}]\n{c}" if crumb else str(c)
        chunks.append(Chunk(
            chunk_id=_chunk_id(str(path), text), source_type=source_type,
            file_path=str(path), symbol_name=crumb or None,
            start_line=None, end_line=None, chunk_text=text))
    return chunks
```

**Two things to confirm in `chunk_docs` before trusting it.** (1) The exact
attribute names — `element.id`, `metadata.category_depth`, and the `overlap`
parameter's name — are version-dependent; verify against the installed
`unstructured`. (2) Whether `chunk_by_title`'s output objects expose an `id`
that matches a source element's. If they don't, map by element *order*
instead of id — the breadcrumb lookup is the only part that depends on it.
**Both fail silently**: chunks still build, just with no breadcrumb.

**Why the breadcrumb is prepended rather than stored in a column:** only
`chunk_text` reaches `ML.GENERATE_EMBEDDING` (`file_path`/`symbol_name` ride
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
                bigquery.SchemaField("source_type", "STRING"),
                bigquery.SchemaField("file_path", "STRING"),
                bigquery.SchemaField("symbol_name", "STRING"),
                bigquery.SchemaField("start_line", "INTEGER"),
                bigquery.SchemaField("end_line", "INTEGER"),
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

CHUNKER_BY_DIR = {
    # context/docs/ = project methodology (README). The page-info HTML is
    # NOT chunked — too small to rank; served whole by get_page_info.
    "docs":      (lambda p: chunk_docs(p, "docs"),      ("*.html", "*.md")),
}

# NOT here, and why:
#   TMDL                -> scripts/build_model_context.py instead. The semantic
#                          model is small and fully enumerable, so it goes in
#                          static context, not behind a retrieval call.
#   relationships.tmdl  -> build_model_context.py parses it into the same
#                          artifact. Join paths are structural and needed on
#                          nearly every DAX composition — never retrieved.
#   page_info HTML      -> served whole by get_page_info.
#   ML repo .py / .sqlx -> read live via list_repo_files / read_repo_file.

def main() -> None:
    all_chunks: list[Chunk] = []

    for subdir, (chunker, patterns) in CHUNKER_BY_DIR.items():
        for pattern in patterns:
            for path in (CONTEXT_ROOT / subdir).rglob(pattern):
                all_chunks.extend(chunker(path))

    # Empty means the glob matched nothing — which would build an EMPTY
    # chunks_docs with no error anywhere, and search_docs would silently
    # return nothing on every call.
    if not all_chunks:
        raise SystemExit("Refusing to load: 0 chunks. Check context/docs/.")

    print(f"Chunked {len(all_chunks)} pieces from context/docs/.")
    load_chunks(all_chunks)   # one call, one WRITE_TRUNCATE — see above
    print("Loaded to staging.doc_chunks. Now execute the 'vector_db' tag in Dataform.")

if __name__ == "__main__":
    main()
```

**This is the exact command referenced throughout this doc's refresh sequence**
(below) — `python scripts/build_vector_db.py` — now with a concrete
implementation behind it rather than a bare command name.

### Embedding model + index

```sql
-- one-time: BigQuery → Vertex AI connection
-- bq mk --connection --location=YOUR_REGION --connection_type=CLOUD_RESOURCE vertex_conn
-- then grant that connection's service account roles/aiplatform.user

CREATE OR REPLACE MODEL `YOUR_PROJECT.staging.embedding_model`
REMOTE WITH CONNECTION `YOUR_PROJECT.YOUR_REGION.vertex_conn`
OPTIONS (ENDPOINT = 'text-embedding-005');   -- verify current model name
```

**One model.** It embeds everything in staging — there's a single source now,
so no `source_type` filter is needed at build time or search time.

```sql
-- definitions/vector_db/chunks_docs.sqlx
config {
  type: "table", schema: "vector_db", name: "chunks_docs",
  tags: ["vector_db"],
  assertions: {
    nonNull: ["chunk_id", "chunk_text", "embedding"],
    uniqueKey: ["chunk_id"]
  }
}

SELECT chunk_id, source_type, file_path, symbol_name,
       start_line, end_line, chunk_text,
       ml_generate_embedding_result AS embedding
FROM ML.GENERATE_EMBEDDING(
  MODEL `YOUR_PROJECT.staging.embedding_model`,
  (SELECT *, chunk_text AS content FROM ${ref("staging", "doc_chunks")}),
  STRUCT(TRUE AS flatten_json_output))
```

It carries the `vector_db` tag, so the refresh workflow below is unchanged.

### Assertions — catch bad builds before the app ever queries them

**Build-time safety net, not a runtime guardrail.** A failed assertion fails
the Dataform run; it doesn't stop the app querying a stale table from an
earlier good build. Complementary to the manual gates in
`docs/success-criteria.md`, which only fire when someone runs a search.

`nonNull` and `uniqueKey` above cover two of three checks:

| Check | Catches |
|---|---|
| `nonNull: [embedding, ...]` | A *per-row* null embedding in an otherwise-successful batch — a wholesale failure is loud, one bad row isn't |
| `uniqueKey: [chunk_id]` | Content-hash not actually varying with content — breaks reload idempotency |

The third needs its own file:

```sql
-- definitions/vector_db/assert_chunks_docs_not_empty.sqlx
-- An empty table fails SILENTLY at runtime: search_docs just returns
-- nothing, no error anywhere.
config { type: "assertion", tags: ["vector_db"] }
SELECT 1 FROM (SELECT COUNT(*) AS n FROM ${ref("chunks_docs")})
WHERE n = 0
```

**Confirm assertion syntax at build time** — same category as
`query_job.result()`'s parameter name.

**Indexes are optional and probably unnecessary** — see the row-count note
above. Only if a table proves large enough to need one:

```sql
CREATE VECTOR INDEX chunks_docs_idx
ON `YOUR_PROJECT.vector_db.chunks_docs`(embedding)
OPTIONS(index_type = 'IVF', distance_type = 'COSINE');
```

### What `search_docs` runs

One parameterized query against the single table. **No `WHERE` clause** —
there's one content type.

```python
SEARCH_SQL = """
SELECT base.chunk_text, base.file_path, base.symbol_name, distance
FROM VECTOR_SEARCH(
  TABLE `{project}.vector_db.chunks_docs`, 'embedding',
  (SELECT ml_generate_embedding_result AS embedding
   FROM ML.GENERATE_EMBEDDING(
     MODEL `{project}.staging.embedding_model`,
     (SELECT @query AS content),
     STRUCT(TRUE AS flatten_json_output))),
  top_k => @top_k, distance_type => 'COSINE')
"""
```

**`search_docs` is the only vector-search tool.** Schema comes from static
context and `get_measure_dax` (`.claude/rules/mcp-tools.md`), code search is
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
