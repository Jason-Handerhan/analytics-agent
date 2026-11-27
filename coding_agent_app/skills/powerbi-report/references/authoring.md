
# Authoring Mode: Power BI Report (PBIR/PBIP)

This mode enables reading, editing, and creation of Power BI report
definition files in the **PBIR (Power BI Report)** format used by **PBIP
(Power BI Project)** files.

## Must/Prefer/Avoid

### MUST

- Use this mode only for concrete PBIR/PBIP report-file mechanics such as pages, visuals, filters, slicers, navigation, bookmarks, themes, formatting, validation, publishing, and renders.
- Use CLI capability lookup before writing visual roles, formatting objects, enum values, selectors, or expression encodings.
- When editing an existing dashboard, copy the neighbors unless the plan says otherwise: start a new visual from an existing visual of the same type and keep its formatting, sizing and placement. For a new dashboard, follow the sizing formulas unless the plan gives you a reference to copy.
- Leave `definition.pbir` (the publish tool sets its model binding), `.pbi/` and the `.pbix` file alone.
- A visual on a measure that is not yet in the published model fails with `Missing_References`: add the measure first (the `semantic-model` skill), then run `publish_dashboard`.

### PREFER

- Start from the plan and, for a new page or visual, the design from `design.md`.
- Route visual-design uncertainty to the `design` mode before writing files.
- For model changes (measures, columns, tables), use the `semantic-model` skill.

### AVOID

- Do not guess PBIR JSON from memory when CLI metadata or reference files are available.
- Do not use only this skill for open-ended design or report planning; pair it with the `design` mode or the `planning` mode.

## Quick Start Workflow

1. **Plan, then design if it is new** → `planning.md` runs first. For a new page, or a visual with nothing similar to copy, read `design.md` next. A focused edit to an existing page comes straight here.
2. **Use the model inventory** → planning wrote it to `/tmp/model-inventory.md`: read it, and query the model again (`run_dax_query`, `INFO.VIEW.*`) or read the TMDL files only for what it does not cover. Model changes belong to the `semantic-model` skill.
3. **Route by intent** → use **Topic Files and Examples** below to pick the relevant guide.
4. **Use CLI metadata** → see `powerbi-report-author-cli.md` for exact visual roles, formatting objects, property names, enum values and selector requirements; do not infer these from memory.
5. **Check common pitfalls** → read `pitfalls.md` before editing when the change touches visuals, bindings, filters, formatting, layout or rendered output.
6. **Review** → review each render using `screenshot-review.md`. For dashboard builds, page scaffolding is not completion: each requested page needs data-bound visuals.
7. **Finish** → give a concise summary of what was done and any issues (major and minor).

## Deleting a page, visual or bookmark

Remove it and everything that points at it.

- **A page:** delete its folder under `definition/pages/` and its entry in `pageOrder` in `pages.json`. If `activePageName` was that page, set it to another page.
- **A visual or bookmark:** delete its folder or file and, for a bookmark, its entry in `bookmarks.json`.
- **What points at it:** search the report files for its id and name, and remove or retarget each match: buttons that navigate to a page, bookmarks that save the page or visual, drillthrough and tooltip pages, slicer sync groups.
- Then run `validate_dashboard`: it reports a bookmark or action that still points at something you removed.

## Topic Files and Examples

Use the user's intent to choose the relevant topic file(s) before editing. Files named `part-XX` continue the named topic:

| File | When to read |
|------|-------------|
| `authoring-workflows.md` | Adding/modifying pages, visuals, drillthrough, interactions — includes complete JSON examples |
| `screenshot-review.md` | Screenshot review checklist and rendered-output troubleshooting after a render |
| `formatting-overview.md` | **Read first for appearance changes** — cascade model, encoding rules, selectors, routing to other formatting files |
| `formatting.md` | Editing `visual.json` appearance — selectors, VCOs, encoding mechanics, background-image routing, cascade |
| `color-strategy.md` | Chart data point colors — theme `dataColors` vs `dataPoint.defaultColor` vs `dataPoint.fill` with selectors, cross-visual measure-color consistency |
| `conditional-formatting.md` | Data-driven formatting — field-value result contracts and support guardrails, color gradients (FillRule), rules, icon sets, data bars, web URLs, field-driven colors, images, and totals/subtotal targeting |
| `page-formatting.md` | Editing `page.json` appearance — canvas background, wallpaper, page background images |
| `filter-pane.md` | Filter pane (`outspacePane`) and filter card (`filterCard`) chrome — Applied/Available state styling, pane width, search/checkbox colors |
| `theming.md` | Creating or editing `theme.json` — dataColors, textClasses, visualStyles, style presets, ThemeDataColor reference |
| `re-theming.md` | **Switching themes on a report with existing visuals** — re-theming workflow (color mapping + bulk sweep), dark mode checklist, dark↔light polarity changes. Pair with `theming.md` when changing colors on a report with per-visual overrides. |
| `expressions.md` | Building field references (Column, Measure, Aggregation, Hierarchy), visual calculations, and sort definitions |
| `filters.md` | Adding/modifying filters — includes complete JSON examples |
| `slicers.md` | **Read first** when adding/modifying slicers or slicer selections — agent workflow, JSON templates, selection config |
| `field-parameters.md` | Slicer-driven "metric selector" / "dynamic view" / "view by" visuals — recognizing/using a field parameter, target-visual projection encoding |
| `bookmark.md` | Creating and editing report bookmarks — metadata/index files, saved page/visual/filter state, visibility toggles, groups, button actions, and validation |
| `cartesian.md` | Adding bar, column, line, and scatter/bubble charts — families, roles, query patterns (multi-measure, drill hierarchy, date hierarchy, scatter grouping), formatting |
| `map.md` | Adding map visuals — template, roles, geocoding workflow, handling render failures |
| `card.md` | Adding or formatting KPI/card visuals — `cardVisual`, id selectors, callout/value sizing, accent bars |
| `kpi.md` | Adding `kpi` visuals (Indicator + Trend axis + Target) — valid field combos, blank-visual root causes and fixes, query templates |
| `button.md` | Adding or formatting button visuals — `actionButton`, actions (Back, PageNavigation, WebUrl, Bookmark, Q&A), state selectors, dual-entry pattern |
| `table.md` | Adding or formatting tables/matrices — `tableEx`, `pivotTable`, grow-to-fit columns, row banding |
| `image.md` | Adding image visuals — local resources, URLs, data-bound images, ImageUrl validation/refusal workflow; also plot area background images for chart visuals |
| `shape.md` | Adding shape visuals — containers, dividers, backgrounds, reference-image matching |
| `textbox.md` | Adding static or dynamic textbox visuals — paragraphs, text runs, and bound value expressions |
| `custom-visuals.md` | Adding AppSource, organizational, or private `.pbiviz` custom visuals — GUID discovery, `report.json` registration (`publicCustomVisuals` / `OrganizationalStoreCustomVisual` `_OrgStore` package / `resourcePackages`), `CustomVisuals/` layout, data binding, freemium licensing |

Also in this skill:

| File | When to read |
|------|-------------|
| `pbir-layout.md` | The report's file tree, what each file is for, and the `$schema` rules |
| `layout.md` | Placing or resizing a visual: the 8px grid, gaps, bounds, slicer placement |
| `powerbi-report-author-cli.md` | The full command list of `powerbi-report-author` and how to read a validation result |
| `pitfalls.md` | Before editing or after a surprising result: known PBIR traps and their fixes |
