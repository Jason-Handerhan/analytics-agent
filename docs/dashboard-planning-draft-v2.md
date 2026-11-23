# Dashboard planning: draft v2 (2026-11-23)

**Status: draft, nothing built.** The earlier nested-schema draft is `docs/dashboard-planning-draft.md` (revision 3, reconstructed). This version replaces its schema with a small flat envelope plus a free-form description, and moves the field-by-field guidance out of the tool schema into a guide the tool returns on request. Fold into `docs/coding-agent.md` and `docs/dashboard-editing.md` once approved, then delete both drafts.

**Decided so far**
- One tool, `save_coding_plan`, with **flat** arguments (not nested, not a discriminated union). `subagent_type` (`code` or `dashboard`) selects the guide, tool set, system prompt, preamble, rubric and path guard. A union is the move once a second type needs two or more fields of its own.
- A plan is an **envelope** the code can validate plus a **free-form `description`** the main agent writes with the user, following the sections the type's guide lists. The agent shapes the description to the request (add, edit, delete, model-only, restyle).
- **The guidance is the same guidance we have now**, in a guide the tool returns when called with only `subagent_type`. It is not in the tool schema or the system prompt, so conversations that never plan pay nothing for it.
- **The coding agent works out the model.** The user specifies what the pages do; the description names model needs only when the user was specific. A plan may change only the model.
- Light validation only (2b). No parsing of headings, no card or slicer caps, no check of page names against the diff.
- A new dashboard follows Microsoft's layout rules (`layout.md`); a change to the existing dashboard matches how its existing pages are arranged.
- Every task gets all the skills. The type selects the tool set and the system prompt.
- A multi-page plan is saved by resending the whole description (at most 20,000 characters, a guard against something outrageous). There is no partial-edit argument.
- A dashboard rubric and the reviewer's final-render tool are part of this plan (section 4e).

## 1. Why the nested schema did not fit

Revision 3 gave each page `cards`, `charts`, `slicers`, `field_parameters`, `bookmarks`, `buttons` and `layout`. It described a new page well and little else:

- **Edits and deletes had no shape.** `change` was `edit` or `add`. To edit one chart the agent put the change in `charts`, a list meaning "what the page shows". Deleting a page or chart had no representation. `slicers` was required, so an edit that did not touch slicers wrote `["None"]`, which says "no slicers".
- **The lists meant two things:** the final state for a new page, a delta for an edit.
- **Weight.** The code plan's schema is about 1.85k tokens now; the dashboard fields and guidance would add roughly 1.5 to 2k, carried on every trip of every turn (about 10 trips a turn, at the cached rate), including the many turns that never plan.

## 2. The call

### 2a. Arguments

```python
class SaveCodingPlanArgs(FlatToolArgs):
    subagent_type: Literal["code", "dashboard"] = Field(description=(
        "'code' changes the ML repo's code (SQLX, Python, notebooks). 'dashboard' changes the Power BI dashboard or its semantic model. "
        "Send only this, with nothing else, to get the guide for that type before writing a plan."))
    name: str | None = Field(None, max_length=100, description=(
        "A short title the user will recognize: what it changes and where, such as 'LGBM ranker' or 'Aisle pacing ratio for the ranker'."))
    goal: str | None = Field(None, description=(
        "What this change achieves and why, in one or two sentences, stated as the result and not the work. No files, steps or limits "
        "(those go in the description), and no numbers."))
    scope: str | None = Field(None, max_length=500, description=(
        "What is changing and where, in a few sentences (for example 'Add a Cost page with cost cards and a cost-by-model chart; "
        "no changes to other pages')."))
    base_branch: str | None = Field(None, description=(
        "`dev` for a new task: the coding agent starts from it and pushes a new branch. To make a revision of an earlier task, set "
        "this to the `branch` in that task's result, copied exactly: the coding agent starts from that branch and adds its commit to "
        "it. Make a revision when the new work needs, fixes or extends that task's changes; independent work stays on `dev`. Ask the "
        "user if it is unclear which."))
    review: bool | None = Field(None, description=(
        "Whether a reviewer checks the finished work against the criteria in the guide and sends the coding agent back to fix what "
        "fails. It increases token cost and run time, more for a larger task. Set it yourself: on for larger or riskier changes, off "
        "for small ones. When you show the plan, say whether it is on and why, and what it does. The user approving the plan "
        "approves this setting for this plan only: keep it through every version of the plan unless they change it, and don't raise "
        "it again. A different build is a new plan, so choose and state it again. If the user says they want it on or off for all "
        "plans going forward, record that with manage_notes, then use it for every plan without recommending, until they say "
        "otherwise."))
    new_pages: int | None = Field(None, ge=0, le=3, description=(
        "Dashboard plans only: how many new pages this plan adds (0 if none), at most 3."))
    audience: str | None = Field(None, description=(
        "Dashboard plans only: who uses the dashboard and how. Needed when the plan adds a new page or a new dashboard."))
    description: str | None = Field(None, max_length=20000, description=(
        "The whole plan, in markdown, with the sections the guide lists. Saves it, replacing the draft."))
    discard: bool = Field(False, description="Set to true instead of a plan to delete the saved draft.")
```

