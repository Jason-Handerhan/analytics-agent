---
paths:
  - 'app/mcp_server/**'
  - 'app/orchestrator/tools.py'
  - 'app/model_schema.py'
  - 'tests/test_tools.py'
  - 'tests/test_chart_tool.py'
  - 'tests/test_combine_tool.py'
  - 'tests/test_guardrails.py'
---

# Tools: inventory, contracts, data access boundary

> **Deep dives:** `docs/chart-tool.md` before touching `chart_tool.py` — it
> has the full spec hierarchy, all 10 chart types, and the styling contract.
> `docs/combine-tool.md` before touching `combine_tool.py` or its stand-in.
> `docs/data-pipeline.md` for how `agent_safe` and the vector index are built.
> `docs/data-pipeline.md` for the access-boundary reasoning.

## Where each tool runs — not all of them are MCP-hosted

**The defining rule: MCP hosting declares a tool safe and useful for someone
outside this project to pick up and run as-is.** Anything that will only
ever execute as `agent-sa`, for this project, doesn't belong on the server no
matter how clean its contract looks. The five exceptions below are each an
instance of this, not five independent rules.

| Tool | Hosted on the MCP server? |
|---|---|
| `run_dax_query` | Yes |
| `list_repo_files`, `read_repo_file` | Yes |
| `generate_chart` | Yes |
| `combine_results` | Yes (stand-in schema bound to the model; `docs/combine-tool.md`) |
| `run_bigquery_sql` | **No** — a plain function in the orchestrator |
| `submit_answer` | **No** — a plain function in the orchestrator |
| `get_measure_dax` | **No** — a plain function in the orchestrator |
| `search_docs` | **No** — a plain function in the orchestrator |
| `get_page_info` | **No** — a plain function in the orchestrator |

**`run_bigquery_sql` is one exception, deliberately.** Its safety depends
on state that spans more than one call — the dry-run threshold applies to a
whole *batch*, the absolute cap is cumulative across the *entire
conversation*, and cancellation is wired to the gateway's turn timeout
(`.claude/rules/orchestrator.md`). None of that is expressible as a
self-contained MCP tool contract: an external caller would have to
replicate our exact budget-tracking and cancellation plumbing to use it
safely at all. Every other tool's guardrails (row caps, schema constraints)
are enforceable *inside a single call*, with no memory of anything outside
it — that's what makes them safe to expose over MCP as-is.

**`submit_answer` is another, for a different reason: it isn't a data
tool at all.** It's how the model delivers its final answer and numeric
claims (`.claude/rules/orchestrator.md`) — control flow internal to this
graph, not something an external MCP client would ever have a reason to
call. See its own section below.

