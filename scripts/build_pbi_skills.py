"""Builds the Power BI skills' reference folders from Microsoft's powerbi-authoring plugin (MIT).

Copies each listed file, applies the recorded cuts, and fails if a cut does not match exactly as many times as stated or if a
forbidden phrase is left in the result. The `powerbi-report` skill's SKILL.md and references are built here from Microsoft's own
files; the `semantic-model` skill's SKILL.md and our other notes in a skill folder are written by hand and never touched.
LICENSE is copied too.

    uv run python scripts/build_pbi_skills.py --source <plugin root> --license <LICENSE file>
"""
import argparse
import pathlib
import re
from dataclasses import dataclass, field

SKILLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "coding_agent_app" / "skills"

# Phrases that describe tools or steps the coding agent does not have.
FORBIDDEN = ("ask_user", "MCP", "Tier 1", "Tier 2", "az rest", "x-ms-fabric-skill", "getDefinition", "updateDefinition",
             "table_operations", "partition_operations", "column_operations", "CreateFieldParameter", "UpdateFieldParameter")


@dataclass
class Replace:
    old: str
    new: str
    count: int = 1  # 0 means every match, at least one


@dataclass
class DropFrom:
    marker: str


@dataclass
class DropRange:
    start: str
    end: str


@dataclass
class DropLine:
    prefix: str  # removes the one line that starts with this


@dataclass
class Append:
    text: str


@dataclass
class AppendSlice:
    """Adds a slice of another source file at the end: from `start` to `end` (or the end of that file), with its own cuts applied."""
    source: str
    start: str
    end: str | None = None
    cuts: list = field(default_factory=list)


@dataclass
class Reference:
    source: str
    dest: str
    cuts: list = field(default_factory=list)
    root: bool = False  # written to the skill's own folder (SKILL.md), not to references/


SAVING = "per the skill's **Saving Changes** and **Validation Checklist**"

CONTINUATION = Replace(" Open this file directly from the skill reference index.", "")


def authoring(name: str, *cuts) -> Reference:
    return Reference(f"skills/powerbi-report-cli/references/authoring/{name}.md", f"{name}.md", list(cuts))


def design(name: str, *cuts, folder: str = "design") -> Reference:
    return Reference(f"skills/powerbi-report-cli/references/{folder}/{name}.md", f"{name}.md", list(cuts))


FILTERS = Replace("page count, or filtering depth", "page count, or desired filters/slicers", count=0)
AUTHORING_MODE = Replace("the `authoring` mode", "authoring", count=0)
DESIGN_MODE = Replace("the `design` mode", "design", count=0)