| Sent | Does |
|---|---|
| `subagent_type` alone | Returns the guide for that type. Saves nothing. |
| `description`, with `name`, `goal`, `scope`, `base_branch`, `review` (and for a dashboard `new_pages`) | Saves a new version of the whole plan. |
| `discard=true` | Deletes the draft. |

The saved draft is attached to the main agent's prompt every turn until the task starts, so a change to one page is made by resending the description with the other pages copied unchanged. The plan window shows the whole plan, and the agent shows the user only what changed.

### 2b. Validation (the whole list)

- Exactly one of the three actions. A save with a missing envelope field fails naming the missing fields.
- `goal` and `scope` are not empty; `scope` is at most 500 characters; `description` is at most 20,000. The description cap only stops something outrageous (a three-page plan is a few thousand characters); the draft stays in the prompt until the task starts, so it is also the ceiling on that cost.
- `new_pages` and `audience` are accepted only when `subagent_type` is `dashboard`, and a dashboard plan must send `new_pages` (0 if none), so the agent has to count. More than 3 fails with "A plan adds at most 3 new pages. Plan the others after this one finishes."
- `base_branch` is checked as today.

Not validated, on purpose: headings, which pages the plan names, card or slicer counts, whether the description matches `new_pages`. The user reads the plan before approving, the coding agent stops with a blocking question if it cannot build the plan, and the diff and the reviewer see what changed.

### 2c. The stored plan

```python
class CodingAgentPlan(BaseModel):
    subagent_type: Literal["code", "dashboard"]
    name: str
    goal: str
    scope: str
    base_branch: str
    review: bool
    new_pages: int | None = None
    audience: str | None = None
    description: str
```

The checks in 2b are its validators, so `start_coding_task` re-validates the stored dict as it does today. Callers read the type inline (`plan.subagent_type == "dashboard"`); no property.

### 2d. What the save does (unchanged from today)

`save_coding_plan` already does this for a code plan (`coding_tools.py` 114-131): it checks the base branch (a new task from `dev`, or a revision of an existing branch), reads the saved draft, adds one to its version, keeps `first_saved_at`, writes the plan with `saved_at`, and replies with the task type line and the instruction to show the plan to the user before starting. The save keeps all of that. Only the fields it reads change: the flat ones above, with `name` and `review` read from the arguments.

## 3. The main agent's guidance

The arguments describe the envelope fields in full (name, goal, scope, base branch, review, new pages, audience). The guide per type covers only what goes in `description`, carried over from the nested schema's field descriptions and the old tool description. The main agent reads it before writing a plan.

### 3a. How it is delivered

One markdown file per type, read at startup like `context/page_info/` and returned verbatim by the tool (`context/coding_plan_guides/code.md`, `dashboard.md`). The tool description and the system prompt carry only a pointer. (A new subfolder of `context/`: to confirm.)

### 3b. `save_coding_plan` description

> Saves a draft of a coding task's plan, to build and change with the user, and start with start_coding_task once the user has approved it. Call it when the user wants a change made to the ML repo's code or to the dashboard. Before writing a plan, call it with only subagent_type ('code' or 'dashboard') to get the guide for that type: it lists the sections to write and what each needs. A plan is for one type, never both: when a request needs both, plan one now and the other after it finishes. A dashboard that needs data that is not built yet, or that should be aggregated first, needs the code plan first: a SQLX change for data before the ML model (anything built by the SQLX), or a change to the notebook that builds the results table for data after it (model results, SHAP values and the like). Read the SQLX or the notebook in the repo to see the schemas (you cannot query those datasets). If the code does not define the table, it has not been added; if it does, ask the user to confirm they have run it. Once they have, plan the dashboard change, which brings the table into the model. Settle the plan by working out what you can yourself and asking the user for the rest. Every save replaces the draft and counts as a new version, so send the whole plan each time. Save a plan before showing it to the user, and save each change before describing it to the user. Set discard to true instead of a plan to delete the draft. Call it only once per round, and never in the same round as start_coding_task or open_pull_request.

### 3c. System prompt

`CODE CHANGES` gains the sentences from revision 3: a plan changes either the ML repo's code or the dashboard, never both; plan one at a time; if a dashboard change needs data that is not built yet, the code plan comes first (SQLX before the ML model, the notebook that builds the results table after it), and the user runs it before the dashboard plan; and, new: plan the pages (what each is for, its cards, charts and slicers), not the model, because the coding agent works out the measures and columns they need. `LARGE REQUESTS` gains one sentence: a dashboard plan with more than one page is a large request: settle the shared things first, then plan one page at a time.