**`get_measure_dax` is a third exception, for a third reason: it isn't
generically reusable at all.** Unlike `run_dax_query` (a real wrapper
around Power BI's own API, usable against any dataset), `get_measure_dax`
is a pure lookup against `MEASURE_DAX` — a dict built at startup from *this
project's own committed* `model_schema.json`. A different deployment of
this MCP server, pointed at a different semantic model, would need this
project's exact schema file to make it work at all, so hosting it
generically alongside `run_dax_query` would misrepresent it as portable
infrastructure when it's really project-specific application logic
(2026-09-27 reusability discussion, see `run_dax_query`'s own note above).

**`search_docs` is a fourth exception, and a deliberate deviation from how
this tool was originally documented here** (2026-10-01) — it was specified
as MCP-hosted before being built, then moved local once the reasoning was
worked through against a real implementation. For `run_dax_query`/
`generate_chart` to be reusable, the hidden args a different deployer
supplies are pure *identity/connection* info (`access_token`, `workspace_id`,
`dataset_id`) — the model still writes 100% of the real query logic.
`search_docs` doesn't clear that bar: reaching it would mean hiding the
table name, the embedding column, the full embedding-model resource path,
*and* the output column list — not connection info, the tool's entire
behavioral shape. A reuser would already have had to build the tool
themselves to supply all of that, leaving nothing of standalone value in
hosting it. It's also impersonation-backed (`vector-search-sa`, below), and
a live `Credentials`/`bigquery.Client` object can't cross MCP's JSON-only
transport anyway — `run_dax_query` already proves the alternative
(`access_token` minted outside the tool as a plain string), but being local
sidesteps needing that pattern here at all.

**`get_page_info` is a fifth exception, same reusability gap as
`get_measure_dax`.** It's a pure lookup against `context/page_info/`'s two
committed files — a different deployment would need this project's exact
page names and content to make it work at all, not just connection info.
Full design (including a real deviation from how this was originally
sketched) in its own section below.

`dispatch_tool` and `call_tool_node` (`docs/approval-workflow.md`,
`.claude/rules/orchestrator.md`) handle both kinds of tool through the same
invocation shape regardless of where they run — hosting location changes
nothing about how a tool is called or how its result comes back.

## Transport: `streamable_http` over localhost — never stdio

**Decided, with a specific reason: stdio cannot cross a container boundary
at all.** It works by the client spawning the server as a child process and
talking over its pipes — a 1:1, same-machine relationship with no network
variant. That rules it out categorically for the intended production shape
(public gateway → private orchestrator → private MCP server as separate Cloud
Run services), not merely making it suboptimal. Its concurrency story is also
weak: no shared connection pooling, so N concurrent clients means N
subprocesses or serializing through one.

**`streamable_http`, not the older HTTP+SSE.** SSE's separate long-lived
server→client stream wants to stay pinned to one backend instance, which
fights Cloud Run's autoscaling model. `streamable_http` consolidates both
directions onto one endpoint and is the current recommended remote transport.

**Point it at `http://localhost:PORT` for now.** Still co-located in one
container — no new infrastructure, no new IAM. But because HTTP behaves the
same across `localhost` or a real network, **relocating the MCP server later
is a URL change, not a transport rewrite.** stdio could never offer that
migration path; it has no URL to swap. Choosing this now buys the option
without paying for the container split yet.

**Client config — verified against `langchain-mcp-adapters`:**

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from app.config import MCP_SERVER_HEADERS, MCP_SERVER_NAME, MCP_SERVER_URL

client = MultiServerMCPClient({
    MCP_SERVER_NAME: {"transport": "streamable_http", "url": MCP_SERVER_URL,
                       "headers": MCP_SERVER_HEADERS},
})
tools = await client.get_tools()
```

Three details that bite:
- **`"http"` is what the current README uses**; `"streamable_http"` is also
  accepted. **`"streamable-http"` (hyphen) is rejected at runtime** —
  the valid set is `stdio`, `sse`, `websocket`, `streamable_http`.
- **The URL needs the `/mcp` path suffix**, not just host:port.
- Custom headers go in a `headers` dict on the same entry, if the server ever
  moves off localhost and needs auth.

## Defining the server

One `FastMCP` instance, one module per tool, registered with `@mcp.tool()`.
The transport chosen above is an argument to `run()` — the same string the
client config expects.

```python
# app/mcp_server/server.py
from fastmcp import FastMCP

from app.config import MCP_SERVER_NAME

mcp = FastMCP(MCP_SERVER_NAME)

# Importing each module runs its @mcp.tool() decorators, which is what
# registers the tools. Import for side effect only — nothing is called here.
# run_bigquery_sql, submit_answer, get_measure_dax, search_docs, and
# get_page_info are NOT here — all five are plain functions in the
# orchestrator, not MCP-registered (see the hosting table above).
# bigquery_schema isn't here either — it's not exposed via MCP at all, see
# the section below.
from app.mcp_server import (          # noqa: F401,E402
    dax_tool,                         # run_dax_query
    code_search,                      # list_repo_files, read_repo_file
    chart_tool,                       # generate_chart
)

if __name__ == "__main__":
    mcp.run(transport="streamable_http", port=PORT)
```

**Group by shared machinery, not one file per tool.** `list_repo_files` and
`read_repo_file` hit the same GitHub client. Splitting those apart
duplicates setup for no gain.

**FastMCP builds each tool's schema from its type hints and docstring** — the
docstring is what the model reads when choosing a tool, so write it for that
reader, not as an implementation note.

**Co-located with the gateway for now** (same container, same process start),
which is why `localhost` works. Splitting it into its own Cloud Run service
later changes the URL and adds auth headers — not this file.

## Tool inventory

| Tool | Purpose | Authority |
|---|---|---|
| `search_docs` | Project methodology — the README's approach/evaluation narrative | Authoritative for *intent*, **never** numbers |
| `get_page_info` | What a Power BI page shows — returns the page-info text **whole**, no search | Authoritative for dashboard content, **never** numbers |
| `run_bigquery_sql` | Warehouse numbers | Only authority for raw/warehouse numbers |
| `run_dax_query` | Semantic-model numbers | Only authority for measure-level numbers |
| `get_measure_dax` | One measure's DAX body, by exact name | The *how* behind a measure; names come from the registry in static context |
| `list_repo_files` | Paths + descriptions for the ML repo (Trees API, one call) | Entry point for any code question |
| `read_repo_file` | One file's contents from the ML repo (Contents API) | Authoritative for implementation detail |
| `generate_chart` | Visualization | Renders already-fetched data; **never** a number source |
| `combine_results` | Recombines earlier results (stack or join) into a new ref | Recombines already-fetched numbers; a left-join null filled with 0 is a numeric source (`docs/combine-tool.md`) |

`bigquery_schema` isn't in this table — it's not an MCP tool or resource at
all, see below. `submit_answer` isn't either — it's not a number *source*,
it's how the model delivers the answer built from everything above; see its
own section below.

**Nine tools, one vector store** — deliberately not 1:1:

- **Code search is agentic (list + read), not vector search.** Embeddings
  flatten the structural relationships — imports, call graphs — that code
  questions depend on. The two GitHub tools are also domain-agnostic: a third
  code domain needs no third tool. Full reasoning in `docs/code-search.md`.
- **`get_page_info` doesn't search at all.** Two pages, 4–5 sections each —
  small enough that ranking adds nothing over returning the whole page.
  No chunking, no embedding, no BigQuery. Details below.
- **The semantic model isn't searched at all.** Table schema and the measure
  registry (names + descriptions) are in static context on every turn, so
  discovery is deterministic — the model cannot fail to find a measure that
  exists, and `strict=True` stops it naming one that doesn't.
  `get_measure_dax` fetches only the *formula*, which is the large part and
  the rarely-needed one: referencing `[AOV]` in a `SUMMARIZECOLUMNS` requires
  knowing the measure exists, not how it's computed.

## The data access boundary

**`agent_safe` is the only dataset `agent-sa` can read** — an IAM boundary, not
a convention. Three layers, each a different job:

1. **IAM on `agent_safe`** — the boundary, and the only enforcement.
   `run_bigquery_sql` runs as `agent-sa`, scoped to `agent_safe`;
   `search_docs` impersonates `vector-search-sa`, scoped to `vector_db`.
   Neither can reach the other's dataset (`docs/data-pipeline.md`).
2. **`get_bigquery_schema()` (`.claude/rules/orchestrator.md`) only ever reads
   `agent_safe`** — the agent shouldn't be composing queries against tables
   it can't see, so IAM is a backstop rather than the thing shaping
   behaviour.
3. **Prompt guidance** — steering only, never enforcement.

**Domain split — BigQuery and DAX must not overlap:**
- **DAX** answers what the dashboard displays: model-eval metrics, financial
  impact — computed from *post-model* results.
- **BigQuery** answers everything upstream: ML feature dataset,
  `base_analytical_table`, ad hoc warehouse questions (sparsity, order counts
  by department).
- Pre-modeling tables are safe in `agent_safe` precisely because Power BI never
  surfaces anything computed from them.
- **Still to finalize:** exactly which results tables get excluded. Ask.

**Column descriptions live on the `agent_safe` objects, not source tables** — the
service account can't read `INFORMATION_SCHEMA` on datasets it has no grant on,
so descriptions there would be invisible to `bigquery_schema`.

## Both query tools — the shared return contract

**Both query tools return `list[dict]` — one dict per row, coerced at the
source.** Normalizing here means every downstream consumer (the model,
`verify_response`, `generate_chart`, telemetry) sees one shape regardless
of which tool produced it. Two coercions are needed, and they differ by
source:

- **BigQuery** returns typed Python objects — `datetime`, `date`, and
  `Decimal` for NUMERIC. **Convert `Decimal` to `float`**: it isn't a
  Firestore type, and it isn't JSON-serializable. Datetimes pass through —
  Firestore stores them natively.
- **DAX** returns already-parsed JSON, so dates arrive as **ISO strings**,
  not datetimes (JSON has no date type). Parse them, so a line chart gets a
  real time axis instead of unordered string categories.

**Serialization to the model is automatic** — FastMCP JSON-encodes a
non-string return into a `TextContent` block. Don't hand-format: the model
reads the same JSON that `verify_response` walks and telemetry stores, and
any divergence between those is where a verification bug would hide.
Real results are 5–20 rows typically, so the repeated-keys overhead of JSON
isn't worth optimizing away.

## `run_bigquery_sql`

**Not MCP-hosted** (see the hosting table above) — a plain LangChain `@tool`
defined in the orchestrator. Everything below is still accurate; only *how*
it's invoked differs from the MCP tools further down this file. Dispatch
mechanics (the `@tool` definition, `.ainvoke()`, `ToolMessage` construction)
live in `docs/approval-workflow.md` and `.claude/rules/orchestrator.md` —
not duplicated here.

- **Dry-run first, then three tiers by BYTES scanned** — not two. Evaluated
  in `call_tool_node` on the **summed batch**, not per call. Under
  `PENDING_APPROVAL_THRESHOLD` runs automatically (unless the query uses
  `TABLESAMPLE`, which always routes to approval regardless of the estimate
  — its dry-run bytes have repeatedly underestimated actual billed bytes);
  over the threshold also routes to approval; at or over the **absolute
  cap** (the turn's cumulative `bytes_consumed`) hard-declines with
  `cost_cap_exceeded` and offers no approval at all. Full logic in
  `docs/approval-workflow.md`; `maximum_bytes_billed` stays set as the
  engine-level fail-safe underneath all of it.
- On the approval path, the generated SQL becomes `pending_query` and the
  derived dollar figure becomes `estimated_cost` — the human needs both,
  not just the price.
- Per-tool timeout (**40s**) with **explicit** `client.cancel_job(job.job_id)`
  — `asyncio` cancellation alone doesn't stop a running BigQuery job.
- `bigquery.Client()` needs no explicit credentials; ADC resolves to `agent-sa`
  on Cloud Run and to your personal identity locally, with no code branching.
- **No tool-layer table allow-list — the boundary is IAM.** `run_bigquery_sql`
  executes under `agent-sa`, which holds `dataViewer` on `agent_safe` only.
  A query against `vector_db.*` fails at BigQuery, not at a check we wrote.
  An allow-list would mean correctly extracting table references from
  arbitrary SQL — CTEs, subqueries, aliases, nested joins — and a parsing bug
  is a boundary you *believe* is enforced but isn't. See
  `docs/data-pipeline.md` for the two-identity setup.
- **Report the generated SQL back as `query_text`**, not just the result —
  telemetry needs both (`.claude/rules/telemetry.md`). Whatever return shape
  carries `result` back to the orchestrator should carry the query
  alongside it.
- **Row cap, orthogonal to the cost gate above — `LIMIT` doesn't help here,
  confirmed, not assumed.** BigQuery scans full columns before applying
  `LIMIT`, so a cheap, well-filtered query can still return tens of
  thousands of rows with nothing in the cost check to catch it. Cap what's
  *fetched*, not the SQL text:
  ```python
  # ONE call — result() is an iterator; calling it twice re-fetches, and the
  # second call without max_results would defeat the cap entirely.
  result = query_job.result(max_results=1000)  # verify exact param at build time
  if result.total_rows > 1000:                 # total_rows = what the query WOULD return
      raise ToolError(
          "Query returned 1,000+ rows. Add a filter or aggregate to narrow it."
      )
  rows = list(result)
  ```
  Fail loudly with the actionable message above, not a silent truncation —
  the agent needs to know the result was incomplete, the same instinct as
  `iteration_cap_hit`.

## `submit_answer` — the structured-answer tool

Not MCP-hosted, for the same reason given above — control flow internal to
this graph, not something an external client would call. Contract only;
dispatch/routing/verification mechanics are `.claude/rules/orchestrator.md`.

**Field descriptions and the docstring carry the real formatting contract —
deliberately, not left thin.** They were migrated here from the system
prompt's old `RESPONDING` section (2026-09-29): a tool's own schema is more
salient to the model at the moment it's deciding how to call that tool than
equivalent prose sitting in the static system block, confirmed via live
transcripts during that migration. `context.py`'s `RESPONDING` section is now
just a pointer to this contract, not a restatement of it — don't duplicate
detail back into the system prompt when tuning this further.

```python
_markdown_ast = mistune.create_markdown(renderer="ast", plugins=["table"])


def _looks_like_table_separator(line: str) -> bool:
    trimmed_line = line.strip()
    # True only if the line has a pipe and is otherwise just dashes/colons/whitespace
    return "|" in trimmed_line and len(trimmed_line.strip("|-: \t")) == 0


def _check_ragged_table(markdown: str) -> bool:
    """True if the text looks like it has a table but mistune didn't parse one out."""
    has_separator_line = any(_looks_like_table_separator(l) for l in markdown.split("\n"))
    has_real_table = any(block.get("type") == "table" for block in _markdown_ast(markdown))
    return has_separator_line and not has_real_table


def _check_unclosed_fence(markdown: str) -> bool:
    """True if a ``` code fence was opened but never closed."""
    fence_count = sum(1 for line in markdown.split("\n") if line.strip().startswith("```"))
    return bool(fence_count % 2)


class SubmitAnswerArgs(BaseModel):
    answer_markdown: str = Field(
        default="",
        description=f"Plain Markdown. Empty only when clarifying_question is set. Keep any single table to at most {MAX_ANSWER_TABLE_ROWS} rows -- "
                    "show the top results and summarize the rest, aggregate to fewer rows in a new query, "
                    "or use generate_chart instead. Escape a literal | inside a table cell as \\| or "
                    "it's read as an extra column and breaks the table. Never state a hand-rolled "
                    "approximation, range, or derived value (a rank, percent change, difference, ratio, "
                    "average) anywhere, in prose or a table -- add it to the query and re-run, or "
                    "describe the pattern in words with no number. Only run_bigquery_sql/run_dax_query "
                    "numbers belong in a table at all. A number from any other tool may be stated in "
                    "prose, except search_docs -- never state a search_docs number anywhere, in prose "
                    "or a table. Table cell values are checked automatically against this turn's "
                    "run_bigquery_sql/run_dax_query results, at up to "
                    f"{MAX_CLAIM_PRECISION} decimal places. Format numbers for readability -- "
                    "thousands separators (977,542) and 1-2 decimal places by default, since "
                    "verification matches each number at its own precision and rounding never "
                    "causes a mismatch. Use more decimals only when the figure itself needs it, "
                    "e.g. a Recall@5 score (0.6688, not 0.67). Include the chart_url from "
                    "generate_chart here as an image -- the user sees no chart otherwise.")
    all_prose_numeric_claims: list[float] = Field(
        # prose numbers only -- table cells are checked separately by
        # extract_table_values, not re-declared here
        description="Every number stated in prose as fact from a run_bigquery_sql or run_dax_query "
                    "result this turn -- the only two tools checked against. Never include a number "
                    "already in a markdown table -- checked separately. Never include a number from "
                    "any other tool -- it will fail verification.")
    suggested_follow_ups: list[str] = Field(
        default=[],
        description="1-3 short, natural follow-up questions -- include these by default, since "
                    "they help the user continue the conversation. Leave empty only when nothing "
                    "natural genuinely fits or clarifying_question is set.")
    clarifying_question: str = Field(
        default="",
        description="Set this INSTEAD of answer_markdown, and leave answer_markdown empty, only when "
                    "the request is genuinely ambiguous and a wrong guess would mislead the user. Ask "
                    "one short question naming the specific choice. Otherwise answer -- do not ask "
                    "about anything a reasonable default covers.")

    @field_validator("answer_markdown")
    def _unescape_newlines(cls, value: str) -> str:
        """Replaces a literal backslash-n with a real newline."""
        return value.replace("\\n", "\n")

    @field_validator("answer_markdown")
    def _strip_leaked_tool_call_tail(cls, value: str) -> str:
        """Drops a leaked </answer_markdown> tag and everything after it."""
        return value.split("</answer_markdown>")[0].rstrip()

    @field_validator("answer_markdown")
    def _check_markdown_structure(cls, value: str) -> str:
        """Rejects a ragged table or an unclosed code fence instead of
        letting either reach the user silently broken."""
        ragged_table = _check_ragged_table(value)
        unclosed_fence = _check_unclosed_fence(value)
        if ragged_table and unclosed_fence:
            raise ValueError(
                "A markdown table looks malformed, and a ``` code fence was opened but never "
                "closed. Every table row needs the same number of '|'-separated cells as the "
                "header, and every fence needs a matching close.")
        elif ragged_table:
            raise ValueError(
                "A markdown table looks malformed -- every row needs the same number of "
                "'|'-separated cells as the header.")
        elif unclosed_fence:
            raise ValueError("A ``` code fence was opened but never closed.")
        return value

    @model_validator(mode="after")
    def _exactly_one_of_answer_or_question(self) -> "SubmitAnswerArgs":
        """Requires exactly one of answer_markdown or clarifying_question."""
        has_answer = bool(self.answer_markdown.strip())
        has_question = bool(self.clarifying_question.strip())
        if has_answer and has_question:
            raise ValueError("Set only one of answer_markdown or clarifying_question, not both.")
        if not (has_answer or has_question):
            raise ValueError(
                "Set exactly one of answer_markdown (your final answer) or clarifying_question "
                "(one question for the user, only when the request is genuinely ambiguous).")
        return self

@tool(args_schema=SubmitAnswerArgs)
async def submit_answer(answer_markdown: str, all_prose_numeric_claims: list[float],
                         suggested_follow_ups: list[str], clarifying_question: str = "") -> str:
    """Call this with your final answer once you have everything you need or
    with a clarifying question when the request is genuinely ambiguous.
    This is how you respond to the user -- a plain-text reply will not be
    delivered and the turn will be asked to retry. If you can't fully
    answer within a reasonable number of steps, submit the best partial
    answer available and say plainly it's partial, rather than presenting
    it as complete. Do not batch this with other tool calls -- if you do,
    the submission is ignored and the loop continues.

    Don't omit a number from all_prose_numeric_claims to dodge verification
    -- it still needs a real run_bigquery_sql or run_dax_query source.
    Verification exists to catch hallucinated or hand-computed numbers, not
    to be routed around.
    """
    return "Answer recorded."
```

**Clarifying question.** `clarifying_question` is the second way to end a
turn, and exactly one of it or `answer_markdown` must be set (the model
validator enforces this). A question turn is normalized in `call_tool_node`
(`.claude/rules/orchestrator.md`): the question is copied into
`answer_markdown`, so the length check, verification, and `finalize` run
unchanged, and `suggested_follow_ups` is cleared. No marker goes on
`AgentResponse` -- the UI renders the text, and the user replies with a
normal `POST /ask`. The field is in the schema either way, so it costs the
model nothing on an ordinary answer, which just leaves it empty.

**No forced tool choice, and that's a real constraint to design around, not
an oversight.** `agent_node` can't set `tool_choice` to force `submit_answer`
specifically — the same node handles every round of the loop, and sometimes
the model legitimately needs `run_bigquery_sql` instead. It's one more
*optional* tool; `verify_node`'s `model_validate` is what catches the model
never calling it.

**Excluded from `build_sources`'s badges** — not something the answer draws
information *from*. **Included in `agent_telemetry.tool_calls`** like any
other call, giving every submission a full audit trail regardless of
verification outcome.

## `run_dax_query`

**REST API only** (`executeQueries`). XMLA is a documented backup, not planned.

**Response shape — confirmed against Microsoft's REST reference:**

```python
rows = response["results"][0]["tables"][0]["rows"]
# [{"MyTable[Year]": 2010, "MyTable[Quarter]": "Q1"}, ...]
```

**Column keys are fully qualified** — `Table[Column]`, not `Column`. Anything
parsing these (row caps) has to expect that form.

**Documented API limits, all relevant here:**
- **One `EVALUATE` per request.** More than one result table returns limited
  data plus an error — so a compound DAX question needs separate calls, or
  `UNION` inside a single `EVALUATE`.
- **100,000 rows** and **15 MB** per result — beyond 15 MB, rows are silently
  dropped after completing the current one. Independent of our own row cap.
- **120-second query timeout**, and **120 requests/minute per user**. The
  ~90s gateway timeout sits under the query timeout, so ours fires first.
  **The rate limit is per *user*, and every query here runs as the same
  service principal** — so all users share one bucket. Accepted trade-off,
  not an oversight (§3): the alternative is an on-behalf-of token exchange
  per user, and this project won't approach 120/min.
- **Service principals are not supported on datasets with RLS or SSO
  enabled** — a hard blocker, not a permissions issue to work around.

Auth is a **separate** Azure service principal (not the connector's Entra app),
via MSAL client-credentials:

```python
import msal

def get_power_bi_token() -> str:
    app = msal.ConfidentialClientApplication(
        get_secret("power-bi-sp-client-id", GCP_PROJECT_ID),
        authority=f"https://login.microsoftonline.com/"
                  f"{get_secret('azure-tenant-id', GCP_PROJECT_ID)}",
        client_credential=get_secret("power-bi-sp-client-secret", GCP_PROJECT_ID),
    )
    return app.acquire_token_for_client(
        scopes=["https://analysis.windows.net/powerbi/api/.default"]
    )["access_token"]
```

Workspace role is **Contributor** (lowest that satisfies "Read + Build"). RLS is
off — a requirement, since service principals aren't supported on RLS-enabled
datasets. `executeQueries` has **no data-modification operation** at all.

**Row cap — a genuinely different mechanism than BigQuery's, confirmed via
Microsoft's own docs, not assumed to work the same way.** `executeQueries`
has a **100,000-row/1,000,000-value hard platform ceiling, plus a separate
15MB payload cap** (truncates mid-response once hit) — nowhere near a safe
number to hand an LLM or render in chat, and not something to rely on as
your actual safety net. Two parts, because there's no `max_results`-style
client-side fetch cap here the way BigQuery has:

1. **Steer generated DAX toward `TOPN`** when a result could be large —
   Microsoft's own recommended best practice for exactly this case, and
   unlike SQL's `LIMIT`, `TOPN` genuinely reduces VertiPaq engine work
   rather than just trimming a client-side fetch.
2. **Deterministic post-fetch check, the real guarantee** — steering is
   never sufficient alone:
   ```python
   response = execute_dax_query(dax_text)
   row_count = len(response["results"][0]["tables"][0]["rows"])
   if row_count > 1000:
       raise ToolError(
           "Query returned 1,000+ rows. Add a filter or tighten TOPN to narrow it."
       )
   ```
   Same actionable-error shape as the BigQuery cap — fail loudly, don't
   silently truncate and let the agent answer as if the result were
   complete.

## Composing DAX — what the model needs to get right

Prompt-side concerns, not tool mechanics. These shape *what the model writes*;
everything above is how the tool behaves once it's written.

**Steer the model toward existing measures, not new calculation logic.** DAX
is measurably harder for an LLM than SQL — context-transition semantics are
implicit and there's far less training data — so narrowing what it has to
generate matters more here than it would for SQL. System prompt: *"When a
relevant measure already exists, reference it by name rather than
reconstructing its logic. Only compose new DAX when no existing measure
covers the question."*

**Filter context must reach the generated DAX — including field-parameter
sliders.** The gateway captures dashboard filter state and puts it in the
prompt (`.claude/rules/gateway.md`); this tool's queries have to actually
*use* it. **Field parameters are the case that fails silently:** they're
filters on a synthetic table, so omitting one doesn't error — the
disconnected table falls back to its own default and the query returns a
different sensitivity scenario, cleanly and plausibly. A dimensional
filter omitted gives a wrong slice; a parameter omitted gives a wrong
*assumption*, with nothing to signal it.

**The model writes the whole DAX query, filter values included — considered
and deliberately kept that way.** A filter value containing a `"` (a real
category name, not an attack — these come from Power BI's SDK reading actual
slicer state, never free text) can produce a malformed query.

**Why no escaping layer or query-builder:** any scheme where code owns part
of the query means the model contributes a fragment, and **nothing can
guarantee it contributes a *fragment* rather than a complete statement** —
`strict=True` constrains shape, not semantics, the same reason measure names
aren't blocked either. Code wrapping a full `EVALUATE` in another
`CALCULATETABLE` is a *new* failure class, worse than the one being fixed.

**The existing error path already covers this**: a malformed query fails
loudly at `executeQueries`, the error surfaces, and the agent retries from an
actionable message. Loud and recoverable — not the silent-wrong-answer
category that justifies real guardrails elsewhere in this design.

**Present filter values already-escaped in the prompt's filter-context block**
so the model copies a correct value. Prompt formatting, not architecture.

**Grounding is structural, not enforced.** Table schema and the measure
registry are in static context on every turn (`.claude/rules/gateway.md`), so
there's no retrieval step to require first — the model composes DAX against a
schema it can already see. `get_measure_dax` is for reading a *formula*, not
for discovering that a measure exists.

**This is a deliberate choice for *this* agent, not a property of
`run_dax_query` as a shareable MCP tool.** A different client connecting to
this same server — one without a static-context pipeline built the same
way — has no way to discover a dataset's tables/measures/relationships at
all today; `run_dax_query` alone can't help until the caller already knows
what to ask for. A `search_semantic_model_schema`-style discovery tool would
close that gap for a generic client, at some latency/relationship-awareness
cost this project's own static-context approach avoids. Not building it here
— tracked as an optional Phase 7 item (`docs/build-order.md`).

## `get_measure_dax` — lookup, not search

**Not MCP-hosted** (see the hosting table above) — a plain function in
`app/orchestrator/tools.py`, dispatched the same way as `run_bigquery_sql`
and `submit_answer`.

```python
# MEASURE_NAMES is built at startup from model_schema.json — the same parse
# that produces the registry, so the tool's argument type and the registry
# the model reads from CANNOT disagree (.claude/rules/gateway.md).
def get_measure_dax(measure_names: list[MEASURE_NAMES]) -> dict[str, str]:
    """The DAX body for one or more measures, by exact name -- includes any
    OTHER measure referenced inside that DAX, one hop, not recursive.

    Names come from the measure registry, which is already in context — this
    returns HOW a measure is calculated, not whether it exists.
    """
    result = {name: MEASURE_DAX[name] for name in measure_names}
    for dax in list(result.values()):
        # One hop only -- recursing through the full dependency tree of
        # every measure found could genuinely balloon the result. A DAX
        # reference to a measure always looks like [MeasureName] --
        # checking every known measure name against the text (rather than
        # extracting bracketed tokens with regex and filtering) can't be
        # tricked by anything unexpected inside the brackets. A [Bracket]
        # that names a column, not a measure, just fails the membership
        # check below and gets dropped, silently and correctly.
        for candidate_name, candidate_dax in MEASURE_DAX.items():
            if candidate_name not in result and f"[{candidate_name}]" in dax:
                result[candidate_name] = candidate_dax
    return result
```

**Always expands one hop — not opt-in.** A flag was considered and dropped:
in this schema, only one measure (`Champion Recall` → `Champion_Model`)
references another at all, so unconditional expansion costs nothing in the
overwhelmingly common case where there's no dependency to find, and removes
a real failure mode — the model forgetting to ask for the dependency and
describing a wrapping measure's calculation as complete when it isn't. The
actual bloat risk this guards against is recursion depth, not the lookup
itself — walking the full dependency tree of every measure found could
genuinely balloon the result, which is why it's one hop, never recursive.

**No KeyError path.** `MEASURE_NAMES` is a `Literal` built from `MEASURE_DAX`
and tool schemas use constrained decoding (below), so an invented name can't
be emitted in the first place.

**Parameter value-measures are deliberately absent** from both — their DAX is
boilerplate `SELECTEDVALUE(...)`, and everything about them is already in the
`PARAMETERS` block in static context (`docs/data-pipeline.md`).

**Needed only to explain or extend a calculation** — never to *reference* one.
`SUMMARIZECOLUMNS(..., "AOV", [AOV])` needs the name, which is already in
context.

**DAX bodies stay out of static context** because near-identical formulas
(`Recall@5`, `Recall@10`, `Precision@5`) sitting side by side is the setup
where a model blends the wrong one. Fetching in isolation removes the
neighbour.

## `get_bigquery_schema()` — inside the static context bundle, not an MCP resource

**Not exposed via MCP at all — a plain function in
`app/orchestrator/context.py`** (`.claude/rules/orchestrator.md`), called
directly when assembling the static context bundle. Originally designed as
an MCP resource; that would have meant a *separate* fetch outside the cached
system-prompt block, so it would never get Anthropic's prompt-cache discount
the rest of the static bundle already gets — worse on the exact axis this
bundle exists to optimize, for no compensating benefit. Baking it into
`context.py` directly means it rides the same cached prefix for free.

**Not `INFORMATION_SCHEMA` — confirmed, not assumed.** `INFORMATION_SCHEMA.COLUMNS`
has no description field at all (checked directly against the real dataset).
Column *and* table descriptions only exist via the table metadata API —
`client.list_tables(dataset)` + `client.get_table(ref)`, which returns both
structure and native descriptions in one call. `INFORMATION_SCHEMA` plays no
role here.

**Read on first real use, cached for the life of the process, not a bare
module-level constant** — a real `bigquery.Client()` call executed eagerly
at import would make importing the module require live credentials,
breaking Layer 1 tests. An `@lru_cache`-decorated getter gets the same
"once per process" guarantee lazily instead (`.claude/rules/gateway.md`).

**No per-request read and no TTL.** Not for cost: any re-read risks the text
differing by a byte — a reordered column, a changed description — and a cache
hit needs a **byte-identical prefix**. One differing byte misses the cache for
the *entire* block, not just the schema. A TTL trades a ~90% discount on
every turn for freshness that `agent_safe` doesn't need: its schema changes
only when a Dataform model changes, which is a deploy, which restarts the
process anyway.

**Trade-off:** a schema change appears on the next deploy rather than within
minutes. Correct here — schema changes *originate* from deploys, so the two
are already coupled.

## Tool schemas use constrained decoding

Every tool's argument schema is designed to be bound with `strict=True` so
the model can't emit a structurally invalid call at all
(`.claude/rules/orchestrator.md` for the reasoning). **Structural only** — it
can't validate that a SQL query is *correct*, just that the call's shape is
valid.

**`strict=True` is enabled globally, with one tool bound outside it —
confirmed live (2026-10-02).** `generate_chart`'s discriminated union and
numeric bounds aren't representable under it on Claude (`docs/chart-tool.md`,
`.claude/rules/orchestrator.md`'s "Constrained decoding" section), so it's
pre-converted to a raw Anthropic tool dict (no `strict` kwarg) that
`bind_tools(BIND_TOOLS_LIST, strict=True)` passes through untouched —
every other tool gets real strict enforcement. `SearchDocsArgs.top_k` also
needed reshaping (its `ge=1` dropped, clamped at runtime instead) — strict
mode's `ge=`/`le=` rejection isn't unique to `generate_chart`, confirmed
against a second tool.

**Every tool gets an explicit args model.** FastMCP already infers a schema
from type hints and validates against it, but a named model buys two things:
**constrained types** the inference can't express (`Literal[...]` for
`get_page_info`'s page names, bounded ints for row limits) and **a class the
node can `model_validate()` against** before dispatch — which matters because
`call_tool_node` unpacks `args` before any tool function is reached
(`docs/approval-workflow.md`).

```python
class RunBigQuerySqlArgs(BaseModel):
    query: str

class GetPageInfoArgs(BaseModel):
    page_name: Literal[PAGES]
```

**Never put an injected argument in an args model.** `conversation_id` and
`bytes_consumed` come from state, not the model — `dispatch_tool` passes them
in Python, and they stay out of the schema entirely. This isn't stylistic:
combining `args_schema` with injected runtime arguments is a documented,
currently-open failure in LangChain (issues #33646, #34246, #34581), and
`extra: forbid` turns it into an unhandled exception. Injecting at the call
site avoids the whole class.

**`generate_chart` is a variant of the same rule: substitution instead of
addition.** The model's bound schema has `source_ref`; the real MCP tool's
schema has `data` instead — never both. `resolve_chart_data` resolves one
into the other before invoking. Full reasoning in `docs/chart-tool.md`.

**Discriminated unions must be a field on a wrapping `BaseModel`, never the
bare parameter type.** `def generate_chart(spec: ChartSpec)` loses the
discriminator — framework schema-synthesis from a function signature drops
`Field(discriminator=...)` on a bare parameter, flattening it to `anyOf`.
Wrap it (`GenerateChartArgs`) so Pydantic's own `model_json_schema()`
emits it. Full reasoning in `docs/chart-tool.md`.

Practical consequence when writing a tool schema: strict mode carries real
constraints (typically every field in `required`, optionality as nullable
types rather than omission). A schema may need reshaping to comply — that's
expected, not a sign the schema is wrong. Confirm the exact rules for the
provider in use.

**On Claude, this is `tools[].strict`** — GA since Feb 2026, no beta header.
Its schema rules match OpenAI's: `additionalProperties: false` on every
object, every property in `required`. Claude's separate `output_format` mode
handles `AgentResponse`; `tools[].strict` handles tool arguments, and both
can be used in the same request.

**Format compliance is not accuracy** — Anthropic's own caveat. A
schema-valid tool call can still carry a wrong value, which is why
`verify_response()` exists alongside this, not instead of it.

## `get_page_info` — lookup, not search

**Not MCP-hosted** (see the hosting table above) — a plain function in
`app/orchestrator/tools.py`, same category as `get_measure_dax`/`search_docs`.

```python
PAGES = tuple(sorted(p.stem for p in PAGE_INFO_DIR.glob("*.txt")))
if not PAGES:
    raise RuntimeError(f"No page-info files found in {PAGE_INFO_DIR}.")


class GetPageInfoArgs(BaseModel):
    page_name: Literal[PAGES]


@tool(args_schema=GetPageInfoArgs)
async def get_page_info(page_name: Literal[PAGES]) -> dict[str, str]:
    """Whole-page content for one of this dashboard's two pages. Use the
    page named in "Current dashboard page" for what the user is currently
    viewing, or the other one if the question is clearly about it instead.
    """
    content = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
    return {"page_name": page_name, "content": content}
```

**`PAGES` is derived from `context/page_info/`'s real files, not a hardcoded
tuple.** Adding a third page is dropping a new `.txt` file there, no code
change — the `Literal[PAGES]` schema picks it up automatically.

**Why an enum, not a system-instruction sentence.** The page set is fixed and
fully enumerable — exactly the condition where a hard schema constraint is
correct (contrast DAX measure names, deliberately *not* blocked because valid
names aren't enumerable). With `strict=True`, the model **cannot** emit
`"Financial Impacts"`; prose in the system prompt would only be a hint. It's
also cheaper: schemas are already sent every turn, so naming the pages there
costs nothing extra, while system-instruction prose is paid on every turn
including ones that never touch page info.

**`page_name` is required, with no `active_page` fallback/injection at
all — a deliberate simplification over the original design, confirmed live
(2026-10-02).** The original sketch made `page_name` optional and injected
`active_page` (from `report.getActivePage().displayName`) as a code-supplied
fallback, on the reasoning that "the model shouldn't be trusted to correctly
relay which page is CURRENT." That reasoning was retested rather than
assumed: with only two pages, and `"Current dashboard page: {active_page}"`
already in every `HumanMessage` (`.claude/rules/gateway.md`), the model
reads its own context and supplies `page_name` correctly every time —
confirmed across both same-page questions (no page named, relying on
transcription) and cross-page questions (naming the *other* page than the
one it's on). No injection plumbing, no `dispatch_other_call` special case,
no optional field.

**Returns a `dict`, not a bare `str` — a real bug, not a style choice.**
LangChain JSON-encodes a tool's non-string return into the `ToolMessage`
content automatically, the same as every other tool here; a bare `str`
return instead passes through as raw, unencoded text, and
`build_tool_call_record`'s `json.loads(...)` (`.claude/rules/orchestrator.md`)
fails on it immediately. Confirmed live before shipping.

**Trade-off:** a third page means a new file, not an enum edit anymore —
smaller trade-off than `FILE_DESCRIPTIONS`/the HTML-measure exclusion list,
which still need a manual line added.

## Tool error quality

The agent reads tool errors and retries from them. Errors must name the
specific gap and what's available:

```python
raise ToolError(f"Field(s) {missing} not in source data. Available: {list(df.columns)}")
```

Not `"invalid input"`.

## Tool-layer governance

SQL-executing tools are a known attack surface. Input validation, statement-type
allow-listing, and row/scan limits live **here**, at the tool layer — not only
at the approval node. Defense in depth.