SKILLS = {
    "semantic-model": [
        # The plugin's UDF section here shows a syntax that Microsoft Learn contradicts, so it is replaced by the Learn syntax, naming rules and
        # limits (Microsoft Learn, "Use DAX user-defined functions"), then the plugin's own type system and best practices from dax-guidelines.md.
        Reference("skills/semantic-model-authoring/references/tmdl-guidelines.md", "tmdl-guidelines.md", [
            DropRange("## DAX User-Defined Functions (UDFs)\n", "## Calculated Tables\n"),
            Append(
                "## DAX User-Defined Functions (UDFs)\n\n"
                "A user-defined function is a DAX calculation defined once in the model and called by name from measures, calculated columns and other "
                "functions. Functions are declared in `definition/functions.tmdl`, a top-level file (not inside a table).\n\n"
                "Syntax:\n\n"
                "```tmdl\n/// Optional description above the function\n"
                "function <FunctionName> = ( [<ParameterName> [: <ParameterType>] [= <DefaultExpression>], ...] ) => <FunctionBody>\n```\n\n"
                "Example:\n\n"
                "```tmdl\n/// AddTax takes in amount and returns amount including tax\n"
                "function AddTax = (amount : NUMERIC) => amount * 1.1\n```\n\n"
                "- Describe a function with `///`. A `//` or `/* */` comment is not a description.\n"
                "- A parameter with a default expression (`= <DefaultExpression>`) is optional.\n"
                "- A function name is unique in the model, may contain periods for namespacing (not at the start or end, not two in a row), and "
                "otherwise only letters, digits and underscores. It must not clash with a built-in DAX function or a reserved word (`measure`, "
                "`function`, `define`). A parameter name has only letters, digits and underscores and is not a reserved word.\n"
                "- Not supported: recursion (including between functions), overloading, an explicit return type, and parameter descriptions. A function "
                "cannot be hidden, placed in a display folder, or combined with translations.\n"
                "- Functions need the model's `compatibilityLevel` in `definition/database.tmdl` to be 1702 or higher. If it is lower, raising it is a "
                "model-wide change: make it only when the plan needs a function, and say so in your report.\n"),
            AppendSlice("skills/semantic-model-authoring/references/dax-guidelines.md", "### Type System\n", "### Examples\n"),
            AppendSlice("skills/semantic-model-authoring/references/dax-guidelines.md", "### Best Practices\n", None),
        ]),
        Reference("skills/semantic-model-authoring/references/naming-conventions.md", "naming-conventions.md", [
            Replace("ask via `ask_user` what terminology their organization uses",
                    "follow the terminology the existing model already uses"),
            Replace("consumed by Copilot or data agents", "consumed by the analytics agent", count=2),
            Append("**In TMDL, a description is the `///` line or lines directly above an object** (Power BI Desktop's Description box writes them). "
                   "There is no `description:` property line. Wherever this file says to give an object a `description`, write it that way."),
        ]),
        Reference("skills/semantic-model-authoring/references/field-parameters.md", "field-parameters.md", [
            Replace("If the fields are ambiguous, STOP and use `ask_user`.",
                    "If the fields are ambiguous, choose the most likely set and record the assumption in your report."),
            Replace(
                "4. **Build** per the skill's **Tool Selection Priority**:\n"
                "   - **Tier 1 (MCP, preferred)** - use the `CreateFieldParameter` operation of `table_operations`. Follow the tool definition for the current request shape.\n"
                "   - **Tier 2 (no MCP)** - hand-author the field parameter TMDL. Load `tmdl-guidelines.md` and, for PBIP, `pbip.md`; for a Fabric workspace source, round-trip via `getDefinition` -> edit -> `updateDefinition` as described in `semantic-model-rest-api.md`.",
                "4. **Build** - hand-author the field parameter TMDL. Load `tmdl-guidelines.md`."),
            Replace(SAVING, "per **Saving and validating** in SKILL.md", count=3),
            Replace(
                "> **No dedicated update operation.** The current MCP has **no**\n"
                "> `UpdateFieldParameter`. The field set lives entirely in the calculated",
                "> **An edit is a partition rewrite.** The field set lives entirely in the calculated"),
            Replace(
                "3. **Rewrite the partition** per the skill's **Tool Selection Priority**:\n"
                "   - **Tier 1 (MCP, preferred)** - use the `Update` operation of `partition_operations` to replace the calculated partition expression with the complete new tuple set. Follow the tool definition for the current request shape.\n"
                "   - **Tier 2 (no MCP)** - rewrite the partition `source` in the field parameter TMDL; leave the three columns and `ParameterMetadata` untouched.",
                "3. **Rewrite the partition** - rewrite the partition `source` in the field parameter TMDL with the complete new tuple set; leave the three columns and `ParameterMetadata` untouched."),
            Replace(
                "   - **Rename the table** - `table_operations`, `Operation: \"Rename\"` (Tier 1), or rename the `table` in TMDL (Tier 2).\n"
                "   - **Rename the slicer label** - rename the visible label column with `column_operations` (Tier 1), or the visible `column` in TMDL (Tier 2).\n"
                "   - **Delete** - `table_operations`, `Operation: \"Delete\"` (Tier 1), or remove the table's TMDL file (Tier 2).",
                "   - **Rename the table** - rename the `table` in TMDL.\n"
                "   - **Rename the slicer label** - rename the visible `column` in TMDL.\n"
                "   - **Delete** - remove the table's TMDL file."),
            Replace("`table_operations` Delete (Tier 1) or remove the TMDL file (Tier 2).", "remove the TMDL file."),
            Replace(
                "- Tier 1 `table_operations` `CreateFieldParameter` over hand-editing TMDL when\n"
                "  creating -- it generates the shape, metadata, and escaping for you.\n"
                "- To edit an existing parameter, rewrite the calculated partition source with\n"
                "  `partition_operations` (supply the complete new `{ ... }` set) over\n"
                "  hand-diffing individual tuples, and preserve the columns and\n"
                "  `ParameterMetadata`.",
                "- To edit an existing parameter, rewrite the calculated partition source with\n"
                "  the complete new `{ ... }` set instead of hand-diffing individual tuples,\n"
                "  and preserve the columns and `ParameterMetadata`."),
            Replace("group-by out of sync (Tier 2 hand-edits).", "group-by out of sync (hand-edits)."),
            Replace("Re-create via Tier 1 or restore the extended property.", "Restore the extended property."),
            Replace(
                "| `UpdateFieldParameter` not recognized | There is no such operation. Edit via `partition_operations` (Tier 1) or TMDL (Tier 2) as in [Edit a field parameter](#workflow-edit-a-field-parameter). |\n",
                ""),
            Replace("- [Examples](#examples)\n", ""),
            DropFrom("\n---\n\n## Examples\n"),
        ]),
        Reference("skills/semantic-model-authoring/references/pbip.md", "pbip.md", [
            DropFrom("\n## References\n"),
        ]),
        Reference("skills/semantic-model-authoring/references/modeling-guidelines.md", "modeling-guidelines.md", [
            Replace("This document describes the modeling guidelines that should be applied when developing against a Power BI semantic model.",
                    "This document describes the modeling guidelines that should be applied when developing against a Power BI semantic model.\n\n"
                    "In TMDL a description is a `///` line directly above the object, not a `description` property."),
            Replace("When in doubt, ask the user if the column is really required.",
                    "When in doubt, leave the column out unless a visual or measure needs it."),
            DropRange("## Naming Conventions\n", "## Tables\n"),
            Replace("- Start by creating the partition and the query to the data source. For Direct Lake models (see [Direct Lake Modeling Guidelines](direct-lake-guidelines.md)).\n",
                    "- Start by creating the partition and the query to the data source.\n"),
            Replace("- Default to M partition source type with import mode. Unless it's a Direct Lake Model.\n",
                    "- Default to M partition source type with import mode.\n"),
            Replace("- Use Power Query M code for tables with Direct Lake storage.\n", ""),
            Replace("| Entity (EntityPartitionSource)         | DirectLake                    | There is no M expression. Used when it's connecting the semantic model table to a data source entity that can be a Lakehouse table. |\n",
                    ""),
            Replace("- **Only when working against a online database**\n"
                    "  - When creating a measure test it with a simple query.\n"
                    "  - Ensure the measure has no errors. Run the query `EVALUATE INFO.MEASURES()` and inspect the column `ErrorMessage`.\n",
                    "- When creating a measure, test it with a simple query using `run_dax_query`.\n"
                    "- After publishing, check the measure with `INFO.VIEW.MEASURES()` and confirm its `State` is `Valid`.\n"),
            DropRange("## Row-Level Security (RLS)\n", "## Descriptions & Documentation\n"),
            DropRange("## Maintenance\n", "## Parameters\n"),
            DropFrom("\n## Parameters\n"),
        ]),
        Reference("skills/semantic-model-authoring/references/semantic-model-ai-readiness.md", "ai-readiness.md", [
            Replace("This document describes the guidelines that should be applied when preparing a Power BI semantic model for conversational BI experiences in Microsoft Fabric (Copilot for Power BI, Data Agents, and other natural-language consumers).",
                    "This document describes how to make a semantic model easy for a natural-language consumer to use. Here that consumer is the analytics agent, which reads the live model's names and descriptions."),
            DropRange("## Consumption Mode Decides the Investment\n", "## Core Principles\n"),
            DropRange("## Supported Editing Routes\n", "### 1. Model Architecture (Foundation)\n"),
            DropRange("### 2. Model Naming\n", "### 3. Descriptions\n"),
            DropFrom("\n### 4. AI Instructions\n"),
            Replace("- **Iterate from observation** - Write descriptions and AI instructions based on real user prompts and Copilot answers. Do not guess.\n", ""),
            Replace("Copilot reads names and descriptions literally.", "The analytics agent reads names and descriptions literally."),
            Replace("- Add a `description` to every visible table, column, and measure.",
                    "- Add a `///` description line above every visible table, column, and measure."),
            Replace("- **Keep descriptions concise: only the first 200 characters are read by Copilot.** Front-load the most important information (preferred usage, disambiguation, units) at the start of the description.",
                    "- **Keep descriptions concise and front-load the most important information** (preferred usage, disambiguation, units) at the start of the description."),
            Replace("- For Copilot-focused descriptions, prefer:", "- For descriptions the agent reads, prefer:"),
            Replace(" Same 200-character limit applies.", ""),
            Replace("- Where both audiences need guidance, put human-oriented detail in the `description` and Copilot-specific routing rules in [AI instructions](#4-ai-instructions).\n", ""),
            Replace("Always validate with the user or a domain expert.",
                    "Base them on the table's definition and its SQLX, and flag any assumption in your report."),
            Replace("### 3. Descriptions", "### 2. Descriptions"),
            Replace(". Copilot derives", ". The analytics agent derives"),
            Replace("`LTV`). Copilot answers", "`LTV`). The analytics agent answers"),
            Replace("Copilot", "the analytics agent", count=0),
        ]),
        Reference("skills/semantic-model-authoring/references/dax-guidelines.md", "dax-guidelines.md", [
            DropFrom("\n---\n\n## DAX User-Defined Functions (UDFs)\n"),
        ]),
        Reference("skills/semantic-model-authoring/references/metadata-discovery.md", "metadata-discovery.md", [
            Replace("Use this reference when discovering model metadata through **DAX INFO functions** instead of `powerbi-modeling-mcp` get/list tools. ",
                    "Use this reference with `run_dax_query` to discover model metadata."),
            DropFrom("- **Do not use `INFO` DAX functions to retrieve role memberships**"),
        ]),
    ],
    "powerbi-report": [
        # --- the dispatcher, SKILL.md: Microsoft's own, with management dropped and the preview, Fabric and approval parts cut
        Reference("skills/powerbi-report-cli/SKILL.md", "SKILL.md", root=True, cuts=[
            Replace("name: powerbi-report-cli", "name: powerbi-report"),
            Replace('description: "Plan, design, author, preview, publish, and manage Power BI reports across requirements, page design, local PBIR/PBIP edits, '
                    'validation, screenshots, Fabric upload/download, and rebinding. Use for report lifecycle work; use semantic-model-authoring for model or DAX '
                    'changes and fabriciq for data questions. Triggers: plan Power BI report, design report page, edit PBIR, preview PBIP, publish report, rebind report"',
                    "description: Edit this dashboard's Power BI report by hand in its PBIR files, including adding or changing pages, visuals, cards, charts, "
                    "slicers, filters, bookmarks, buttons, themes and formatting. Use whenever a task touches the report under dashboard/pbip (the folder "
                    "ending in .Report). The semantic model's tables, columns and measures are covered by the semantic-model skill."),
            Replace("metadata:\n  version: 1.0.5\n", ""),
            DropRange("> **Telemetry — MANDATORY.**", "# Power BI Reports - CLI Skill\n"),
            Replace("# Power BI Reports - CLI Skill\n", "# Power BI Report\n"),
            Replace("This skill owns the Power BI report lifecycle end to end: requirements and\nscope, visual design, local PBIR/PBIP authoring and preview, and report-item\ntransport to and from Microsoft Fabric.",
                    "This skill covers the dashboard's report in three modes: planning (check the plan against the dashboard and the model before building),\n"
                    "design (decide how a new page or visual looks), and authoring (edit the PBIR files, validate, publish with `publish_dashboard` and render). A mode that does not\n"
                    "apply is skipped. Model changes (measures, columns, tables) belong to the `semantic-model` skill; this skill covers the report only."),
            DropRange("| Mode | Use when the request... | Existing route examples | Read first |", "## Mode Boundary Rules\n"),
            Replace("## Mode Boundary Rules\n",
                    "| Mode | Use when the request... | Read first |\n|---|---|---|\n"
                    "| `planning` | starts a dashboard task: the plan is checked against the dashboard and the model, the model is inventoried, and anything vague is settled before building | [references/planning.md](references/planning.md) |\n"
                    "| `design` | adds a new page, or a visual with nothing similar to copy: archetype, chart choice, layout, color, typography, theme, accessibility | [references/design.md](references/design.md) |\n"
                    "| `authoring` | edits the PBIR files, validates them, publishes to the Agent Sandbox workspace with `publish_dashboard`, and renders the result: pages, visuals, filters, slicers, bookmarks, themes, formatting | [references/authoring.md](references/authoring.md) |\n\n"
                    "## Mode Boundary Rules\n"),
            Replace("- `authoring` changes local files and performs preview/validation. It does not\n  publish to Fabric.\n"
                    "- `management` transports definitions and manages Fabric report items. It does\n  not invent or directly author PBIR content.\n"
                    "- `planning` owns the guided requirements-to-approval flow for a new report. A\n  focused edit to an existing report goes directly to `authoring`.",
                    "- `authoring` edits the files, validates them, publishes to the Agent Sandbox workspace\n  with `publish_dashboard`, and renders the result.\n"
                    "- `planning` checks the plan before any edit. It is short for a focused edit to an\n  existing page, which then goes straight to `authoring` without `design`."),
            Replace("A greenfield request can span modes in this order:\n\n```text\nplanning -> design -> authoring -> management\n```\n\n"
                    "Handle one mode at a time. Announce each switch and read the new mode reference\nbefore starting that part. The `planning` approval gate is a turn boundary:\n"
                    "never build or publish until the user explicitly approves the locked spec in a\nlater reply.",
                    "A task that adds a page, or a visual with nothing to copy, spans modes in this order:\n\n```text\nplanning -> design -> authoring\n```\n\n"
                    "A task that only edits existing pages is `planning -> authoring`.\n\n"
                    "Handle one mode at a time. Announce each switch and read the new mode reference\nbefore starting that part."),
            DropRange("> **GREENFIELD APPROVAL BARRIER — MANDATORY.**", "## Required Deliverable by Mode\n"),
            Replace("| `planning` | Persist `_brief/report-spec.md` or the user-named equivalent, ask for explicit approval, and stop before implementation. |",
                    "| `planning` | The model inventory and the todo list. A plan that is vague, or missing something essential, ends the task with a blocking question instead. |"),
            Replace("| `design` | Produce the complete `Design Brief:` contract, including `design_identity` and a page archetype/layout contract for every page. Do not edit PBIR or call Fabric APIs. |",
                    "| `design` | The design for each new page or visual: archetype and variant, chart choices, and the layout the plan gives, checked against the pre-flight list. Do not edit PBIR. |"),
            Replace("| `authoring` | Persist the requested PBIR/PBIP changes, validate each logical batch, then complete the selected preview and screenshot-review workflow required by the authoring reference. Do not publish. |",
                    "| `authoring` | The requested PBIR and model changes, validated after each batch, then published with `publish_dashboard`, the last publish being the final state of the files, and every changed page rendered and reviewed after it. |"),
            Replace("| `management` | Complete the requested Fabric report operation, including LRO polling and documented readback verification. |\n", ""),
            Replace("- In `planning` mode, also read `references/planning-part-02.md` before\n  producing `_brief/report-spec.md`; it contains the required canonical design\n  contract and approval checks.\n", ""),
            Replace("- Across a multi-turn task, retain a compact working state containing resolved\n  workspace/item/model IDs, local and downloaded paths, and references already\n"
                    "  read. Reuse it in later turns unless the user explicitly requests a fresh\n  query or the preceding operation invalidated a value.\n", ""),
            Replace("- Keep local edits local unless the user explicitly requests publishing.\n- Resolve Fabric workspace and item IDs by listing and filtering rather than\n  guessing identifiers.\n", ""),
            Replace("- Preserve every argument and safety boundary documented by the selected mode.\n", ""),
            Replace("prior approved spec", "the plan"),
            Replace("- Loading a retired sibling skill name; planning, design, authoring, and\n  management are modes of this skill.\n", ""),
            Replace("- Reloading this dispatcher, a mode file, or a topic reference already read in\n  the current task solely because the conversation advanced to a later turn.\n", ""),
            Replace("- Preserve the user's existing project structure, schemas, host, operation,\n  binding, and delivery target unless the selected mode explicitly requires a\n  change.\n", ""),
            Replace("- Editing PBIR or calling Fabric APIs in `planning` or `design`.\n- Publishing from `authoring`, or authoring PBIR content from `management`.\n"
                    "- Bypassing the planning approval gate because the original request also asked\n  to build or publish.",
                    "- Editing PBIR in `planning` or `design`."),
            DropFrom("\n<!-- BEGIN GENERATED REFERENCE INDEX -->"),
        ]),

        # --- planning mode: the coding agent checks the plan it was given; the interview and approval belong to the main agent
        Reference("skills/powerbi-report-cli/references/planning.md", "planning.md", [
            DropRange("## Contents\n", "# powerbi-report-cli planning mode -- Power BI Report Planning\n"),
            Replace("# powerbi-report-cli planning mode -- Power BI Report Planning\n",
                    "# Planning Mode: Settle the Plan Before Building\n\n"
                    "The plan you were given was settled with the user before this task started, so this mode interviews no one. It checks the plan against "
                    "the real dashboard and model before you edit anything, so the build starts from facts.\n\n"
                    "1. Check the plan for vagueness before anything else. For a new page or dashboard it is vague if audience, purpose, page count, or "
                    "desired filters/slicers are missing; for an edit, if it does not say which page to change and how. If it is vague, stop and end the task with a blocking question.\n"
                    "2. Inventory the published dashboard (`powerbi-report-author preview-pages` and `preview-visuals`) and the published model (`run_dax_query` with "
                    "`INFO.VIEW.TABLES()`, `COLUMNS()`, `MEASURES()` and `RELATIONSHIPS()`), using the summary under *Model inventory*, and write that summary to `/tmp/model-inventory.md`. "
                    "Then compare it with the files you start from: if the published dashboard or model does not match them (a page, table, column or measure in one and not the other), "
                    "stop and end the task with a blocking question; publish over it only if the plan says the user knows about the mismatch and asked you to.\n"
                    "3. Check that every page, measure and field the plan names exists, or that you can build it from the model and the data in BigQuery, and that the plan "
                    "covers what is listed under *Slicers and interactions*. If something it names does not exist and its meaning is not clear enough to build, or something essential cannot be done, "
                    "stop and end the task with a blocking question. A smaller "
                    "gap is a sensible choice that you list as an assumption.\n"
                    "4. Write the todo list: model changes first, then each page's visuals, then `validate_dashboard`, `publish_dashboard` and `render_dashboard`.\n\n"
                    "**Deliverable:** the inventory and the todo list. Then read `design.md` for a new page or visual, or `authoring.md` for an edit.\n"),
            DropRange("> **Required continuation.**", "### Round 2 — Model Inventory and Scope\n"),
            Replace("### Round 2 — Model Inventory and Scope\n", "## Model inventory\n"),
            DropRange("Goal: inspect the model and define the first-build scope boundary without", "Summarize:\n"),
            DropRange("After inspection, infer the first-build scope from the original request", "### Round 3 — Narrative and Page Plan\n"),
            Replace("### Round 3 — Narrative and Page Plan\n", "## Slicers and interactions\n"),
            DropRange("Goal: turn the model and scope into page architecture.", "Capture slicers and interactions:\n"),
            Replace("Capture slicers and interactions:", "Check that the plan covers:"),
            Replace("### Round 4 — Design Identity, Accessibility, and Delivery\n", "## Design defaults\n"),
            DropRange("Goal: lock the design identity, accessibility baseline, and delivery target.", "Design defaults (these come from the design skill's gotchas + base theme;"),
            Replace("Design defaults (these come from the design skill's gotchas + base theme;\napplied automatically unless the user overrides):",
                    "These come from the design gotchas and the base theme, and apply unless the plan says otherwise:"),
            AppendSlice("skills/powerbi-report-cli/references/planning-part-02.md", "## Validation Standards\n", None, [
                Replace("- `definition.pbir` points to the expected semantic model.\n", ""),
                Replace("- Power BI Desktop opens the `.pbip`.\n- Desktop reload succeeds.\n"
                        "- Screenshot capture succeeds for at least the cover and any newly authored\n  pages.\n- If published, Fabric LRO returns `Succeeded`.",
                        "- `validate_dashboard` passes.\n- The last `publish_dashboard` call is the final state of the files.\n- Every changed page renders without errors after that publish."),
                Replace("- Generated PBIR is safer than hand-editing individual visual JSON files.\n- Model changes must be persisted before Desktop reload.\n",
                        "- Model changes must be published with `publish_dashboard` before a visual that uses them renders; otherwise it fails with `Missing_References`.\n"),
                Replace("- Desktop validation catches issues that JSON validation cannot.", "- Rendering catches issues that validation cannot."),
                Replace("- Fabric publishing is sensitive to `byConnection` schema and theme paths.\n", ""),
            ]),
        ]),

        # --- authoring mode: Microsoft's authoring.md with the preview protocol cut, our tools in its workflow, and its own topic table
        Reference("skills/powerbi-report-cli/references/authoring.md", "authoring.md", [
            DropRange("## Contents\n", "# powerbi-report-cli authoring mode -- Power BI Report Authoring (PBIR/PBIP)\n"),
            Replace("# powerbi-report-cli authoring mode -- Power BI Report Authoring (PBIR/PBIP)\n",
                    "# Authoring Mode: Power BI Report (PBIR/PBIP)\n\n"
                    "> **AUTHORING COMPLETION GATE — MANDATORY FOR REPORT EDITS**\n>\n"
                    "> **Validation and rendering are paired. A passing `validate_dashboard` is never task completion.** Any task that creates or "
                    "modifies rendered report content is incomplete until:\n>\n"
                    "> 1. `validate_dashboard` passes.\n"
                    "> 2. The dashboard is published with `publish_dashboard`, and the last publish is the final state of the files.\n"
                    "> 3. Every affected page is rendered with `render_dashboard` after that publish.\n"
                    "> 4. The renders are reviewed using `screenshot-review.md`.\n"
                    "> 5. Visible issues are fixed and the `validate_dashboard`, `publish_dashboard`, `render_dashboard` and review loop is repeated.\n>\n"
                    "> Render each page changed by page, visual, filter, slicer, bookmark or layout edits, and every page for report-wide changes such as "
                    "themes or global formatting. If a page cannot be rendered or reviewed, say so in your report instead of claiming completion.\n"),
            Replace("This skill enables reading", "This mode enables reading"),
            DropRange("- **Desktop terminal rule (highest priority):**", "- Use this skill only for concrete PBIR/PBIP report-file mechanics"),
            Replace("- Use this skill only for concrete PBIR/PBIP report-file mechanics such as pages, visuals, filters, slicers, navigation, bookmarks, themes, formatting, validation, previews, and screenshots.",
                    "- Use this mode only for concrete PBIR/PBIP report-file mechanics such as pages, visuals, filters, slicers, navigation, bookmarks, themes, formatting, validation, publishing, and renders."),
            Replace("- Validate PBIR with `powerbi-report-author validate` after each logical batch.\n  Every successful schema validation must continue into fresh screenshot\n"
                    "  capture and review; never stop at the validation result.",
                    "- Validate PBIR with `validate_dashboard` after each logical batch. Every successful\n  validation must continue into `publish_dashboard`, a fresh render and a review; never stop\n  at the validation result."),
            DropRange("- Use the `powerbi-report-author preview` command for rendered-output", "- Use CLI capability lookup before writing visual roles, formatting objects"),
            Replace("- Use CLI capability lookup before writing visual roles, formatting objects, enum values, selectors, or expression encodings.",
                    "- Use CLI capability lookup before writing visual roles, formatting objects, enum values, selectors, or expression encodings.\n"
                    "- When editing an existing dashboard, copy the neighbors unless the plan says otherwise: start a new visual from an existing visual of the same "
                    "type and keep its formatting, sizing and placement. For a new dashboard, follow the sizing formulas unless the plan gives you a reference to copy.\n"
                    "- Leave `definition.pbir` (the publish tool sets its model binding), `.pbi/` and the `.pbix` file alone.\n"
                    "- A visual on a measure that is not yet in the published model fails with `Missing_References`: add the measure first (the `semantic-model` skill), then run `publish_dashboard`."),
            Replace("- Start from an approved `Design Brief:` or `_brief/report-spec.md` for greenfield report builds.",
                    "- Start from the plan and, for a new page or visual, the design from `design.md`."),
            Replace("- For semantic model metadata or model-side changes, use a semantic-model authoring skill, Power BI Modeling MCP, or local TMDL files when available.",
                    "- For model changes (measures, columns, tables), use the `semantic-model` skill."),
            Replace("open-ended design, report planning, or Fabric report item CRUD; pair it with the `design` mode, the `planning` mode, or the `management` mode.",
                    "open-ended design or report planning; pair it with the `design` mode or the `planning` mode."),
            DropFrom("\n## Quick Start Workflow\n"),
            Append(
                "## Quick Start Workflow\n\n"
                "1. **Plan, then design if it is new** → `planning.md` runs first. For a new page, or a visual with nothing similar to copy, read `design.md` "
                "next. A focused edit to an existing page comes straight here.\n"
                "2. **Use the model inventory** → planning wrote it to `/tmp/model-inventory.md`: read it, and query the model again (`run_dax_query`, "
                "`INFO.VIEW.*`) or read the TMDL files only for what it does not cover. Model changes belong to the `semantic-model` skill.\n"
                "3. **Route by intent** → use **Topic Files and Examples** below to pick the relevant guide.\n"
                "4. **Use CLI metadata** → see `powerbi-report-author-cli.md` for exact visual roles, formatting objects, property names, enum values and "
                "selector requirements; do not infer these from memory.\n"
                "5. **Check common pitfalls** → read `pitfalls.md` before editing when the change touches visuals, bindings, filters, formatting, layout or "
                "rendered output.\n"
                "6. **Validate** → run `validate_dashboard` after every logical batch of PBIR changes, then always continue to step 7.\n"
                "7. **Publish and render** → publish with `publish_dashboard`, render every affected page with `render_dashboard`, and review the renders "
                "using `screenshot-review.md`. Do not proceed until both validation and visual review pass. For dashboard builds, page scaffolding is not "
                "completion: each requested page needs data-bound visuals.\n"
                "8. **Finish** → the last publish must be the final state of the files, with every changed page rendered after it. Give the user a concise "
                "summary of what was done and any issues (major and minor).\n"),
            Append(
                "\n## Deleting a page, visual or bookmark\n\n"
                "Remove it and everything that points at it.\n\n"
                "- **A page:** delete its folder under `definition/pages/` and its entry in `pageOrder` in `pages.json`. If `activePageName` was that page, set it to another page.\n"
                "- **A visual or bookmark:** delete its folder or file and, for a bookmark, its entry in `bookmarks.json`.\n"
                "- **What points at it:** search the report files for its id and name, and remove or retarget each match: buttons that navigate to a page, "
                "bookmarks that save the page or visual, drillthrough and tooltip pages, slicer sync groups.\n"
                "- Then run `validate_dashboard`: it reports a bookmark or action that still points at something you removed.\n"),
            AppendSlice("skills/powerbi-report-cli/references/authoring-part-02.md", "## Topic Files and Examples\n", "### Greenfield / Design Handoff\n", [
                DropLine("| `model-binding.md`"),
                DropLine("| `preview.md`"),
                DropLine("| `version-control.md`"),
                Replace(", validation, and Desktop verification |", ", and validation |"),
                Replace("after preview capture", "after a render"),
                Replace("Use the user's intent to choose the relevant topic file(s) before editing:",
                        "Use the user's intent to choose the relevant topic file(s) before editing. Files named `part-XX` continue the named topic:"),
            ]),
            Append(
                "Also in this skill:\n\n| File | When to read |\n|------|-------------|\n"
                "| `pbir-layout.md` | The report's file tree, what each file is for, and the `$schema` rules |\n"
                "| `layout.md` | Placing or resizing a visual: the 8px grid, gaps, bounds, slicer placement |\n"
                "| `powerbi-report-author-cli.md` | The full command list of `powerbi-report-author` and how to read a validation result |\n"
                "| `pitfalls.md` | Before editing or after a surprising result: known PBIR traps and their fixes |\n"),
        ]),

        Reference("skills/powerbi-report-cli/references/authoring/card.md", "card.md"),
        Reference("skills/powerbi-report-cli/references/authoring/card-part-02.md", "card-part-02.md", [
            Replace(" Open this file directly from the skill reference index.", ""),
        ]),
        Reference("skills/powerbi-report-cli/references/authoring/card-part-03.md", "card-part-03.md", [
            Replace(" Open this file directly from the skill reference index.", ""),
        ]),
        Reference("skills/powerbi-report-cli/references/design/layout.md", "layout.md", [
            Replace("- [Space Allocation Rules](#space-allocation-rules)\n  - [Composition Templates](#composition-templates)\n", ""),
            Replace("Brownfield redesigns preserve the existing canvas size unless a\nresize is approved.",
                    "Brownfield redesigns preserve the existing canvas size unless a\nresize is approved. A new page in an existing report is designed as greenfield "
                    "but takes the report's canvas size."),
            Replace(" See\n`archetypes/analytical-canvas.md` for the full template.", ""),
            DropRange("## Space Allocation Rules\n", "## Power BI Grounding\n"),
        ]),
        Reference("skills/powerbi-report-cli/references/authoring/bookmark.md", "bookmark.md", [
            Replace("- [Desktop verification checklist](#desktop-verification-checklist)\n", ""),
            Replace("has no bookmarks, use the schema versions in the templates below and let\n   Desktop upgrade them on save.",
                    "has no bookmarks, use the schema versions in the templates below."),
            Replace("6. Run `powerbi-report-author validate <Report>.Report`.\n"
                    "7. Reload in Desktop and exercise every forward and reverse transition.",
                    "6. Run `validate_dashboard`.\n"
                    "7. Publish with `publish_dashboard`, then use `render_dashboard` to check every forward and reverse transition."),
            Replace("`powerbi-report-author validate` reports", "`validate_dashboard` reports"),
            DropRange("## Desktop verification checklist\n", "## Anti-patterns\n"),
        ]),
        authoring("button", Replace("3. For each clipped button, apply **one** fix and reload.", "2. For each clipped button, apply **one** fix and render again."),
                  Replace("After authoring, run the preview verification loop\n(preview.md (see `preview.md`)) and **inspect the screenshot of each button**:",
                          "After authoring, run `publish_dashboard` and look at the page with `render_dashboard`, and **inspect each button**:"),
                  Replace("1. `powerbi-report-author validate \"<folder>\"` →\n"
                          "   `powerbi-report-author preview \"<folder>\" --host desktop --status` (the\n"
                          "   unsaved-changes preflight required before every reload) →\n"
                          "   `powerbi-report-author preview \"<folder>\" --host desktop --reload` →\n"
                          "   `powerbi-report-author preview \"<folder>\" --host desktop --screenshot \"<file>\" --page <pageId>`.\n", ""),
                  Replace("2. Scan every button in the image", "1. Scan every button in the image")),
        authoring("button-part-02", CONTINUATION,
                  Replace("  - [Data Function (translytical task flow)](#data-function-translytical-task-flow)\n", ""),
                  Replace("| Data Function | `'DataFunction'` | `dataFunction`: structured item reference (see [Data Function](#data-function-translytical-task-flow)) | Invoke a Fabric User Data Function (translytical task flow) |\n", ""),
                  DropRange("### Data Function (translytical task flow)\n", "## Formatting Objects\n")),
        authoring("button-part-03", CONTINUATION),
        authoring("cartesian", Replace(" (see SKILL.md anti-patterns)", "")),
        authoring("cartesian-part-02", CONTINUATION,
                  Replace("Confirm with the user before setting `logAxisScale: true`**", "Do not set `logAxisScale: true`**"),
                  Replace("2. If negatives are possible, **warn the user before applying log scale**.\n"
                          ">    Explain that log scale is mathematically undefined for zero/negative values\n"
                          ">    and PBI will silently fall back to linear. Use the `ask_user` tool to\n"
                          ">    present alternatives and let the user choose:",
                          "2. If negatives are possible, **do not apply log scale**. It is mathematically\n"
                          ">    undefined for zero/negative values and PBI will silently fall back to\n"
                          ">    linear. Choose one alternative and say which in your report:"),
                  Replace("3. Apply `logAxisScale: true` only after the user resolves the negative values\n"
                          ">    via one of the alternatives above, or confirms that all values are positive.",
                          "3. Apply `logAxisScale: true` only when every bound value is confirmed positive.")),
        authoring("cartesian-part-03", CONTINUATION),
        authoring("cartesian-part-04", CONTINUATION),
        authoring("image",
                  Replace("If the user has not specified the source or just asks to add a new image, prompt the user with the source options. "
                          "Do NOT even render an empty image visual layout, prompt for the source first unless the user mentions to render only the layout specifically.",
                          "If the plan asks for an image but does not give its source (local file, URL or data field), or names a URL or file without giving it, "
                          "end with a blocking question. Do not create an empty image visual."),
                  Replace("warn the user before proceeding — the visual will render blank or error, and the user may not know this requirement. "
                          "Present alternatives (other ImageUrl fields, local file, URL, or adding `dataCategory: ImageUrl` in the semantic model) "
                          "and confirm the user's choice before creating.",
                          "the visual will render blank or error. Pick an alternative (other ImageUrl fields, local file, URL, or adding "
                          "`dataCategory: ImageUrl` in the semantic model) and say in your report what you chose and why."),
                  Replace("or after the user has chosen an alternative.", "or with the alternative you chose."),
                  Replace("**Warn the user before creating.**", "**Do not create it as requested.**"),
                  Replace(" Confirm the user's choice before creating.", " Say in your report which you chose."),
                  Replace("Inform the user that the semantic model has no fields with the ImageUrl data category. "
                          "Suggest the local file or URL alternatives and confirm before creating any data-bound image visual.",
                          "The semantic model has no fields with the ImageUrl data category. Use the local file or URL alternatives "
                          "and say so in your report."),
                  Replace("Prompt the user to select which field/measure to use. Do NOT randomly select one unless the user asks to do so.",
                          "Choose the field/measure whose name and description fit the request, and say which in your report.")),
        authoring("kpi"),
        authoring("map",
                  Replace("`powerbi-report-author validate` raises", "`validate_dashboard` raises"),
                  Replace("and tell the user why the map was replaced.", "and say in your report why the map was replaced."),
                  Replace("3. **Ask the user for clarification** — if you cannot resolve the geocoding issue,\n"
                          "   use `ask_user` to describe the problem and ask which field to use or whether\n"
                          "   lat/lon columns are available\n",
                          "3. **Choose the most likely field** — if you cannot resolve the geocoding issue,\n"
                          "   say in your report which fields you tried and why the map still fails\n"),
                  Replace("use `ask_user` before changing visual types. Substituting a non-map visual for a\n"
                          "   resolvable data problem violates the design brief.",
                          "keep the map and say what is wrong. Substituting a non-map visual for a\n"
                          "   resolvable data problem is not acceptable.")),
        authoring("shape"),
        authoring("table", Replace("`powerbi-report-author validate` with", "`validate_dashboard` with"),
                  Replace("tell them Power BI does not support conditional column\nwidth and offer the supported alternatives:",
                          "say in your report that Power BI does not support conditional column\nwidth, and use one of the supported alternatives:")),
        authoring("textbox"),
        authoring("custom-visuals",
                  Replace("and verifying the\nresult in Power BI Desktop.", "and verifying the\nresult."),
                  Replace("`powerbi-report-author validate`", "`validate_dashboard`", count=2),
                  Replace("[Desktop verification](#desktop-verification)", "[Verification](#verification)", count=2),
                  Replace("## Desktop verification\n", "## Verification\n"),
                  Replace("- After adding the registration and (for private visuals) the `CustomVisuals/`\n"
                          "  package, `powerbi-desktop reload` picks up the visual — a full close/reopen is\n"
                          "  **not** required, including for a brand-new custom visual added to an\n"
                          "  already-open report. If a private visual still shows *\"add it to this report\n"
                          "  first\"* after reload, the missing piece is almost always the\n"
                          "  `resourcePackages` entry (Part B), not the reload mechanism.\n"
                          "- **Org visuals load lazily from the tenant store.** The first reload/screenshot\n"
                          "  can come back blank while the tenant blob is still resolving. Wait ~15–20s and,\n"
                          "  if still blank, **reload a second time** before concluding it is broken.\n", ""),
                  Replace("always verify org visuals by rendering in Desktop, since `validate` cannot reach\n  the tenant store.",
                          "always verify org visuals by rendering with `render_dashboard`, since validation cannot reach\n  the tenant store."),
                  DropFrom("\n> **⚠️ Custom-visual clobber symptom.**")),
        authoring("conditional-formatting",
                  Replace("Read this when applying data-driven visual formatting in PBIR. Conditional",
                          "> **Read first:** `formatting-overview.md` — cascade and encoding.\n\n"
                          "Read this when applying data-driven visual formatting in PBIR. Conditional"),
                  Replace("Explain the limitation and offer a static\n   value or a supported built-in visual/target.",
                          "Use a static\n   value or a supported built-in visual/target, and say why in your report."),
                  Replace("5. Run PBIR validation, load the latest report, and review a screenshot with\n"
                          "   representative nonblank, blank, and invalid values where those cases can\n   occur.",
                          "5. Run `validate_dashboard`, `publish_dashboard`, and check the render. Cover representative\n"
                          "   nonblank, blank, and invalid values where those cases can occur."),
                  Replace("Replace it with an exact catalog name before reloading", "Replace it with an exact catalog name before publishing"),
                  Replace("Run `powerbi-report-author validate <path>`, reload\n   the report in Desktop, and confirm that the formatting appears.",
                          "Run `validate_dashboard`, `publish_dashboard`, and confirm with\n   `render_dashboard` that the formatting appears.")),
        authoring("conditional-formatting-part-02", CONTINUATION),
        authoring("conditional-formatting-part-03", CONTINUATION,
                  Replace("delegate the model-side\n> change to the `semantic-model-authoring` skill / Power BI Modeling MCP (out of\n"
                          "> scope for this skill), or ask the user to add or fix the measure.",
                          "make the model-side\n> change with the `semantic-model` skill.")),
        authoring("conditional-formatting-part-04", CONTINUATION,
                  Replace("`powerbi-report-author validate` reports it\n> as `PBIR_FILLRULE_STOP_DOUBLE_WRAP`; still verify gradients after a Desktop\n> reload.",
                          "`validate_dashboard` reports it\n> as `PBIR_FILLRULE_STOP_DOUBLE_WRAP`; still verify gradients with `render_dashboard`.")),
        authoring("conditional-formatting-part-05", CONTINUATION,
                  Replace("(needs a local `.pbip` semantic model or a live model via Modeling\n> MCP)",
                          "(read the model's TMDL files or query it with `run_dax_query`)")),
        authoring("conditional-formatting-part-06", CONTINUATION),
        authoring("conditional-formatting-part-07", CONTINUATION,
                  Replace("`powerbi-report-author validate` rejects", "`validate_dashboard` rejects"),
                  Replace("**text**. Warn the\n  user; offer to use an ImageUrl field or set the category on a column/measure\n  holding valid image URLs.",
                          "**text**. Say so in your report, and use an ImageUrl field or set the category on a column/measure\n  holding valid image URLs.")),
        authoring("formatting-overview",
                  Replace("verify rendered defaults in Desktop", "verify rendered defaults with `render_dashboard`"),
                  Replace("verify it through\nthe host-specific workflow in preview.md (see `preview.md`).", "verify it with\n`render_dashboard`.")),
        authoring("formatting", Replace("use `powerbi-report-author validate`", "use `validate_dashboard`")),
        authoring("formatting-part-02", CONTINUATION,
                  Replace("Ask the user to clarify — page canvas or visual plot area", "Pick by context, page canvas or visual plot area, and say which in your report")),
        authoring("expressions", Replace("Referenced from SKILL.md. Read this for expression", "Read this for expression")),
        authoring("color-strategy"),
        authoring("page-formatting"),
        authoring("theming",
                  Replace("Referenced from SKILL.md. Read formatting-overview.md", "Read formatting-overview.md"),
                  Replace("5. Reload Desktop — the new filename guarantees cache invalidation.", "5. The new filename guarantees cache invalidation in Desktop."),
                  Replace("Run `powerbi-report-author validate <path-to-.Report-dir>` after\n> registering or editing a theme to catch registration and theme JSON issues\n"
                          "> before following the host-specific workflow in preview.md (see `preview.md`).",
                          "Run `validate_dashboard` after\n> registering or editing a theme to catch registration and theme JSON issues\n> before `publish_dashboard`.")),
        authoring("theming-part-02", CONTINUATION,
                  Replace("see SKILL.md Anti-Patterns (see `../authoring-part-04.md`, section `anti-patterns-and-pitfalls`)", "see `pitfalls.md`"),
                  Replace("`powerbi-report-author validate` does not infer", "`validate_dashboard` does not infer")),
        authoring("re-theming", Replace("Referenced from SKILL.md and theming.md. Read this", "Read this")),
        authoring("re-theming-part-02", CONTINUATION,
                  Replace("Do NOT preview until this entire step is complete.", "Do NOT run `publish_dashboard` until this entire step is complete."),
                  Replace("Follow the standard edit -> validate -> preview -> screenshot review loop in\npreview.md (see `preview.md`).",
                          "Follow the standard edit, `validate_dashboard`, `publish_dashboard`, `render_dashboard` loop.")),
        authoring("re-theming-part-03", CONTINUATION),
        authoring("slicers", Replace("`powerbi-report-author validate` checks", "`validate_dashboard` checks")),
        authoring("slicers-part-02", CONTINUATION,
                  Replace("Delegate semantic-model discovery to the semantic-model authoring skill or\n"
                          "available modeling MCP server. Ask it to return a renderable Date/DateTime\n"
                          "column and the exact table/column identity needed by the report query. For a\n"
                          "fixed Manual preselection, also request the live minimum and maximum nonblank\n"
                          "dates; for a Relative preselection, provide the requested business period so\n"
                          "the model workflow can confirm that it intersects available data.\n\n"
                          "Use the verified field identity and date values returned by that capability in\n"
                          "the report templates below; semantic-model inspection and DAX-query procedures\n"
                          "remain owned by the semantic-model capability. If no model capability is\n"
                          "available, author the unselected Manual state or ask the user to provide the\n"
                          "date field and intended range.",
                          "Find a renderable Date/DateTime column and the exact table/column identity needed\n"
                          "by the report query, with `run_dax_query` (`INFO.VIEW.COLUMNS()`) or the model's\n"
                          "TMDL files. For a fixed Manual preselection, also query the live minimum and\n"
                          "maximum nonblank dates; for a Relative preselection, confirm that the requested\n"
                          "business period intersects available data.\n\n"
                          "Use the verified field identity and date values in the report templates below. If\n"
                          "you cannot find a suitable date field, author the unselected Manual state and say\n"
                          "so in your report."),
                  Replace("validate the target Desktop build by screenshot and use", "check the render with `render_dashboard` and use")),
        authoring("filters",
                  Replace("Referenced from SKILL.md. Read this when creating", "Read this when creating"),
                  Replace("`powerbi-report-author validate` flags", "`validate_dashboard` flags")),
        authoring("filter-pane"),
        authoring("field-parameters",
                  Replace("This skill owns the **report-side** of the pattern: wiring the slicer and\n"
                          "target visual in PBIR. The field-parameter table itself is a **semantic-model\n"
                          "object** — this skill never edits TMDL or `.SemanticModel` files directly.\n"
                          "Any model-side mutation (creating, editing, reordering, renaming, or deleting a\n"
                          "field parameter) is out of scope — delegate to the `semantic-model-authoring`\n"
                          "skill if available, otherwise try other model-authoring skills. If no\n"
                          "model-authoring capability is available, prompt the user. Reading TMDL directly\n"
                          "is acceptable only for **inspection** (recognizing an existing parameter), never\n"
                          "for mutating one.",
                          "This reference covers the **report-side** of the pattern: wiring the slicer and\n"
                          "target visual in PBIR. The field-parameter table itself is a **semantic-model**\n"
                          "object: create, edit, reorder, rename or delete one with the `semantic-model`\n"
                          "skill (its `field-parameters.md`), not here."),
                  Replace("4. If the parameter is missing (or must be edited/deleted), delegate the\n"
                          "   model-side change to the `semantic-model-authoring` skill if available;\n"
                          "   otherwise try other model-authoring skills. If no model-authoring capability\n"
                          "   is available, stop and prompt the user that the model-side changes cannot be\n"
                          "   completed, then confirm before continuing. Do not silently fall back to a\n"
                          "   worse (static) visual, and do not hand-edit TMDL.",
                          "4. If the parameter is missing (or must be edited/deleted), make the\n"
                          "   model-side change with the `semantic-model` skill. If the model change cannot\n"
                          "   be made, end with a blocking question instead of building a worse (static)\n"
                          "   visual."),
                  Replace("5. Build the slicer and target visual, validate PBIR, follow the host-specific\n"
                          "   workflow in preview.md (see `preview.md`), and review the rendered result.",
                          "5. Build the slicer and target visual, run `validate_dashboard`, `publish_dashboard`, and\n"
                          "   review the result with `render_dashboard`."),
                  Replace("4. Validate PBIR, follow the host-specific workflow in\n"
                          "   preview.md (see `preview.md`), and review the rendered result.",
                          "4. Run `validate_dashboard`, `publish_dashboard`, and review the result with\n"
                          "   `render_dashboard`."),
                  Replace("always\ncomplete the selected-host preview and screenshot review loop in\npreview.md (see `preview.md`).",
                          "always\nrun `publish_dashboard` and check the result with `render_dashboard`.")),
        authoring("powerbi-report-author-cli",
                  Replace("- [Rendering preview vs PBIR inspection commands](#rendering-preview-vs-pbir-inspection-commands)\n", ""),
                  Replace("- [Preview result handling](#preview-result-handling)\n  - [Screenshot options](#screenshot-options)\n", ""),
                  Replace("Use this file when the root command table is not enough. The CLI is the source", "The CLI is the source"),
                  Replace("Before using an unfamiliar non-preview command or before the preview host/operation is selected; never before an explicit Desktop operation |",
                          "Before using an unfamiliar command |"),
                  DropRange("| `pack <folder>", "| `preview-visuals <path>"),
                  DropRange("## Rendering preview vs PBIR inspection commands\n", "## Validation result handling\n"),
                  Replace("Run `powerbi-report-author validate <path-to-.Report-dir>` after every logical\nbatch of PBIR edits.\n\n- Fix every error before rendered preview.",
                          "`validate_dashboard` runs the CLI's `validate`. Read its result like this:\n\n- Fix every error before `publish_dashboard`."),
                  Replace("  the broken node.\n- For large diagnostics, use `--pretty` for readable output or `--out <file>` to\n  write the full result to a file.",
                          "  the broken node."),
                  DropFrom("\n## Preview result handling\n")),
        authoring("authoring-workflows",
                  DropRange("> **Mandatory completion gate:**", "> **Before editing existing reports:**"),
                  Replace("`powerbi-report-author validate` flags duplicates", "`validate_dashboard` flags duplicates"),
                  Replace("SKILL.md § PBIR File Layout (see `../authoring-part-02.md`, section `pbir-file-layout`)", "`pbir-layout.md`"),
                  Replace("and rerun the host-specific workflow in\npreview.md (see `preview.md`). Only change", "and run `publish_dashboard`. Only change"),
                  Replace("6. Validate and reload the report, select a source data point carrying the\n"
                          "   field, navigate to the target, and confirm the filter context and Back action.",
                          "6. Run `validate_dashboard`, `publish_dashboard`, and render the target page. `render_dashboard` cannot click\n"
                          "   a source visual, so check the drillthrough wiring in the files and say in your report that the\n"
                          "   interaction itself was not exercised.")),
        authoring("screenshot-review",
                  Replace("Use this file after mandatory screenshot capture through the host-specific\nworkflow in preview.md (see `preview.md`) and before reporting completion for every\n"
                          "rendered-output change. Capturing and reviewing every affected page is part of\nthe authoring completion gate, even when the user did not explicitly request a\nscreenshot.\n\n"
                          "Keep every validation capture and retry in the workflow-owned directory\nselected through preview.md (see `preview.md`). Delete that directory only after the\n"
                          "complete review loop finishes.",
                          "Use this file after you render a page with `render_dashboard` and before you finish a\nrendered-output change. Review every affected page."),
                  Replace("1. The screenshot image file paths.", "1. The rendered image."),
                  Replace("then repeat the PBIR validate -> preview -> screenshot review loop.", "then repeat the `validate_dashboard`, `publish_dashboard`, `render_dashboard` loop."),
                  DropRange("> **Screenshot scope:**", "## Checklist\n"),
                  Replace("- Does each measure follow `Design Brief.color_map` consistently across visuals?", "- Does each measure keep the same color across visuals?"),
                  Replace("| Same measure uses different colors | `Design Brief.color_map` was not applied consistently | Re-read the brief and set each measure-bound visual to the mapped color. |",
                          "| Same measure uses different colors | The color was not applied consistently | Set each measure-bound visual to the same color (see `color-strategy.md`). |")),
        Reference("skills/powerbi-report-cli/references/authoring-part-02.md", "pbir-layout.md", [
            DropRange("# powerbi-report-cli authoring mode -- Power BI Report Authoring (PBIR/PBIP) - Part 2", "## PBIR File Layout\n"),
            DropFrom("\n---\n"),
            Replace("the normal edit -> validate -> preview -> review loop.", "the normal edit, `validate_dashboard`, `publish_dashboard`, `render_dashboard` loop."),
            Replace("the binding must be repointed (see `model-binding.md` (see `authoring/model-binding.md`))\n  before the report opens in Desktop.",
                    "the binding must be repointed before the report opens in Desktop (the publish tool sets the model binding)."),
            Replace("## PBIR File Layout\n", "# PBIR File Layout\n"),
            Replace("└── <Report>.SemanticModel/                # OUT OF SCOPE", "└── <Report>.SemanticModel/                # the semantic-model skill"),
            Replace("Preserve schema/version unless intentionally migrating; when creating/repointing/repairing the binding, see `model-binding.md` (see `authoring/model-binding.md`)",
                    "Leave it alone; the publish tool sets the model binding"),
            Replace("Validate with `powerbi-report-author validate`.", "Validate with `validate_dashboard`."),
        ]),
        Reference("skills/powerbi-report-cli/references/authoring-part-04.md", "pitfalls.md", [
            Replace("# powerbi-report-cli authoring mode -- Power BI Report Authoring (PBIR/PBIP) - Part 4\n\n"
                    "Continuation of `authoring.md`. Open this file directly from the skill reference index.\n\n## Anti-Patterns and Pitfalls",
                    "# Anti-Patterns and Pitfalls"),
            Replace("`objects.general.filter`. Always read `references/authoring/slicers.md` first when modifying slicers |",
                    "`objects.general.filter` | Always read `references/authoring/slicers.md` first when modifying slicers |"),
            Replace("validate every bookmark and action target before Desktop reload", "validate every bookmark and action target before publishing"),
            Replace("Creating an image visual without prompting for the source type", "Creating an image visual without settling the source type"),
            Replace("Always ask the user for the image source (local file / URL / data field) before creating the visual",
                    "Decide the image source (local file / URL / data field) from the task before creating the visual, and say which in your report"),
            Replace("**Warn the user first** — the visual will render blank without `dataCategory: ImageUrl`. Present alternatives (other ImageUrl fields, local file, URL) and confirm before creating",
                    "**Do not create it as requested.** The visual will render blank without `dataCategory: ImageUrl`. Pick an alternative (other ImageUrl fields, local file, URL) and say which in your report"),
            Replace("debug the fields, try alternative geographic columns/coordinates, or ask the user — do **not** silently substitute a non-map visual without consulting the user first",
                    "debug the fields and try alternative geographic columns/coordinates — do **not** silently substitute a non-map visual; say what is wrong in your report"),
            Replace("**Warn the user before applying.** Use `ask_user` to present alternatives (filter negatives, switch measure, use `labelDisplayUnits`). "
                    "Apply `logAxisScale: true` only after the user resolves negative values or confirms all bound values are positive",
                    "**Do not apply it.** Choose an alternative (filter negatives, switch measure, use `labelDisplayUnits`) and say which in your report. "
                    "Apply `logAxisScale: true` only when every bound value is confirmed positive"),
            Replace("do not appear after preview reload", "do not appear after publishing"),
            Replace(", validate, then follow the host-specific reload workflow in preview.md (see `authoring/preview.md`)", ", `validate_dashboard`, then `publish_dashboard`"),
            Replace("old hex values before preview", "old hex values before publishing"),
            DropRange("| Using PowerShell `ConvertTo-Json`", "| Using regex or string replacement"),
            DropFrom("\n## Official Documentation\n"),
        ]),
        Reference("skills/powerbi-report-cli/references/design.md", "design.md", [
            DropRange("<!-- Mode reference for the `powerbi-report-cli` skill", "# powerbi-report-cli design mode -- Power BI Report Visual Design"),
            Replace("# powerbi-report-cli design mode -- Power BI Report Visual Design", "# Power BI Report Visual Design"),
            Replace("Use the Semantic Model MCP server if available, or read `.tmdl` files directly.", "Read the model's `.tmdl` files and query it with `run_dax_query`."),
            Replace("- Ask only for missing design inputs that materially affect the outcome.",
                    "- End with a blocking question only for missing design inputs that materially affect the outcome."),
            Replace("## Workflow\n\n### Step 0",
                    "## Workflow\n\n**A text-only or info page** (text, an image, navigation; no measures) takes a minimal brief: title, body, image and navigation. "
                    "Skip archetype routing, `color_map` and `space_audit`; take the canvas and theme from the report.\n\n### Step 0"),
            Replace("- Do not replace the planning workflow for broad create/build requests.\n", ""),
            Replace("If the user picks, proceed; if still unsure, ask one narrowing follow-up; after at most two rounds, pick the best option, "
                    "document the assumption in `variant_rationale`, and proceed.", ""),
            Replace("brownfield reports preserve the existing canvas unless resize is approved.",
                    "brownfield reports preserve the existing canvas unless resize is approved. A new page in an existing report is designed as greenfield "
                    "but takes the report's canvas size and theme; `brownfield.md` is for restyling existing pages."),
            Replace("**Vague prompts: ASK before routing.**", "**Vague prompts: stop and ask before routing.**"),
            Replace("page count, or filtering depth is missing", "page count, or desired filters/slicers are missing"),
            Replace("When vague, **stop and offer 2–3 concrete named options** in user-facing terms", "When vague, **stop and end with a blocking question** that offers 2–3 concrete named options in user-facing terms"),
            Replace("When this skill is used inside the planner workflow, embed this YAML block in\n`_brief/report-spec.md` under a \"Canonical design contract\" section. For the\nfull template,",
                    "Write this YAML block to `/tmp/design-brief.md` (outside the repo, so it never enters the patch) and re-read it before each page. For the\nfull template,"),
            AUTHORING_MODE,
        ]),
        design("accessibility"),
        design("anti-patterns", AUTHORING_MODE, FILTERS,
               Replace("Strong warnings surface to the user for explicit confirmation.",
                       "Strong warnings are fixed unless the plan asks for them, and are listed in your report."),
               Replace("Likely unintentional; surface to user", "Likely unintentional; report it"),
               Replace("Recommend explicit user confirmation before proceeding", "Fix it unless the plan asks for it, and say so in your report"),
               Replace("The agent surfaces findings and recommendations; the user decides.", "The agent reports its findings and recommendations; the plan decides."),
               Replace("Stop and offer 2–3 named, concrete options (each referencing a specific archetype + variant combination, in user-facing language). "
                       "Only after the user picks, or after two clarification rounds with a documented assumption, proceed to design",
                       "Stop and end with a blocking question offering 2–3 named, concrete options (each referencing a specific archetype + variant combination, in user-facing language). "
                       "Proceed to design only once the plan settles it")),
        design("archetype-composition", Replace("lives in `SKILL.md` Step 2", "lives in `design.md` Step 2")),
        design("brownfield",
               Replace("confirm the existing PBIP path with the user.", "locate the existing PBIP path."),
               Replace("   - authoring anti-patterns\n", "   - `pitfalls.md`\n"),
               Replace("in `SKILL.md` still applies", "in `design.md` still applies"),
               Replace("new), **ask** before proceeding.", "new), end with a blocking question instead of proceeding."),
               Replace("2. **Screenshot all pages** — first open the report in Desktop with\n"
                       "   `powerbi-report-author preview <path> --host desktop` (only a bare preview can\n"
                       "   launch Desktop; run\n"
                       "   `powerbi-report-author preview <path> --host desktop --status` first, because\n"
                       "   a bare preview re-reads PBIR into a window that already holds the report and\n"
                       "   discards its unsaved edits), then capture with\n"
                       "   `powerbi-report-author preview <path> --host desktop --screenshot <dir> --all-pages`\n"
                       "   — these become the \"before\" evidence.",
                       "2. **Render all pages** — with `render_dashboard`. These become the \"before\" evidence."),
               Replace("`powerbi-report-author validate` after each batch", "`validate_dashboard` after each batch"),
               Replace("layout regressions before reload", "layout regressions before publishing"),
               Replace("**Final Audit** — screenshot AFTER.", "**Final Audit** — render again AFTER."),
               Replace("`powerbi-report-author validate`\n      catches", "`validate_dashboard`\n      catches"),
               AUTHORING_MODE),
        design("chart-selection"),
        design("color", AUTHORING_MODE),
        design("design-brief",
               Replace("validation,\nDesktop reloads, and screenshots.", "validation,\npublishing, and renders."), DESIGN_MODE, AUTHORING_MODE,
               Replace("In the planner workflow, embed this YAML block inside `_brief/report-spec.md`\nunder a \"Canonical design contract\" section. The surrounding Markdown is for\n"
                       "user approval and context; the embedded YAML is authoritative for\nimplementation.",
                       "Write this block to `/tmp/design-brief.md`, outside the repo so it never enters the patch, and re-read it before each page: "
                       "your context can be summarized during a long task, and a file cannot.")),
        design("design-brief-part-02", CONTINUATION, AUTHORING_MODE),
        design("interactivity", AUTHORING_MODE),
        design("pre-flight-checklist", AUTHORING_MODE, FILTERS,
               DropRange("- [ ] **Single source of truth**", "- [ ] **Tone committed**"),
               Replace("brief shows that 2–3 named options were offered to the user and\n      one was confirmed (or, after two rounds, an assumption was\n      documented in `variant_rationale`). Do NOT",
                       "brief shows that the plan settled it, or the task ended with a blocking\n      question offering 2–3 named options. Do NOT")),
        design("signatures"),
        design("tone-catalog", FILTERS,
               Replace("Surface the recommendation to the user or record it as an assumption in the\nbrief", "Record the recommendation as an assumption in the\nbrief"),
Replace("follow the root skill's\nclarification workflow first", "follow the vague-prompt rule in `design.md`\nfirst")),
        design("typography", AUTHORING_MODE),
        design("visual-cookbook",
               Replace("Follow the Card Sizing formula in the\n  the `authoring` mode — it\n  scales", "Follow the Card Sizing formula in `card-part-02.md`: it\n  scales"),
               AUTHORING_MODE),
        design("visual-cookbook-part-02", CONTINUATION, AUTHORING_MODE),
        design("analytical-canvas", folder="design/archetypes"),
        design("comparative-benchmark", AUTHORING_MODE, folder="design/archetypes"),
        design("comparative-benchmark-part-02", CONTINUATION, folder="design/archetypes"),
        design("executive-summary", folder="design/archetypes"),
        design("narrative-story", folder="design/archetypes"),
        design("operational-monitor", folder="design/archetypes"),
        Reference("skills/powerbi-report-cli/references/design/assets/base.json", "assets/base.json"),
    ],
}


