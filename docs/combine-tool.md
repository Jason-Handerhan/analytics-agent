# Combine Results Tool Design

**Not built yet — planning doc, read before starting item 11
(`docs/build-order.md`).** Mirrors `generate_chart`'s `source_ref` pattern
(`docs/chart-tool.md`, `.claude/rules/tools.md`) rather than inventing a new
mechanism.

## Motivating case

A live BigQuery cost-cap investigation (2026-10-04, `agent_telemetry`
transcript) surfaced a real gap: a wide correlation question across many
features didn't fit under the real per-query byte cap in one shot.
`TABLESAMPLE`'s dry-run estimate turned out to dramatically underestimate
real execution cost on the table in question, so steering the model toward
it as a cost-reduction strategy isn't safe. The reliable alternative —
select fewer columns per query — means a wide analysis has to be split
across several smaller `run_bigquery_sql` calls, each covering a different
slice (a different subset of features). Nothing could then combine those
separate results into one `generate_chart` call: the chart tool takes
exactly one `source_ref`.

**This tool is the fix — not a `generate_chart` change.** It's a new,
separate step that combines several existing results into one new
`ref_id`, which `generate_chart` (or another `combine_results` call) then
consumes exactly like any other chartable result.

## Hosting — MCP, and more portable than `generate_chart`

The real tool operates purely on already-fetched tabular data (`list[dict]`
per source) plus an operation name — no identity, no connection info, no
GCS bucket to hide. Unlike every other MCP-hosted tool here, it needs
**zero hidden args at all**, which makes it the most reusable tool in the
inventory, not an exception to the hosting rule (`.claude/rules/tools.md`).

## Two methods, model-picked — guardrails catch a mismatched choice

- **`stack`** — appends rows together. Use when every referenced result has
  the *same columns* but covers a different slice (e.g. different feature
  batches of the same two-column `feature_name, correlation` shape — the
  motivating case above).
- **`join`** — merges rows that share a key into wider rows. Use when
  referenced results have *different columns* describing the same entities
  (e.g. per-product revenue in one ref, per-product units in another),
  matched on a shared key column.

**Guardrails live in the real tool, not the resolve step** — same place
`chart_tool.py`'s `render()` validates its spec against its data
(`docs/chart-tool.md`), so the tool is self-contained and correct for any
caller, not just this orchestrator:
- `stack`: every input must have the identical column set. Mismatched →
  actionable `ToolError` naming which columns differ and in which ref.
- `join`: `join_key` must be present in every input. Missing from any →
  actionable `ToolError` naming which ref lacks it. If the merge produces
  zero rows, error rather than silently returning nothing — this is exactly
  what catches the model picking `join` when the inputs were actually
  disjoint batches that needed `stack`.

## Real tool schema

```python
# app/mcp_server/combine_tool.py

class CombineResultsArgs(BaseModel):
    data: list[list[dict]] = Field(..., min_length=2)
    method: Literal["stack", "join"]
    join_key: str | None = None

    @model_validator(mode="after")
    def _join_key_required_for_join(self) -> "CombineResultsArgs":
        if self.method == "join" and not self.join_key:
            raise ValueError("join_key is required when method is 'join'.")
        return self


@mcp.tool()
async def combine_results(args: CombineResultsArgs) -> list[dict]:
    """Stacks or joins several already-fetched results into one combined
    table. 'stack' appends rows -- use when every input has the same
    columns but covers a different slice. 'join' merges rows on a shared
    key into wider rows -- use when inputs have different columns about
    the same entities. Never computes a new value; only recombines rows
    and columns that already exist in the inputs.
    """
    if args.method == "stack":
        return _stack(args.data)
    return _join(args.data, args.join_key)
```

`_stack`/`_join` are plain-Python/`pandas` helpers implementing the
guardrails above — no new concept beyond what `chart_tool.py`'s own
row-count-mismatch check already establishes for this codebase.

## Model-facing stand-in — same substitution pattern as `generate_chart`

