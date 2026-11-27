---
name: semantic-model
description: Edit this dashboard's Power BI semantic model by hand in its TMDL files, including measures, columns, relationships, field parameters, descriptions, hierarchies and calculation groups. Use whenever a task touches the model under dashboard/pbip (the folder ending in .SemanticModel). Report pages and visuals are covered by the powerbi-report skill.
---

# Semantic model (TMDL)

You edit the model's TMDL text files directly, in `dashboard/pbip/<name>.SemanticModel/definition/`. Tables are in `tables/`, one `.tmdl` file each, and a measure lives inside its table's file. Report pages and visuals are a different job.

A separate AI assistant, the **analytics agent**, answers users' questions about this dashboard's data. It reads the live model's table, column and measure names and descriptions, and runs DAX against the model as a service principal. The references below mention it wherever a rule exists for its sake.

## Rules

- **A new data table must already exist in BigQuery, aggregated as far as the visuals allow.** Calculated tables (field parameters, calculation groups) hold no source data and are exempt.
  - Use a table from `agent_safe`, `agent_dashboard` or one the ML repo's code builds. Bring in the small result, not raw rows.
  - Check it with `list_tables` and `get_table_schema`. They cannot see `ml_model_results`; for a table there, rely on the plan and `Notebooks/evaluation/model_evaluation.ipynb`, which builds it.
  - If the table does not exist yet or is finer than the visuals need, make no model change and stop. End with a blocking question telling the user it needs a code plan first, then resubmit the dashboard part.
- **Do not add security roles.** A model with row-level security cannot be queried by the service principal the analytics agent uses, which would break it.
- **Describe everything you add.** The analytics agent reads descriptions from the live model. Write them as `references/ai-readiness.md` and `references/tmdl-guidelines.md` describe: a `///` line above the object, never a `description:` property.
- **Every measure needs a `formatString`.**
- **Match the neighbors' names.** This model mixes styles (`_Model_Evaluation_Measures`, `Pct_Diff_F1`, `Recall_at_5`), so copy the style of the nearest existing object instead of the defaults in the naming reference. A name given in the plan wins.
- **Leave generated metadata alone.** Keep every existing `lineageTag` and `PBI_*` annotation as it is, and omit `lineageTag` on new objects.

## Modify the model

1. Find what exists, in the TMDL files under `dashboard/pbip` (list the table files and search them for the object) and in the published model (`run_dax_query` with `INFO.VIEW.TABLES()`, `INFO.VIEW.COLUMNS()`, `INFO.VIEW.MEASURES()`, `INFO.VIEW.RELATIONSHIPS()`, which return names, descriptions and format strings but not a measure's DAX). If you notice a significant difference between them, end with a blocking question, unless the plan says to publish over it. Write what you learn to `/tmp/model-inventory.md`, adding to it if it already exists.
2. Read the reference you need (table below) before writing TMDL.
3. Plan the change and check for a duplicate or a naming conflict.
4. Edit in this order: columns, then relationships, then measures. Measures go before columns inside a table file.
5. To test a draft measure, use `run_dax_query` with `DEFINE MEASURE '<table>'[<name>] = <expression> EVALUATE ROW("v", [<name>])`.

## Add or change a table or column

A new table or column has no rows in the published model until the model is refreshed. Do this every time you add one.

1. Make the TMDL change. For a new BigQuery table, copy the `partition ... = m` block of an existing BigQuery-backed table such as `evaluation_metrics.tmdl`, changing only the schema and table names.
2. Publish with `publish_dashboard`.
3. Refresh the model with `refresh_dashboard_model`.
4. Check the data arrived with `run_dax_query`, for example `EVALUATE ROW("rows", COUNTROWS('<table>'))` and a sample of the new column. Build visuals on it only once it returns rows. If the refresh fails or no rows arrive, end with a blocking question that gives the reason.

## Author a field parameter

Read `references/field-parameters.md` and follow its create, edit or rename workflow. Create one only if the plan asks for it.

## Check your TMDL

Check your own TMDL text: tab indentation, a `///` description, a `formatString`, single quotes around names with spaces. `validate_dashboard` checks report files only and does not read TMDL. The model's TMDL is checked at publish, and a malformed file is rejected with the reason.

## References

| File | Read it when |
|---|---|
| `references/tmdl-guidelines.md` | Before writing any TMDL: syntax, file layout, relationships, hierarchies, calculation groups, user-defined functions, descriptions, format strings |
| `references/naming-conventions.md` | When naming a new measure, column or display folder; the existing model's names win over these defaults |
| `references/field-parameters.md` | For any field-parameter table: create, edit, rename or delete |
| `references/modeling-guidelines.md` | Before adding a table, column, relationship or hierarchy: star schema, data types, hiding keys, explicit measures, format strings |
| `references/dax-guidelines.md` | Before writing a measure or a `run_dax_query` query: DAX patterns, `DEFINE MEASURE`, `SUMMARIZECOLUMNS`, time intelligence |
| `references/metadata-discovery.md` | To look up what the model contains with `INFO.VIEW.*` queries: tables, columns, measures, relationships, dependencies |
| `references/ai-readiness.md` | When writing descriptions and names the analytics agent will read |
| `references/pbip.md` | To see how the project folder, `definition.pbism` and `definition.pbir` fit together. Do not rebind the report or edit these files |