class BuildError(Exception):
    pass


def apply_cuts(reference: Reference, text: str, source_root: pathlib.Path) -> str:
    for cut in reference.cuts:
        if isinstance(cut, Replace):
            found = text.count(cut.old)
            if found != cut.count and not (cut.count == 0 and found):
                raise BuildError(f"{reference.source}: expected {cut.count} match(es) of {cut.old[:70]!r}, found {found}")
            text = text.replace(cut.old, cut.new)
        elif isinstance(cut, DropRange):
            for marker in (cut.start, cut.end):
                if text.count(marker) != 1:
                    raise BuildError(f"{reference.source}: marker {marker[:70]!r} not found exactly once")
            if text.index(cut.start) > text.index(cut.end):
                raise BuildError(f"{reference.source}: {cut.start[:40]!r} comes after {cut.end[:40]!r}")
            text = text[:text.index(cut.start)] + text[text.index(cut.end):]
        elif isinstance(cut, DropLine):
            lines = text.split("\n")
            matches = [line for line in lines if line.startswith(cut.prefix)]
            if len(matches) != 1:
                raise BuildError(f"{reference.source}: expected one line starting {cut.prefix[:60]!r}, found {len(matches)}")
            text = "\n".join(line for line in lines if not line.startswith(cut.prefix))
        elif isinstance(cut, Append):
            text = text.rstrip("\n") + "\n\n" + cut.text.strip("\n") + "\n"
        elif isinstance(cut, AppendSlice):
            other = (source_root / cut.source).read_bytes().decode("utf-8").replace("\r\n", "\n")
            if other.count(cut.start) != 1 or (cut.end is not None and other.count(cut.end) != 1):
                raise BuildError(f"{cut.source}: slice markers {cut.start[:50]!r} / {str(cut.end)[:50]!r} not found exactly once")
            piece = other[other.index(cut.start):other.index(cut.end) if cut.end else len(other)]
            piece = apply_cuts(Reference(cut.source, "", cut.cuts), piece, source_root)
            text = text.rstrip("\n") + "\n\n" + piece.strip("\n") + "\n"
        else:
            if text.count(cut.marker) != 1:
                raise BuildError(f"{reference.source}: marker {cut.marker[:70]!r} not found exactly once")
            text = text[:text.index(cut.marker)].rstrip("\n") + "\n"
    for old, new in (("references/authoring/", "references/"), ("references/design/archetypes/", "references/"), ("references/design/", "references/"),
                     ("`design/archetypes/", "`"), ("`design/", "`"), ("`archetypes/", "`"), ("`authoring/", "`"), ("`../", "`")):
        text = text.replace(old, new)
    # Microsoft's generator adds "(see `x.md`)" after a bare "x.md"; the repeat carries nothing
    text = re.sub(r"(`?)([\w-]+\.md)\1 \(see `\2`\)", r"\1\2\1", text)
    leftovers = [(line_number, term) for line_number, line in enumerate(text.split("\n"), 1)
                 for term in FORBIDDEN if term in line]
    if leftovers:
        raise BuildError(f"{reference.source}: forbidden phrases left: {leftovers[:5]}")
    return text


