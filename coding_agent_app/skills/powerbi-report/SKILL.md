---
name: powerbi-report
description: Edit this dashboard's Power BI report by hand in its PBIR files, including adding or changing pages, visuals, cards, charts, slicers, filters, bookmarks, buttons, themes and formatting. Use whenever a task touches the report under dashboard/pbip (the folder ending in .Report). The semantic model's tables, columns and measures are covered by the semantic-model skill.
---

# Power BI Report

This skill covers the dashboard's report in three modes: planning (check the plan against the dashboard and the model before building),
design (decide how a new page or visual looks), and authoring (edit the PBIR files). A mode that does not
apply is skipped. Model changes (measures, columns, tables) belong to the `semantic-model` skill; this skill covers the report only.

It is a **mode dispatcher** and intentionally contains no detailed procedures.
Select the mode that matches the request, then read the matching mode reference
end to end before acting. The mode file holds the required workflow, commands,
payloads, templates, and failure rules.

## Mode Selection

| Mode | Use when the request... | Read first |
|---|---|---|
| `planning` | starts a dashboard task: the plan is checked against the dashboard and the model, the model is inventoried, and anything vague is settled before building | [references/planning.md](references/planning.md) |
| `design` | adds a new page, or a visual with nothing similar to copy: archetype, chart choice, layout, color, typography, theme, accessibility | [references/design.md](references/design.md) |
| `authoring` | edits the PBIR files: pages, visuals, filters, slicers, bookmarks, themes, formatting | [references/authoring.md](references/authoring.md) |

## Mode Boundary Rules

- Classify by **intent**, not by which file or tool is already open.
- `design` decides what the report should look like; `authoring` writes the
  PBIR that realizes it. Choosing a chart type is `design`; encoding it into
  `visual.json` is `authoring`.
- Restyling or reformatting an existing local report is `authoring`, even when
  the request uses design language. Use `design` when the user wants advice or
  a design contract rather than a file change.
- `planning` checks the plan before any edit. It is short for a focused edit to an
  existing page, which then goes straight to `authoring` without `design`.

A task that adds a page, or a visual with nothing to copy, spans modes in this order:

```text
planning -> design -> authoring
```

A task that only edits existing pages is `planning -> authoring`.

Handle one mode at a time. Announce each switch and read the new mode reference
before starting that part.

## Required Deliverable by Mode

| Mode | Completion requirement |
|---|---|
| `planning` | The model inventory and the todo list. A plan that is vague, or missing something essential, ends the task with a blocking question instead. |
| `design` | The design for each new page or visual: archetype and variant, chart choices, and the layout the plan gives, checked against the pre-flight list. Do not edit PBIR. |
| `authoring` | The requested PBIR and model changes. |

## Rules

### MUST

- Select the narrowest mode that fully covers the current request.
- Read `references/<mode>.md` end to end before the first command or file edit
  in that mode. When an already-loaded mode explicitly routes one narrow
  cross-mode operation to a named topic reference, treat that topic reference
  as the complete handoff; do not load the target mode's umbrella file unless
  the work expands beyond that operation.
- Announce and perform an explicit mode switch when the request crosses a
  boundary.
- Use the `powerbi-report-author` CLI metadata and validation surfaces instead
  of guessing PBIR schemas, roles, formatting properties, selectors, or enum
  values.
- Produce the artifact the user requested; reading or summarizing a reference
  is not task completion.

### PREFER

- Infer facts already present in the prompt, semantic model, existing PBIP, or
  the plan instead of asking again.
- Load only the selected mode and its directly relevant topic references.
- Reuse reference guidance already loaded in the current task. Open a new
  reference only when the requested operation or an observed mismatch requires
  it.

### AVOID

- Acting from this dispatcher without reading the selected mode reference.
- Editing PBIR in `planning` or `design`.