```python
# app/orchestrator/tools.py, next to chart_tool_call_standin

CombineResultsToolCallArgs = create_model(
    "CombineResultsToolCallArgs",
    source_refs=(list[str], Field(
        ..., min_length=2,
        description="Reference ids of the results to combine -- e.g. "
                     "['ref_1', 'ref_2']. Copy them exactly as shown; "
                     "never invent one.")),
    method=(Literal["stack", "join"], Field(
        ..., description="'stack' appends all rows together -- use when "
                          "every ref has the same columns but covers a "
                          "different slice (e.g. different feature "
                          "subsets, different date ranges). 'join' merges "
                          "rows that share a key into wider rows -- use "
                          "when each ref has different columns about the "
                          "same entities (e.g. revenue per product in one "
                          "ref, units per product in another).")),
    join_key=(str | None, Field(
        default=None, description="Column name to join on -- required "
                                    "when method is 'join', must exist in "
                                    "every referenced ref's columns. Omit "
                                    "for 'stack'.")),
)


@tool("combine_results", args_schema=CombineResultsToolCallArgs)
async def combine_results_call_standin(**kwargs) -> None:
    """Stacks or joins several earlier tool results into one new reference
    id, which can then be passed to generate_chart or another
    combine_results call. See method's own description for which to pick.
    """
    raise NotImplementedError(
        "combine_results' real MCP tool object handles dispatch -- this "
        "stand-in exists only so bind_tools() advertises a different schema."
    )
```

**No discriminated union, no `ge=`/`le=` bound** — unlike `generate_chart`'s
spec, this schema has nothing `strict=True` rejects
(`.claude/rules/orchestrator.md`'s "Constrained decoding" section), so it
binds under the normal `strict=True` path. No raw-Anthropic-dict workaround
needed here.

## Dispatch wiring

```python
# app/orchestrator/orchestrator.py

def resolve_combine_data(combine_tc: dict, prior_tool_calls: list[ToolCallRecord]) -> dict:
    """Rewrites a model-facing combine call into the real MCP tool's args shape."""
    call_args = CombineResultsToolCallArgs.model_validate(combine_tc["args"])
    source_tcs = [_lookup_source_ref(ref, prior_tool_calls) for ref in call_args.source_refs]
    return {
        **combine_tc,
        "args": {"args": {
            "data": [tc["result"] for tc in source_tcs],
            "method": call_args.method,
            "join_key": call_args.join_key,
        }},
    }
```

In `dispatch_other_call`:

```python
if tc["name"] == "combine_results":
    try:
        resolved = resolve_combine_data(tc, prior_tool_calls)
    except ToolError as e:
        return ToolMessage(content=str(e), name=tc["name"], tool_call_id=tc["id"], status="error")
    return await ALL_TOOLS[tc["name"]].ainvoke(resolved)
```

No `inject_combine_args` — nothing to inject.

## `ref_id` reuse — its result is chartable, and combinable again

Add `"combine_results"` to `CHARTABLE_TOOLS`, so a successful call gets a
`ref_id` the same way `run_bigquery_sql`/`run_dax_query` do, and can feed
`generate_chart` or a second `combine_results` call.

**Rename `_lookup_chart_source` → `_lookup_source_ref`** — it's resolving
refs for two tools now, not just charts. Update its error message to name
all three source tools:

```python
def _lookup_source_ref(source_ref: str, prior_tool_calls: list[ToolCallRecord]) -> ToolCallRecord:
    source_tc = next(
        (r for r in prior_tool_calls
         if r.get("ref_id") == source_ref and r["success"] and r["name"] in CHARTABLE_TOOLS),
        None)
    if source_tc is None:
        raise ToolError(
            f"'{source_ref}' is not a successfully completed run_bigquery_sql, "
            "run_dax_query, or combine_results call from earlier this turn -- it may not "
            "exist, may have failed, or may be from later in this same batch and hasn't "
            "run yet. Fetch the data first, then reference it in a follow-up step.")
    return source_tc
```

`resolve_chart_data` calls this same renamed function — no behavior change
for `generate_chart`, just a shared name.

`SOURCE_LABELS` (`.claude/rules/orchestrator.md`) gains
`"combine_results": "Combine data"` for the badge trail.

## Verification contract — unchanged, deliberately

`combine_results` stays **out of `NUMERIC_SOURCE_TOOLS`.** Every value in
its output already exists in the pool via the original
`run_bigquery_sql`/`run_dax_query` records it recombined — it's a pure
reshape, never a computation. Adding it to `NUMERIC_SOURCE_TOOLS` would be
redundant at best; leaving it out keeps the verification contract exactly
as documented, with nothing new to reason about. This only holds because
the tool's contract is genuinely restricted to stack/join with no derived
columns — if that ever changes, revisit this section first.

## Build order

Notebook-first in `phase3_graph.ipynb`, proven live (both methods, plus
each guardrail's failure case), then ported — same workflow as every other
tool this build. See `docs/build-order.md` item 11.
