# Combine Results Tool

Built 2026-10-04 (build-order item 11). Stacks or joins several earlier tool
results into one new `ref_id`, which `generate_chart` or another
`combine_results` call can consume.

## Why it exists

A wide question sometimes can't fit under the per-query cost cap in one shot.
`TABLESAMPLE` looked like the fix, but its dry-run estimate underestimated real
cost on the table in question, so it isn't a safe steer. Selecting fewer
columns per query works, but splits the answer across several results, and
`generate_chart` takes exactly one `source_ref`. `combine_results` is the
separate step that reassembles those results. It's also the way to put a
BigQuery result and a DAX result side by side for comparison.

## Hosting

Hosted on the MCP server. The real tool needs only the data it's given: no
identity, connection, or hidden args. Going local was considered and rejected:
the logic is small, but the tool is generic enough that another MCP client can
call it directly with its own data, which is the reuse case that justifies
hosting it. Both hosting options need the same plumbing (the model supplies
`source_refs`, so something has to resolve them), so hosting cost nothing extra.

## The two methods

- **`stack`** appends rows. Every input must have the same columns.
- **`join`** merges rows on `join_key` (one or more columns) into wider rows.
  `join_how` picks the join type:
  - `inner` (default) keeps only rows that match in every input.
  - `left` keeps every row of the first input. Unmatched numeric columns fill
    with 0, and unmatched categorical columns stay null.

The first input is the left table for a join.

## Real tool

`app/mcp_server/combine_tool.py`, registered on the MCP server.

- `CombineResultsArgs`: `data` (list of lists of dicts, `min_length=2`),
  `method`, `join_key` (`list[str] | None`), `join_how` (default `inner`).
  A model validator requires `join_key` when `method` is `join`. That's an
  argument-shape check, so it runs at construction, before the tool body.
- Guardrails run in the tool body against the real data, and raise
  `fastmcp.exceptions.ToolError`:
  - `stack`: input column sets must match.
  - `join`: every key column must exist in every input.
  - `join` with no real matches: an inner probe catches this even for a left
    join, since a left join is never empty on its own. The message depends on
    whether the inputs share columns. Same columns suggest stacking. Different
    columns point at the key or its values.
- The tool returns JSON-safe rows (`to_json` then `json.loads`), so `NaN` becomes `null`.

## Stand-in (model-facing)

`app/orchestrator/tools.py`: `CombineResultsToolCallArgs` and
`combine_results_call_standin`.

- `source_refs: list[str]` replaces `data`. There's no `minItems`, because
  Anthropic's tool schemas accept only `minItems` 0 or 1. The "at least two"
  rule lives in a model validator on this class instead, which keeps it out of
  the schema the model sees.
- The stand-in's docstring carries the guidance for when to use the tool: query
  size limits (split by columns, then combine), and comparing BigQuery with DAX.
- Bound to the model in `BIND_TOOLS_LIST` in place of the real tool, the same
  swap `generate_chart` uses.

## Dispatch

`app/orchestrator/orchestrator.py`.

- `resolve_combine_data`: validates the stand-in args. On a `ValidationError`, it
  raises `ToolError` with the validator's own message. Then it looks up each ref
  with `_lookup_source_ref` and builds the real tool's args: `data`, `method`,
  `join_key`, `join_how`.
- `dispatch_other_call`: a `combine_results` branch. A `ToolError` becomes an
  error `ToolMessage` the model can retry from.
- No injected args, since nothing is hidden from the model.
- `CHARTABLE_TOOLS` includes `combine_results`, so its results get a `ref_id`.

## Verification

`combine_results` is in `NUMERIC_SOURCE_TOOLS`. It only recombines numbers
from earlier queries, plus the 0s that a left join fills in for missing
numeric values. A filled 0 means "no matching value," which is a fact about the
data, so a model that states it passes verification.

## Tests

`tests/test_combine_tool.py`: `test_combine_results_stack_join_and_guardrails`
covers the tool. Ref resolution (`resolve_combine_data`) is tested in
`tests/test_orchestrator.py`, alongside `resolve_chart_data`.

## Live

Q2 (the wide-correlation question) ran end to end: batched queries, then
`combine_results`, then a chart.