### 3d. The code guide: what goes in `description`

The sections of a code plan's description. The envelope's own fields (name, goal, scope, base branch, review) are described in the arguments and not repeated here.

- **Requirements.** The deliverables, one item per thing to build or change, each naming the table, file or model it applies to. Split anything joined by 'and' that touches more than one place. For example: 'Add user_aisle_pacing_ratio to prelim_user_aisle_features', 'Forward-fill the new columns in skeleton_features', 'Expose user_aisle_pacing_ratio in final_ml_features_table and add it to rowConditions', 'Pass the feature to the LGBM ranker only', 'Create a notebook comparing the ranker with and without the feature, including gain importance'. How to tell it works is not a requirement (put the test behaviors in Tests), and what not to do goes in Constraints.
- **Tests** (omit when the change needs no new test). Only what the compile, the SQL dry runs and linting cannot check: the behaviors the new pytest tests must pin down, each as one sentence (for example 'select_features returns the feature only for the ranker'). Ask for the fewest tests that would fail if the behavior broke; no tests of wiring, training, real data or SQL.
- **Constraints.** Rules the change must follow: paths it may or may not touch, naming conventions, models or tables that must stay as they are, a limit on scope.
- **Files to imitate.** Existing repo files whose style or structure the new code should follow, each with what to follow in it, specifically: its column documentation block, how it forward-fills, its test style.
- **Decisions.** Choices the user made that settle something the agent would otherwise guess (which of two approaches, a name, a threshold, what is out of scope), each as an instruction with the user's reason if they gave one. If the user dictated specific logic or SQL, write it here in full. Do not list your own suggestions here.
- **Suggested code.** Code you suggest as a starting point, each with its purpose, the file it would live in if it belongs to one, and the code. The coding agent treats it as unverified, and the repo wins on any conflict. Do not paste code that already exists in the repo; name the file in Files to imitate instead.

**What the reviewer checks** (the one part of the guide that is not about the description: the criteria cannot live in the `review` argument without the schema carrying both rubrics, and you tell the user what is checked when you show the plan): `CODING_RUBRIC`, copied into the guide.

### 3e. The dashboard guide: what goes in `description`

A dashboard plan is about the pages: what each is for and how it looks. The coding agent works out the model (it finds or builds the measures and columns the pages need), the files to copy and the code. Write the information below in whatever structure fits the request; it is a list of what the plan should hold, not a template.

**When they apply, for any dashboard plan**
- **Constraints.** What must not change ('do not touch page X'). **Decisions.** Choices the user settled, each as an instruction with their reason if they gave one. If the user says the published sandbox dashboard is out of step with the files and asks to publish over it, record that here.
- **Suggested code.** Only code the user supplied, or when they asked to work through it (for example a DAX measure tested with the DAX tool). Do not draft code yourself unprompted.
- **Style, an existing dashboard.** Match its colors, fonts and card style unless the user asked for something different. If they want a specific card, chart or slicer styled differently, say which and how.
- **Style, a new dashboard.** The user describes it. Save the plan with what you know, then ask for what is missing in one question, and write down what they say: the company or brand it should follow (its colors, fonts, a logo if they have one); the main colors, as names or hex codes (for example 'purple and orange' for a FedEx dashboard); light or dark; the look, offering these in plain words (they are the looks the coding agent can build): editorial newsroom (reads like a business broadsheet: type-led, one sharp accent, generous whitespace); dark instrument panel (dark canvas, high contrast, color only for status); calm and clinical (cool near-white, teal and sage, generous spacing); Financial Times style (pink-tinted broadsheet, tabular numerals, editorial discipline for financial data); Bauhaus (geometric, modernist, primary colors used sparingly); monospace terminal (developer or quant feel, monospaced numbers, ticker-style KPI bars); dense analyst workbench (off-white, dense data, charcoal text, two accent colors); playful and energetic (warm, bright, coral accents, soft shapes); minimal (white, black type, one accent, no clutter); corporate cool (cool grey, slate type, one tech accent: the usual enterprise look); scholarly calm (stone surface, forest green and soft red, serif headings, like an academic paper); or the company's own brand. They can mix looks. Ask for anything to avoid. Colors that carry meaning (red for bad, green for good) are chosen separately from the brand colors. The coding agent turns this into a theme and keeps the text readable (contrast, color-blind safe): if a requested color would hurt that, it keeps the color, changes how it is used, and says so in its report. If the user wants a specific card, chart or slicer styled a particular way, say which and how.

