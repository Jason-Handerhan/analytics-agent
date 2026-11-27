
# Planning Mode: Settle the Plan Before Building

The plan you were given was settled with the user before this task started, so this mode interviews no one. It checks the plan against the real dashboard and model before you edit anything, so the build starts from facts.

1. Check the plan for vagueness before anything else. For a new page or dashboard it is vague if audience, purpose, page count, or desired filters/slicers are missing; for an edit, if it does not say which page to change and how. If it is vague, stop and end the task with a blocking question.
2. Understand the current dashboard as far as the request needs. In the sandbox you have the files under `dashboard/pbip` and `powerbi-report-author preview-pages` and `preview-visuals`, which read them. For what is published you have `run_dax_query` for the model and `render_dashboard` for the report. If you notice a significant difference between the published version and the files, stop and end the task with a blocking question, unless the plan says to publish over it. Write what you learn to `/tmp/model-inventory.md`, using the headings under *Model inventory*.
3. Check that every page, measure and field the plan names exists, or that you can build it from the model and the data in BigQuery, and that the plan covers what is listed under *Slicers and interactions*. If something it names does not exist and its meaning is not clear enough to build, or something essential cannot be done, stop and end the task with a blocking question. A smaller gap is a sensible choice that you list as an assumption.
4. Write the todo list for the work, including the validation steps.

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

## Anti-Patterns and Pitfalls

- DAX filter direction can break dimension aggregations from fact-side flags.
- High-cardinality slicers need search or prefix filtering.
- Rendering catches issues that validation cannot.
