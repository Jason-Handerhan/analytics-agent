
# Planning Mode: Settle the Plan Before Building

The plan you were given was settled with the user before this task started, so this mode interviews no one. It checks the plan against the real dashboard and model before you edit anything, so the build starts from facts.

1. Check the plan for vagueness before anything else. For a new page or dashboard it is vague if audience, purpose, page count, or desired filters/slicers are missing; for an edit, if it does not say which page to change and how. If it is vague, stop and end the task with a blocking question.
2. Inventory the published dashboard (`powerbi-report-author preview-pages` and `preview-visuals`) and the published model (`run_dax_query` with `INFO.VIEW.TABLES()`, `COLUMNS()`, `MEASURES()` and `RELATIONSHIPS()`), using the summary under *Model inventory*, and write that summary to `/tmp/model-inventory.md`. Then compare it with the files you start from: if the published dashboard or model does not match them (a page, table, column or measure in one and not the other), stop and end the task with a blocking question; publish over it only if the plan says the user knows about the mismatch and asked you to.
3. Check that every page, measure and field the plan names exists, or that you can build it from the model and the data in BigQuery, and that the plan covers what is listed under *Slicers and interactions*. If something it names does not exist and its meaning is not clear enough to build, or something essential cannot be done, stop and end the task with a blocking question. A smaller gap is a sensible choice that you list as an assumption.
4. Write the todo list: model changes first, then each page's visuals, then `validate_dashboard`, `publish_dashboard` and `render_dashboard`.

**Deliverable:** the inventory and the todo list. Then read `design.md` for a new page or visual, or `authoring.md` for an edit.

## Model inventory

Summarize:

```markdown
Facts:
- table, grain, keys, useful measures

Dimensions:
- date/time, geography, entity, category, owner, status, segment

Existing measures:
- core available measures

Likely missing model work:
- measures
- calculated columns
- relationship fixes
- sort columns
- helper fields for slicers/search

Risks:
- nulls/sparsity
- inactive relationships
- high-cardinality slicers
- fields that will not filter as expected
```

## Slicers and interactions

Check that the plan covers:

- Global slicers
- Page-specific slicers
- Search/prefix slicers for high-cardinality dimensions
- Drillthrough/profile pages
- Navigation model: page navigator vs. custom buttons vs. bookmark navigator (pick one primary)
- Button actions needed: Back, page navigation, bookmark, drillthrough, apply/clear all slicers, Web URL, Q&A
- Bookmarks: named saved states for narrative steps or view toggles

## Design defaults

These come from the design gotchas and the base theme, and apply unless the plan says otherwise:

- Use accessible contrast (WCAG AA minimum on every text/background pair).
- Avoid red-on-red and low-contrast palettes.
- Prefer Azure Map over deprecated map/filledMap visuals.
- Add alt text to every chart.
- Use searchable dropdown slicers for high-cardinality fields.
- Use tile/list slicers only for short categorical fields.
- Place detailed tables near the bottom of the page.
- Keep report interactions predictable and consistent.

## Validation Standards

A report is not complete until:

- Required PBIP/PBIR files exist.
- All JSON parses.
- Pages and visuals are generated in expected counts.
- `validate_dashboard` passes.
- The last `publish_dashboard` call is the final state of the files.
- Every changed page renders without errors after that publish.

## Anti-Patterns and Pitfalls

- Model changes must be published with `publish_dashboard` before a visual that uses them renders; otherwise it fails with `Missing_References`.
- DAX filter direction can break dimension aggregations from fact-side flags.
- High-cardinality slicers need search or prefix filtering.
- Rendering catches issues that validation cannot.