**A new page or dashboard: for each page**
- **Name** and **what it answers**: what the page lets its audience see or decide, in one or two sentences and the user's words (for example 'which model to ship, and where the champion is weakest').
- **Cards.** Headline numbers. One item each: the number, based on what the user asked for but made clearer (for example 'Champion F1'), and a label if it should differ. Use an existing measure's name if you know one that fits; the coding agent finds or builds the measure. Five is the default maximum (about 250 to 300 pixels each, so they fit in one row). If the user wants more, follow up: more than five is outside good design practice, but there may be a good reason, so ask what they want, and write down what they decide.
- **Charts.** Charts and tables, each with what it shows (a measure by a field, for example 'Champion F1 by model') and the chart type (for example clustered bar, line, table). List them in priority order, most important first, and a detail table last.
- **Slicers.** One item each: the field and the slicer type (for example dropdown, list, date range), and 'synced with <page>' if it should share state. List them broadest first (for example the data split, then the model, then the metric). Write 'None' if the user wants no slicers: never leave this out. Up to 3 sit in a row with the title; 4 are the default maximum, in a filter rail down the side. If the user wants more than 4, follow up the same way as for cards, and write down what they decide.
- **Field parameters, bookmarks and buttons.** What they are, so you can recognize them in a user's wish: a *field parameter* lets the viewer switch which measure or which field a visual shows, through a slicer ('let me switch the chart between recall and F1'); a *bookmark* is a saved state of a page (which filters are set, which visuals show) that a button or the viewer can apply ('a view with only the top models', or switching one chart for another in the same spot); a *button* goes to a page, applies a bookmark, clears the filters or opens a link. Include them where they belong in the plan: they are often tied to a chart or a slicer. For each, say what it does, what it is tied to, where it sits, and what is shown when the page first opens.
- **Layout.** Say where things go in words, not only in an order. For example (an illustration of the style, not a layout to copy): 'title across the top, slicers in a row beneath it, four cards beneath the slicers, then the champion chart on the left and the detail table on the right'. If you list items in an order, say what it means in position ('priority order: left to right, then down').
  - *The default layout*, which applies unless the user describes something different. Label it as the default in the plan.
    - **The existing dashboard** (a new page): follow how its existing pages are arranged. If the existing pages are arranged inconsistently, ask the user which page to base it on, or whether they want something different, in which case use the new-dashboard default below.
    - **A new dashboard** (the new-dashboard default): the title at the top, up to 3 slicers at the right end of the title row (4 or more in a filter rail down the side), the cards in one row beneath, then the charts in priority order reading left to right and down, the detail table last.
  - *The user's layout.* If they describe a different layout, or where something goes, use theirs and write it in.
  - After you save, show the user the layout and ask whether they want anything different.

**Editing an existing page.** The page name, exactly as in the dashboard. What changes, exactly (which visual, which property, what it becomes). What stays, when the user wants the rest left alone. Anything it affects: a bookmark, a button, a slicer sync, a navigation link. Do not rearrange an existing page unless the user asked. Say nothing about slicers or cards the edit does not touch.

**Deleting a page or visual.** What goes, by page name (and visual, if only one visual). What points at it and should go with it or be retargeted: buttons, bookmarks, navigation, slicer sync, page order.

**A request that changes only the model.** Say what the user wants changed, in their words: what a new measure or column computes or holds, or which BigQuery table to bring in, with any definition or name they gave. The coding agent works out the rest. Set `new_pages` to 0.

**Large requests.** A plan adds at most 3 new pages; more is several plans, one after another, so say so and plan the first part now. With more than one page, settle what is shared first (goal, audience, style, slicers that appear on every page), save a task list with `manage_tasks`, then add one page per turn: resend the whole description with the new page added and the others copied unchanged, and show the user only the new page. The user approves once, when the last page is in.

**What the reviewer checks:** `DASHBOARD_RUBRIC` (section 4e), copied into the guide, for the same reason as the code guide's.

## 4. The coding agent

### 4a. A system prompt per type

`SANDBOX_SYSTEM_PROMPT` stays as the code prompt and a new `DASHBOARD_SYSTEM_PROMPT` is added; `build_coding_agent` picks one by `subagent_type`. The dashboard prompt carries only what the skills do not say. `planning.md` already holds the vagueness checks, the inventory, the check that every named page, measure and field exists or can be built, the blocking-question rule and the todo list; `authoring.md` holds the validate, publish and render loop, the last-publish rule, "copy the neighbors", and the report; `layout.md` the geometry; `semantic-model` the rule to test a measure with `DEFINE MEASURE`. What is left:

```
You work on the Power BI dashboard in {WORK_DIR}/dashboard/pbip: the report (the folder ending in .Report) and the semantic model
(the folder ending in .SemanticModel). Change only files under dashboard/. Do not run git commit or git push. Your skills are
/skills/powerbi-report for the report and /skills/semantic-model for the model: use the one the plan needs, or both (a plan may
change only the model). /skills/project-background explains the project's own terms (champion model, Recall@5): read it when a plan
uses a term you do not know. Put scratch files in /tmp, never in the repo, and keep there any note you will need later in a long task:
your context can be summarized, files cannot. Re-read them before the step that needs them.

To see what a table the plan needs holds, and whether it exists, read the code that builds it: the SQLX under `definitions/`
(`sources_*.sqlx`, `staging/`, `silver/`, `gold/`) for data before the ML model, and for data after it (model results, SHAP values and
the like) `Notebooks/evaluation/model_evaluation.ipynb`, which writes the results tables (the dataset is `results_dataset` in
`config/config.py`).
A table the code does not define has not been added.

If the plan needs data that is not in BigQuery yet, or an error or missing capability keeps you from meeting its core requirements,
stop with blocking_questions as soon as you notice. Do not ship a weaker substitute that quietly drops a requirement.
```

The two sentences of the second paragraph are the only rules not already in a skill, and the file-level duplication in revision 3's four sections (BEFORE YOU BUILD, LAYOUT, HOW YOU WORK, CHECKING YOUR WORK) is gone. Because the guide describes the plan in prose, the old stop rule's "has pages and does not say who it is for" is `planning.md` step 1 alone.

### 4b. `TASK_PREAMBLE` for a dashboard task

> The task below is a plan: its goal, its scope, and a description written by another agent with the user. Build what it describes, make the model changes it names and the ones its pages need, and follow the choices the user already made. Anything marked as suggested code was drafted by another agent and tested only if it says so; the model and the dashboard are the source of truth, so if a snippet conflicts with them or with a choice the user made, follow those and say so in your report.

### 4c. Final answer

The same structured answer as today. The `report` field description gains, for a dashboard task: per page, what changed and what is on it; the model changes made, with what each new measure calculates; which pages and states you rendered; and what you could not check (a drillthrough, for example).

### 4d. Checks the code makes

These run in `PatchCheckMiddleware.aafter_agent` and the final checks, not in the reviewer. The middleware reads `state["messages"]` (every tool call and result, with their ids) and the sandbox, which is all these checks need; `compact_transcript` is only the reviewer's input and is not involved.

| Check | How |
|---|---|
| **Path guard.** A dashboard task's patch may change only files under `dashboard/`; a code task's may not touch `dashboard/`. | `validate_patch(patch_bytes, subagent_type)`. A violation fails the task, like today's blocked paths. |
| **The last publish is the final state, and every changed page was rendered after it.** | `publish_dashboard` returns a content hash of the files it sent (the same function that reads the report and model folders out of the sandbox). The middleware finds the last successful publish among the `ToolMessage`s, hashes the sandbox's dashboard files the same way, and compares. A difference is a fixable violation: "You changed the dashboard after your last publish: publish it, then render the changed pages." It also finds the `render_dashboard` results after that publish and requires one for each changed page. Changed pages come from the patch (the page folders under `.Report/definition/pages/`), and the render result must name the page folder it rendered, so the match is by folder, never by display name. Both are requirements on the tools when they are built. |

The final `validate_dashboard` check of revision 3 is dropped. `publish_dashboard` runs the same validation as `validate_dashboard` and refuses on any error (`docs/dashboard-editing.md`, step 5), and the check above guarantees the last publish is the final state of the files, so a passing last publish already means the final files validated. The service checks the model's TMDL at the same publish, which a local check could not do. For a dashboard the clean-sandbox step only confirms the patch applies. A separate local schema check would repeat the publish check on the same bytes.

**What stops an agent from finishing without a valid, published dashboard.** Both checks are violations in `PatchCheckMiddleware`, which sends the agent back (twice). If a violation is still there after the retries, `execute_coding_run` does not apply or push the patch (`orchestration.py` 207-213: "only a patch that passed validation goes on"), and the task ends with the problems reported. A refused publish (validation error) or a failed one is not a "successful publish", so the agent can never match the final hash without a publish that passed validation. A separate run of `validate_dashboard` by our code would repeat what that publish already did on the same bytes, so it is skipped. Add a test that a refused or failed publish never counts as the last successful one.

The app does not publish the final version itself: publishing is how the agent sees the result, and a final publish it never rendered would ship unseen. Hashes catch an edit after the last publish without relying on timestamps; both normalize line endings the same way. No check compares the pages a diff changed with the pages the plan names: names are free text, the agent may choose a better page name, and a failure would waste a whole task. The diff, the reviewer and the PR review cover that.

The tool set follows the type: only a dashboard task is given `publish_dashboard`, `render_dashboard`, `validate_dashboard` and `run_dax_query`. A future type (a read-only researcher on a smaller model) adds its own tool set, prompt, rubric and model the same way.

### 4e. The reviewer

