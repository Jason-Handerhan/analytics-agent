# Semantic Model AI Readiness Guidelines

This document describes how to make a semantic model easy for a natural-language consumer to use. Here that consumer is the analytics agent, which reads the live model's names and descriptions.

## Core Principles

- **Foundation first** - A weak model cannot be rescued by AI instructions. Star schema, explicit measures, and correct data types are prerequisites, not optional polish.
- **Literal interpretation** - The analytics agent reads names and descriptions literally. `AMT` is not the same as `Sales Amount` to a language model.
- **Explicit over implicit** - Implicit measures, extension measures defined in reports, and hidden business logic are invisible to AI. If it must be reachable by natural language, it must live in the model as an explicit measure with metadata.

### 1. Model Architecture (Foundation)

Without this, the analytics agent will produce poor results regardless of any other configuration. Load [modeling-guidelines.md](./modeling-guidelines.md) for full rules; this section flags the items that matter most for the analytics agent.

**DO:**
- Apply a star schema with clear fact and dimension tables. Avoid flat, denormalized, or pivoted structures.
- Define correct `dataType` on all columns. The analytics agent derives summarization and filter behavior from the data type.
- Create explicit DAX measures for every key metric. Implicit aggregations are not reliably accessible to the analytics agent.
- Anticipate common questions: include predefined measures users are most likely to request (e.g., `YTD Sales`, `MoM Growth`, `ROI`, `CAC`, `LTV`). The analytics agent answers more reliably when the metric already exists as an explicit measure.
- Consolidate or clearly differentiate duplicate or overlapping measures.
- Define logical hierarchies on dimension tables to support drill-down (e.g., `Year > Quarter > Month > Day` on a date dimension; `Country > State > City` on a geography dimension).
- Set `summarizeBy` correctly on numeric columns - especially `None` for IDs, year, month number, postal codes, etc., to prevent accidental sums.
- Set the `isDefaultLabel` on the column that defines the natural label for each dimension table, allowing the analytics agent to identify the table's default "name" column (for example, **Product Name** in the **Product** table).

**DON'T:**
- Leave unused columns or tables in the model - they pollute the schema the analytics agent sees and increase the chance of wrong field selection.

### 2. Descriptions

Descriptions provide context that names alone cannot convey. Descriptions written for AI consumers are different from descriptions written for human users.

**DO:**
- Add a `///` description line above every visible table, column, and measure.
- **Keep descriptions concise and front-load the most important information** (preferred usage, disambiguation, units) at the start of the description.
- Make implicit knowledge explicit. Do not restate the field name.
- For descriptions the agent reads, prefer: preferred-usage guidance, disambiguation against similar fields, expected grain, and units.
- For Calculation Groups: calculation *items* are not surfaced as model metadata, so use the description of the calculation group **column** to enumerate items and how to use them (e.g., "Use with measures and date table for: Current, MTD, QTD, YTD, PY, YOY, YOY%").

**DON'T:**
- Generate descriptions purely from AI without business context. AI-only descriptions tend to restate what the model structure already shows. Base them on the table's definition and its SQLX, and flag any assumption in your report.
- Contradict descriptions across related fields (the column description and the measure description should agree).
