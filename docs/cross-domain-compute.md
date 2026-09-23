# Cross-domain compute tool (Phase 7, not built)

A deferred design, not a committed feature — `docs/build-order.md` Phase 7.
Read before building it, if testing ever shows same-domain query-side
derivations (`.claude/rules/orchestrator.md`) aren't enough.

## Why this exists at all

Most derived numbers (a percentage change, a difference, a ratio) should be
computed in the query that produces them — a DAX measure, a SQL expression —
not by the model in prose. That's enforced by `verify_response`'s pooled
matching: a number the model computes itself won't exist in any tool result,
so it fails verification regardless of whether the model was told not to do
that.

That approach breaks down for one case: a derivation spanning a BigQuery
value and a DAX value. No single query can produce it — they're separate
engines. `tools.md`'s domain split (BigQuery pre-modeling, DAX post-modeling)
already keeps this rare; this tool is the fallback for when it's actually
needed, not the default path.

## Design

**Inputs are references, not literal values — closes a real hole.** If the
model retyped the numbers as arguments, a transcription error would produce
a wrong-but-tool-sourced result that still passes verification. Same pattern
`generate_chart` already uses for its data: resolve from state, never let the
model re-supply a value it could get wrong.

```python
class ComputeArgs(BaseModel):
    source_a: str          # tool_call_id
    column_a: str
    source_b: str          # tool_call_id
    column_b: str
    join_on: str            # shared key column, e.g. "product_id"
    operation: Literal["add", "subtract", "multiply", "divide"]
```

**Rows are paired by an explicit join key, never by position.** Nothing
guarantees two independently-issued queries return rows in the same order —
one sorted by id ascending, the other by value descending, same underlying
rows. Pairing positionally would silently combine the wrong rows. `join_on`
requires an actual (if small) join: match `table_a[join_on] == table_b[join_on]`
before applying the operation.

**Output is a table, not a scalar** — `list[dict]`, one row per matched pair,
e.g. `[{"product_id": ..., "rank_diff": ...}, ...]`. That's the same shape
`run_dax_query`/`run_bigquery_sql` already return (`tools.md`'s shared return
contract), so this tool's result becomes just another `ToolCallRecord` —
automatically part of `verify_response`'s pool, no special-casing needed.

**Bounded by construction.** An inner join can't produce more rows than the
smaller input table, and both inputs already went through their own row cap
— no separate cap needed on this tool's output.

**A small, enumerable operation set, not code execution.** Four parameterized
operations, not a Python sandbox. Covers the actual need (simple arithmetic
on real cross-domain values) without the security surface of running
arbitrary code.

## Open questions — not resolved, decide when building

- **No rows share a `join_on` value between the two tables** — should be an
  actionable `ToolError` ("no matching product_id between the two sources"),
  not a silent empty result.
- **Divide by zero** — needs an explicit policy (error the call, or null that
  row and continue) rather than whatever the runtime does by default.