Today the reviewer is a `RubricMiddleware` grader (`coding_agent.py` 267): an agent with `show_changes` and the read-only sandbox tools, three iterations, reading `compact_transcript` (the task, the latest todo list, the dry runs, the report). It runs after the patch check. For a dashboard task:

- **A rubric by type.** `DASHBOARD_RUBRIC`, selected where `CODING_RUBRIC` is now (`orchestration.py` 181), and listed in the dashboard guide so the main agent can tell the user what is checked. Criteria, from revision 3's notes: everything the plan describes for each page is present with every card, chart, slicer, bookmark and button it names; every field a visual uses exists in the model or is added by the diff; new model objects have a `///` description and a `formatString`; the report says what each new measure calculates; no bookmark, interaction or slicer sync points at something the diff removed; the diff changes nothing under `dashboard/` the plan did not ask for; the report matches the diff. The path, last-publish and render checks of 4d are code, not criteria.
- **A tool for the final renders.** `show_renders` returns the image of each changed page's last render, so the reviewer judges the picture against the plan. The last-publish check guarantees those are the renders of the final state. It is a tool, not part of `compact_transcript`, so the reviewer fetches images only when it needs them. `render_dashboard` saves each render under `/tmp/renders/` in the sandbox (outside the repo) named by page folder, and `show_renders` reads them. (How render images are stored for the PR and the user is the open question in `docs/dashboard-editing.md` step 10; this does not decide it.)
- **A grader prompt per type.** `GRADER_SYSTEM_PROMPT_WITH_TOOLS` becomes one prompt per `subagent_type`, chosen where it is passed to `RubricMiddleware`. The dashboard one names `show_renders`, says the plan's description is prose to read against the diff and the renders, and carries the dashboard criteria. A new type gets its own prompt, rubric and tools the same way, even though only two exist. `compact_transcript` is unchanged.

## 5. Later (notes, not for now)

- **The refresh tool** (build-plan step 6c) and the dashboard dataset decision (6g, item 5), both needed before the code-plan-then-dashboard-plan flow works without manual steps.
- **More subagent types.** A researcher (read-only, internet search, a smaller model) would not use the draft, approve, branch and PR flow, so it likely starts through its own path; the `subagent_type` dispatch and the guide mechanism carry over.
- **An append argument** for adding a page without resending the others, only if resending the description proves error-prone in the Layer 2 trial.

## 6. Everything that reads the plan, and what has to change

Found by searching every file for `CodingAgentPlan`, `plan.<field>`, `["plan"]`, `get_plan_draft`, `coding_plan` and the functions below. Line numbers are as of 2026-11-20 and will have moved.

### 6a. The plan model: `app/coding_task_models.py`

| Where | Change |
|---|---|
| `CodingAgentPlan` (42-93) | Replace with the flat model in 2c and its validators. `requirements`, `tests`, `constraints`, `decisions`, `files_to_imitate` and `suggested_code` are removed as fields; `SuggestedCode` and `FileToImitate` go with them; their guidance moves into the guides (3d). |
| `review` field description (85-93) | Shortened (2a); the long text and the rubric move into the guides. |
| `CODING_RUBRIC` | Stays; listed in the code guide. `DASHBOARD_RUBRIC` is added beside it (4e). |
| `CodingPlanDraft`, `CodingAgentTask` docstrings (96-100, 191) | No change: they hold the plan as a dict. |

### 6b. The main agent's tools and prompt