def build_reference(source_root: pathlib.Path, reference: Reference, target: pathlib.Path) -> str:
    raw = (source_root / reference.source).read_bytes()
    crlf = raw.count(b"\r\n") == raw.count(b"\n")
    text = apply_cuts(reference, raw.decode("utf-8").replace("\r\n", "\n"), source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return "verbatim" if not reference.cuts else f"{len(reference.cuts)} cut(s)"


def build_skill(name: str, source_root: pathlib.Path, license_file: pathlib.Path) -> None:
    skill_dir = SKILLS_DIR / name
    references = skill_dir / "references"
    references.mkdir(parents=True, exist_ok=True)
    wanted = {reference.dest for reference in SKILLS[name] if not reference.root}
    for stale in references.rglob("*"):
        if stale.is_file() and stale.relative_to(references).as_posix() not in wanted:
            stale.unlink()
    print(f"{name}:")
    for reference in SKILLS[name]:
        outcome = build_reference(source_root, reference, (skill_dir if reference.root else references) / reference.dest)
        print(f"  {reference.dest:28s} {outcome}")
    (skill_dir / "LICENSE").write_bytes(license_file.read_bytes())


def check_frontmatter(skill_dir: pathlib.Path) -> None:
    """Fails if a skill's SKILL.md frontmatter would not load: the skills library silently skips a skill whose frontmatter does not
    parse, and an unquoted value containing ': ' or ' #' is read by YAML as a new key or a comment."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    if not match:
        raise BuildError(f"{skill_dir.name}/SKILL.md: no frontmatter between two '---' lines.")
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if fields.get("name") != skill_dir.name:
        raise BuildError(f"{skill_dir.name}/SKILL.md: name is {fields.get('name')!r}; it must equal the folder name.")
    description = fields.get("description", "")
    if not description or description[0] in "|>":
        raise BuildError(f"{skill_dir.name}/SKILL.md: write the description on one line.")
    if len(description) > 1024:
        raise BuildError(f"{skill_dir.name}/SKILL.md: description is {len(description)} characters; the limit is 1024.")
    if description[0] not in "'\"" and (": " in description or " #" in description):
        raise BuildError(f"{skill_dir.name}/SKILL.md: the description contains ': ' or ' #', which YAML does not read as text. "
                         "Reword it or put it in quotes.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=pathlib.Path, required=True, help="the plugin's root folder")
    parser.add_argument("--license", type=pathlib.Path, required=True, help="the plugin repository's LICENSE file")
    parser.add_argument("--skill", choices=sorted(SKILLS), help="build one skill instead of all")
    args = parser.parse_args()
    for name in ([args.skill] if args.skill else sorted(SKILLS)):
        build_skill(name, args.source, args.license)
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if (skill_dir / "SKILL.md").exists():
            check_frontmatter(skill_dir)
    print("frontmatter ok")


if __name__ == "__main__":
    main()