| Where | Change |
|---|---|
| `coding_tools.py` 39-46 `SAVE_CODING_PLAN_DESCRIPTION` | Replace with the text in 3b. |
| `coding_tools.py` 49-61 `SaveCodingPlanArgs` | The arguments in 2a, with the actions and validators in 2b (the existing "exactly one of plan or discard" becomes the table in 2a). |
| `coding_tools.py` 103-131 `save_coding_plan` | A guide branch (returns the type's guide file, saves nothing). The save branch is as today (2d), from the flat fields. |
| `coding_tools.py` 152-163 `check_start_coding_task` | No change: `model_validate` of the stored dict validates the flat plan. |
| `coding_tools.py` 268-269, 284, 312, 345, 356, 460 | No change: they read only `name`, `goal` and `base_branch`, which stay top level. |
| `orchestrator.py` 108 `ANTHROPIC_NON_STRICT_TOOLS` | Already lists `save_coding_plan`; the flat schema is validated at dispatch. |
| `orchestrator.py` 112, 455, 636 (round rules), 818 (`SOURCE_LABELS`) | No change. A guide call and the save are separate rounds, which "once per round" allows. |
| `orchestrator.py` 938-944 `render_coding_plan` | No change: it dumps the plan into the prompt each turn, and the description cap in `save_coding_plan` already bounds what it dumps. |
| `context.py` 139-145 `LARGE REQUESTS`, `CODE CHANGES` | The sentences in 3c. |
| `context/coding_plan_guides/` | New: `code.md` and `dashboard.md` (3d, 3e). |
| **Process** | `.claude/rules/orchestrator.md`: orchestrator changes are built and tried in `notebooks/phase3_graph.ipynb` first and ported after. `coding_tools.py` and `context.py` are under that rule. |

### 6c. Showing the plan to the user: `app/markdown_display_formatting.py`

| Where | Change |
|---|---|
| 187-236 `PlanMarkdown`, `render_plan_markdown` | Header lines (name, scope, audience, new pages, reviewer), then the description as written. No per-type rendering. Used by `gateway.py` 590 (the plan window), so that endpoint needs no change. |
| 167-180 `coding_approval_message` | No change: name and goal only. |
| 262-312 `build_pull_request_body`, line 272 | Reads `task["plan"]["requirements"]`. Now includes the scope and the description. |

### 6d. The coding agent app: it has to know the type

The plan reaches `run_coding_task` as a stored dict and is validated at `orchestration.py` 239. Everything below needs the type passed down; a string is smaller than passing the plan, and the callers only branch on it.

| Where | Change |
|---|---|
| `orchestration.py` 37-38 `TASK_PREAMBLE`, 252 | One preamble per type (4b). The text sent to the agent excludes `review` and `base_branch`. |
| `orchestration.py` 155-224 `execute_coding_run` | New `subagent_type`. It selects the agent's tools and prompt (via `build_coding_agent`), the rubric (181), the clean-sandbox step (216) and the final checks (221). |
| `orchestration.py` 29, 139-152 `SKILL_FILES`, `prepare_sandbox` | Uploads skills by a fixed file list per skill. Replace with an upload of the whole `skills/` tree (build-plan step 7), which every task gets. |
| `coding_agent.py` 59-80 `SANDBOX_SYSTEM_PROMPT` | Stays the code prompt. Add `DASHBOARD_SYSTEM_PROMPT` (4a), chosen by type. |
| `coding_agent.py` 211-275 `build_coding_agent` | New type. For a dashboard task: the dashboard tools, the dashboard prompt, the grader prompt for the type, `show_renders` in the reviewer's tools and the type passed to `PatchCheckMiddleware`; `list_tables` and `get_table_schema` stay, now able to read `ml_model_results`, which `CODING_AGENT_DATASETS` lacks. |
| `coding_agent.py` 91-108 `StructuredCodingAgentResponse` | Shared by both types. The `report` description gains the dashboard wording (4c). |
| `coding_agent.py` 131-193 `PatchCheckMiddleware` | Takes the type. Passes it to `validate_patch`. For a dashboard task, adds the last-publish-and-render check (4d). |
| `patch.py` 12, 83-127 `BLOCKED_PATTERNS`, `validate_patch(patch_bytes)` | Becomes `validate_patch(patch_bytes, subagent_type)`. Common blocks apply to both. `dashboard/*` is blocked for a code task. For a dashboard task everything outside `dashboard/` is blocked, and inside it `.pbix` and `.pbi/` stay blocked (build-plan step 8). |
| `apply_and_verify.py` 113-157 `verify_patch_in_clean_sandbox` | A dashboard patch has no SQLX or Python, so it only applies the patch. |
| `apply_and_verify.py` 161-239 `build_final_checks` | Dashboard branch: the apply check only (no compile, dependency, ruff, pytest or validate); a separate function, called by type. |
| `coding_agent_app/tools.py`, `config.py` | New: the dashboard tools, `show_renders`, `POWER_BI_RENDER_TARGETS`, the coding agent's Power BI secrets, and `ml_model_results` for the coding agent's datasets (build-plan steps 2 to 6b). The publish and render tools return the hash and the page folder (4d). |
| Sandbox snapshot | Node and the pinned report CLI (build-plan step 7). |
| `coding_agent_app/telemetry/` `schema.py` 34-39, `writer.py` 33-51 | Add `subagent_type`: a `STRING` column in the schema and `plan["subagent_type"]` in the row. BigQuery cannot add a `REQUIRED` column to an existing table, so it is `NULLABLE` there (the writer always sets it), or the table is recreated. |

### 6e. Tests and scripts

| File | Change |
|---|---|
| `tests/test_orchestrator.py` 509-520 | Plan constructors use the flat fields. Add a plan-view case. |
| `tests/test_coding_tools.py` 23, 48-58 | The stored-task fixture uses the flat plan; `test_build_pull_request_body` covers the description. |
| `tests/test_patch.py` (6 calls) | `validate_patch` gains the type argument. Add: a code patch touching `dashboard/` fails, a dashboard patch touching anything else fails, `.pbix` inside `dashboard/` fails. |
| `tests/test_apply_and_verify.py` | A dashboard branch of the final checks. |
| `scripts/coding_agent_tasks.py` | Ten plans use `requirements=` and `tests=`: rewrite each as a `description`. |
| Notebooks | `notebooks/phase6_coding_agent.ipynb` is parked and keeps its own copy. |

**New tests, lean:** the validators (one action, `new_pages` range and dashboard-only, the length caps, guide mode); the path guard by type; the last-publish-and-render check as a pure function over messages and hashes. The guides are prose: Layer 2, a trial of the main agent writing an add, an edit and a delete plan from the dashboard guide and a code plan from the code guide.

### 6f. Docs and rules

| File | Change |
|---|---|
| `docs/coding-agent.md` | The plan section (130-163) and the tool table (118, 121-122): the flat envelope, the guide call, the caps. The patch rules (260-261, 401). The final checks and clean sandbox (755, 819). The rubric section (1113+): one rubric per type. The skills upload. |
| `docs/dashboard-editing.md` | Patch rules and steps 8 and 9 (a type-aware guard, not a narrowed `dashboard/*`), the dashboard checks (no separate final validate), step 9b, the reviewer's `show_renders` (step 9c). |
| `docs/frontend.md` (539-560) | The plan window renders markdown, so no change. |
| `.claude/rules/tools.md`, `gateway.md`, `orchestrator.md` | Each mentions `save_coding_plan` or `coding_plan`: the tool's arguments, the plan window, the state field. Check and update. |
| `docs/build-order.md` | The status block and the Phase 6 dashboard item. |

### 6g. Open questions and things the audit turned up

1. **Code plans move to the new shape too** (decided 2026-11-23): one tool, one shape. Today's structured fields become the code guide's sections, and the reviewer sees a text plan. The draft is revised as we build; the regression test of code tasks (6h, step 3b) is the check that this did not break code tasks.
2. **Where the guides live:** `context/coding_plan_guides/` (decided).
3. **The caps:** 500 characters for `scope`, 20,000 for `description`. The second only stops something outrageous; the first keeps the summary short. Check both against real plans.
4. **`review`'s rubric** lives in the guides, not the schema (decided); the rest of its description stays in the argument.
5. **Dataset access for dashboard tasks:** leave as is for now (step 6d of `docs/dashboard-editing.md`); revisit if the coding agent cannot check a table it is told to bring in.
5b. **The main agent plans a data change from the repo, not from the data.** It cannot query the pre-model datasets, but it can read the SQLX (or the notebook for data after the model) with its code tools and see the schemas, which is enough to plan; the coding agent checks real tables with its own dry runs and, once 6d is decided, `ml_model_results`. It also cannot see whether the user has built the table, so it relies on the user saying so.
6. **Page info for the coding agent** is a separate idea (step 9d of `docs/dashboard-editing.md`).
7. **The dashboard tools do not exist yet** (build-plan steps 2 to 6b). The main agent side (6a to 6c) can be built and tried first.

### 6h. A build order

1. The plan model, its validators and their tests (`app/coding_task_models.py`, new tests).
2. The guides and the main agent side, in the notebook first: `save_coding_plan`'s arguments and description, the guide call, the system prompt sentences, the plan view and PR body. Then port. A Layer 2 trial of the guides: code, and the dashboard add and model-only plans now; the dashboard edit and delete plans after step 4b.
3. The coding agent's type plumbing: the path guard, `TASK_PREAMBLE` and prompt by type, the grader prompt by type, the skills tree upload, telemetry `subagent_type`. The `project-background` skill is already built (`coding_agent_app/skills/project-background/`): its two text files are copied from `context/orientation/` by `Dockerfile.coding-agent`, which replaces the local test copies, and it is in `SKILL_FILES` until the tree upload replaces that list.
3b. **Regression test of code tasks, before any dashboard tool is built.** The code path now runs through the new plan shape, the type dispatch and the changed prompts and checks. Re-run the ten plans in `scripts/coding_agent_tasks.py` (rewritten as descriptions) and compare with earlier results, with the reviewer on and off, to confirm the coding agent still works for code. The user runs it (live credentials).
4. The dashboard tools (build-plan steps 2 to 6b), then the last-publish-and-render check and `show_renders`.
4b. **The main agent's read tools for dashboards**, so it can see the dashboard it is planning a change to. Both depend on the shared render tool from step 4.
   - `get_page_info` extended (`docs/dashboard-editing.md` step 11): a page can be rendered live in the Chromium browser, including a slicer or bookmark state, and the Agent Sandbox's copy of the dashboard can be viewed, not only the production reference page.
   - `get_repo_contents` extended to read the PBIP files (the report and model folders under `dashboard/pbip`), so the main agent can read a page's visuals and the model's tables when it plans an edit or a delete.
   The Layer 2 trial of the dashboard guide (edit and delete plans) runs after these exist, since those plans refer to pages and visuals that already exist.
5. An end-to-end dashboard task, then the dashboard rubric tuned on real runs.
