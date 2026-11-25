# Dashboard planning: draft for review (2026-11-20, revision 3)

**Superseded by `docs/dashboard-planning.md` (the flat plan). Kept for reference only; do not build from this file.**

**Status: draft, nothing built.**

**Reconstructed 2026-11-23.** This file was overwritten by revision 4 (now `docs/dashboard-planning.md`). It was untracked, so git could not restore it. It is rebuilt from the session's reads of it with every later edit applied; wording may differ from the lost original in places.

**Decided so far**
- A plan is for `code` or for the `dashboard`, exactly one. A request that needs both is done one plan at a time.
- Shared fields stay at the top level of the plan (no `shared:` wrapper). Only the divergent parts are nested.
- Page contents (`cards`, `charts`, ...) are short strings.
- Model changes are part of the dashboard plan: one task, not a separate model task. The coding agent works out the measures and
  columns the pages need; `model_changes` holds only what the user specified, or a model-only request. A plan may have no pages.
- Plans saved in the old flat shape stop validating (accepted break). No `@property` unless a caller needs it.
- The main agent owns placement: each page carries a `layout` sentence that defaults to the standard arrangement
  (`DEFAULT_PAGE_LAYOUT`) and is rewritten only when the user asks, so the user sees and approves where things go.
- `slicers` is required and never empty: `["None"]` when the user wants no slicers, so an empty list cannot read as "not asked".
- `cards` and `slicers` are each one row, listed left to right in the order they appear, with a cap that makes them fit (5 cards, 4
  slicers; both enforced with `max_length`, and starting points to check against the real 1500-pixel-wide pages).
- A dashboard plan says whether it edits the existing dashboard or builds a new one (`target`). Only `existing` is accepted until
  phase 2. `target` decides the mode path (below), as Microsoft's greenfield versus focused-edit split does.
- Every task gets all the skills (only their short frontmatter loads until one is used). Only the tool set follows the plan's kind.
- A dashboard plan holds at most 3 pages (`max_length=3`); a bigger dashboard is several plans, one after another.
- A page is added or changed with a `page` argument on `save_coding_plan`, merged into the saved draft by name, so the main agent
  never resends the other pages (section 2d). Descriptions written for the main agent do not explain the coding agent's behavior.
- A second reviewer rubric for dashboards, and giving the reviewer the final renders, are **later** (section 5).

## 1. Does today's plan fit a dashboard?

`CodingAgentPlan` today (`app/coding_task_models.py`) is one flat object written for ML-repo code.

| Field | Fits a dashboard? | Why |
|---|---|---|
| `name`, `goal`, `base_branch` | Yes | Same meaning. `goal` is the purpose. |
| `review` | Yes, but the criteria do not | The rubric in its description is about SQL columns, `rowConditions` and dry runs. Dashboards need their own (later). |
| `requirements` | No | "One deliverable per item, each naming the table, file or model" does not describe pages, cards and slicers. |
| `tests` | No | Pytest behaviors. A dashboard is checked by validate and render. |
| `constraints`, `decisions` | Yes | "Do not touch page X"; choices the user settled. |
| `files_to_imitate` | Yes | An existing page or visual to copy. |
| `suggested_code` | Yes | A DAX measure the main agent tested with its DAX tool. |

Also code-specific, outside the model: the system prompt, `TASK_PREAMBLE`, the rubric, the path rules, the plan view
(`render_plan_markdown`), the PR body (`task["plan"]["requirements"]`) and the tool set.

## 2. The plan model

### 2a. Shape

```
CodingAgentPlan
  top level:  name, goal, base_branch, review, constraints, decisions, files_to_imitate, suggested_code
  code:       CodeWork       requirements, tests                      # SQLX, Python, notebooks in the ML repo
  dashboard:  DashboardWork  audience, pages[], model_changes, style  # the Power BI dashboard
```

### 2b. Draft code (not written to the repo)

```python
DEFAULT_PAGE_LAYOUT = ("The page title at the top. Any slicers in one row beneath the title, then the cards in one row beneath the "
                       "slicers. Slicers and cards are each left to right in the listed order. Then the charts in the listed order "
                       "reading left to right and down, so the first is top left, with the detail table last.")
DEFAULT_EDIT_LAYOUT = ("Keep the existing visuals where they are. Put new ones in free space or beside the visuals they relate to, "
                       "and move or resize existing ones only to make room.")


class DashboardPage(BaseModel):
    page_name: str = Field(description=(
        "The page's display name exactly as in the dashboard when change is 'edit', or the new page's name when it is 'add'."))
    change: Literal["edit", "add"] = Field(description=(
        "'edit' changes an existing page; 'add' creates a new one. In a plan with target 'new', every page is 'add'."))
    answers: str = Field(description=(
        "What this page lets its audience see or decide, in one or two sentences and the user's words (for example 'which model "
        "to ship, and where the champion is weakest'). The coding agent chooses the layout and chart types from this."))
    cards: list[str] = Field([], max_length=5, description=(
        "Headline numbers in one row, listed left to right in the order they appear, at most 5 (about 250 to 300 pixels each, so "
        "more will not fit on one row). One item each: the number, based on what the user asked for but made clearer (for example "
        "'Champion F1'), and a label if it should differ. Use an existing measure's name if you know one that fits; the coding agent "
        "finds or builds the measure. If the user wants more than 5, tell them it will not fit and ask which to keep or whether to "
        "split them across pages."))
    charts: list[str] = Field([], description=(
        "Charts and tables in priority order: the first is the most important. One item each: what it shows, based on what the user "
        "asked for but made clearer (a measure by a field, for example 'Champion F1 by model'), and the chart type only if the user "
        "asked for one. The coding agent finds or builds the measures. A detail table goes last."))
    slicers: list[str] = Field(min_length=1, max_length=4, description=(
        "Page slicers in one row, listed left to right in the order they appear, at most 4 (more will not fit on one row). One "
        "item each: the field, and 'synced with <page>' if it should share state. Write ['None'] if the user wants no slicers: "
        "never leave this empty. If the user wants more than 4, ask which matter most."))
    field_parameters: list[str] = Field([], description="Field parameters, each with the fields it switches between.")
    bookmarks: list[str] = Field([], description="Bookmarks, each with the state it saves.")
    buttons: list[str] = Field([], description="Buttons, each with what it does (navigate, apply a bookmark, clear slicers).")
    layout: str | None = Field(None, description=(
        "Where things go on the page. Leave it out unless the user asked for something different: a new page gets the standard "
        "arrangement and an edited page keeps its existing visuals in place. If they did ask, say so here, keeping the rest "
        "(for example 'the trend chart top right')."))

    @model_validator(mode="after")
    def _default_layout(self) -> "DashboardPage":
        if self.layout is None:
            self.layout = DEFAULT_PAGE_LAYOUT if self.change == "add" else DEFAULT_EDIT_LAYOUT
        return self


class DashboardWork(BaseModel):
    target: Literal["existing", "new"] = Field("existing", description=(
        "'existing' edits the dashboard that is already there (changing its pages or adding pages to it). 'new' builds a dashboard "
        "from scratch. Set it from what the user asked for; ask if it is unclear."))
    audience: str = Field("", description=(
        "Who uses the dashboard and how (for example 'the ML team, to decide which model ships'). Required when the plan has pages."))
    pages: list[DashboardPage] = Field([], max_length=3, description=(
        "One item per page to edit or add, at most 3 per plan. A larger dashboard is planned in parts, one plan after another. "
        "Leave empty when the task changes only the model."))
    model_changes: list[str] = Field([], description=(
        "Only what the user asked for specifically in the semantic model, one item each, with what it computes or holds: a "
        "definition or name they gave, a table to bring in (a BigQuery table from the Instacart ML project that is already built), "
        "or the whole request when it is a model-only task. Leave empty otherwise: the coding agent works out the measures and "
        "columns the pages need. Data that is not built yet, or that should be aggregated first, is a code plan before this one."))
    style: str = Field("Match the existing dashboard's colors, fonts and card style.",
                       description="Leave as is unless the user asked for something different.")

    @model_validator(mode="after")
    def _target_rules(self) -> "DashboardWork":
        if self.target == "new":
            raise ValueError("Building a new dashboard is not supported yet: plan changes to the existing dashboard.")  # until phase 2
        if not self.pages and not self.model_changes:
            raise ValueError("A dashboard plan needs at least one page or one model change.")
        if self.pages and not self.audience:
            raise ValueError("A plan with pages needs an audience: who uses the dashboard and how.")
        return self


class CodeWork(BaseModel):
    requirements: list[str] = ...   # unchanged from today
    tests: list[str] = ...          # unchanged from today


class CodingAgentPlan(BaseModel):
    name, goal, base_branch, constraints, decisions, files_to_imitate, suggested_code   # unchanged
    review: bool = ...              # unchanged; the criteria text is per kind (section 5)
    code: CodeWork | None = None
    dashboard: DashboardWork | None = None

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> "CodingAgentPlan":
        if (self.code is None) == (self.dashboard is None):
            raise ValueError("Set exactly one of code or dashboard. A plan changes either the ML repo's code or the dashboard, "
                             "never both: save one plan now and the other after it finishes.")
        return self
```

**No `@property`.** The validator enforces one kind and needs none. A caller that must branch (the path guard, tool set, prompt,
plan view, PR body) writes `plan.dashboard is not None` inline; add a property only if that repeats enough to earn one.

### 2c. What else changes

(Summary only. The full list, found by searching every reader of the plan, is section 6.)

| Place | Change |
|---|---|
| `render_plan_markdown` | Per kind. A dashboard plan shows the audience, each page with its cards, charts, slicers and the rest, then model changes and style. A plan with no pages skips the audience and pages. |
| `coding_approval_message` and task summaries | None: they read only `name` and `goal`. |
| `build_pull_request_body` | Reads `task["plan"]["requirements"]`. A dashboard task lists its pages instead, or its model changes when it has no pages. |
| `TASK_PREAMBLE` | One variant per kind (4b). |
| `scripts/coding_agent_tasks.py` (10 plans), `tests/test_orchestrator.py` | `requirements=` and `tests=` move inside `code=CodeWork(...)`. |
| Old stored drafts and tasks | Accepted break: their flat `requirements` fails validation. |
| `save_coding_plan` | Gains `page` and `remove_page` arguments (2d). Still non-strict, validated at dispatch. The nested schema is larger, so check it still binds. |

### 2d. Saving a page: merged into the draft by name

`save_coding_plan` replaces the whole draft, so a multi-page plan has a real failure mode: the main agent sends only the page it just
planned and the earlier pages are gone, or it rewrites a large plan and quietly changes a page it should not have touched. A `page`
argument removes both by construction: the agent sends one page, the tool merges it into the saved plan, and the other pages are never
retyped.

```python
class SaveCodingPlanArgs(FlatToolArgs):
    plan: CodingAgentPlan | None = Field(None, description=(
        "The whole plan: the first save, or a change to anything except a single page. Every save of a plan replaces the draft, so "
        "send all of it. Leave out when using page, remove_page or discard."))
    page: DashboardPage | None = Field(None, description=(
        "One page of the saved dashboard plan, added to it or replacing the page with the same page_name. Send only this page: the other "
        "pages are kept."))
    remove_page: str | None = Field(None, description="The name of a page to remove from the saved dashboard plan.")
    discard: bool = Field(False, description="Set to true instead of a plan to delete the saved draft, ...")

    @model_validator(mode="after")
    def _exactly_one_action(self) -> "SaveCodingPlanArgs":
        chosen = [self.plan is not None, self.page is not None, self.remove_page is not None, self.discard]
        if sum(chosen) != 1:
            raise ValueError("Set exactly one of plan, page, remove_page or discard=true.")
        return self
```

**What the tool does with each**
- `plan`: as today, replaces the draft (checks `base_branch`). The first save of a dashboard plan sends the plan with its first page.
  A `plan` sent while a dashboard draft exists may not drop a saved page: it is refused with "This plan drops page(s) <names>. Include
  them, or use remove_page." So even a full resend cannot silently delete one.
- `page`: needs a saved dashboard plan, else "Save the plan first with plan, then add pages with page." It replaces the saved page with
  the same `page_name` (exact match), or appends. The merged plan is validated as a whole, so the 3-page cap and every field rule apply:
  a fourth page is refused with "A dashboard plan holds at most 3 pages. Plan the others after this one finishes."
- `remove_page`: removes the page with that `page_name`, or errors with the saved page names if it is not found, or if it is the last
  page and the plan has no `model_changes` ("use discard instead"). Renaming a page is `remove_page` plus `page`.
- Every action counts as a new version, keeps `first_saved_at`, and ends the reply with the pages now in the plan ("Pages: Model
  Evaluation, Cost", or "No pages: model changes only") and whether this save added a page, replaced one or removed one, so the
  agent and the user see the same state.

Plan-level fields (`goal`, `audience`, `model_changes`, `style`, `constraints`, `decisions`) are still changed by sending the whole
`plan`. They are small, and the draft is already attached to the prompt each turn for the agent to copy from.

**Size.** Pages are added one at a time, so a save costs one page (roughly 150 to 300 tokens) regardless of plan size.

## 3. The main agent's planning guidance

The main agent talks to the user and writes the plan. It looks things up and asks the user only what the coding agent cannot be
left to decide.

### 3a. System prompt, `CODE CHANGES` paragraph, additions

> A plan changes either the ML repo's code or the dashboard, never both. When a request needs both, plan one at a time and finish
> it before planning the other. If a dashboard change needs data that is not built yet, or that should be aggregated first, the
> code plan comes first. When the user has run Dataform to build the tables, plan the dashboard change, which brings the tables
> into the model. Plan the pages (cards, charts, slicers, what each page is for), not the model: the coding agent works out the
> measures and columns they need, so put in `model_changes` only what the user specified. A request that changes only the model
> (a measure, a column, a table) is a dashboard plan with no pages: put it in `model_changes`.

(The coding agent refreshes the model after it adds a table once build-plan step 6c exists. Until then the user refreshes it.)

### 3b. `save_coding_plan` description, revised

The existing description with the new parts folded in. The new sentences run from "Send the whole plan with `plan`" to "showing the user
only that page":

> Saves a draft of a coding task's plan, to build and change with the user, and start with start_coding_task once the user has
> approved it. Call it when the user wants a change made to the ML repo's code or to the dashboard, and again each time the plan
> changes: every save counts as a new version. Send the whole plan with `plan` when you start one or change anything but a single
> page; that replaces the draft. A plan is for code or for the dashboard, never both: set `code` or `dashboard`, and when a request
> needs both, plan one now and the other after it finishes. A dashboard that needs a table that is not built yet, or data that should
> be aggregated first, needs the code plan first: aggregate in SQLX, and once the user has built the tables with Dataform, plan the
> dashboard change, which brings them into the model. Settle the plan by working out what you can yourself and asking the user for
> the rest. A dashboard plan holds at most 3 pages; plan any more after it finishes. With more than one page, settle what is shared
> first, save the plan with the first page, then add each further page with `page`, showing the user only that page. Save a plan
> before showing it to the user, and save each change before describing it to the user. Set discard to true instead of a plan to
> delete the draft. Call it only once per round, and never in the same round as start_coding_task or open_pull_request.

What the plan has to contain is not repeated here: the field descriptions in 2b say it.

### 3c. Placement

**The main agent owns the placement defaults.** Only the main agent talks to the user, so it is the only place a user's different
wish can enter. Each page has a `layout` sentence, filled in with a default when the plan is saved: for a new page the standard
arrangement (page title at the top, any slicers in a row beneath it, cards in a row beneath the slicers, charts in priority order reading
left to right and down, detail table last), and for an edited page "keep the existing visuals where they are". The main agent leaves it alone
unless the user asked for something different, then rewrites it. The default is saved with the plan, so the user sees where things will
go when they approve it. The coding agent follows each page's `layout` and uses `layout.md` only for the geometry (grid, spacing,
bounds, slicer sizing).

A per-item position (a small object per chart) would be finer-grained but larger, and a page-level sentence covers it: a specific wish
goes in that sentence. Say if you want per-item positions.

### 3d. Settle the shared things once, then plan page by page

A multi-page plan is too big to write and review in one turn (the main agent has about two minutes), and most of what it needs applies
to every page. It reuses the existing `LARGE REQUESTS` mechanism instead of a new one.

**Once, before any page** (skip any the user's request already answers):
1. What the dashboard should help them do or decide (`goal`).
2. Who uses it (`audience`).
3. The page list: which existing pages change and which are new, with one line each on what the page answers.
4. Slicers: "the same on every page, or do some differ?" Write the shared ones once and ask about exceptions per page.
5. Style: matching the existing dashboard unless they say otherwise.

**Then the pages.** The main agent saves a task list with `manage_tasks`, one task per page to plan, and asks whether to proceed. It
saves the plan with the first page (`plan`), then adds each further page with `page`, one per turn. It shows the user only the page
just added (the full plan is always open from the coding plan icon), takes their changes, marks the task done, and offers the next page
as the follow-up. A button that navigates to a page not planned yet is added when that page is.

**More than 3 pages is more than one plan.** The main agent tells the user it will be built in parts of up to 3 pages, plans the first
part now, and plans the next after the first has finished (the one-at-a-time rule, applied to building).

When the last page of a part is in, the user approves the plan once, and it becomes one coding task.

**Where the main agent reads this:** the `save_coding_plan` description (3b) and the plan's field descriptions (2b). The system
prompt's `LARGE REQUESTS` paragraph gets one sentence: "A dashboard plan with more than one page is a large request: settle the
shared things first, then plan one page at a time." Microsoft's `planning.md` Rounds 1 and 3 (audience and job choices, global versus
page slicers) are the source for suggested choices to offer when a user is unsure, such as executives, analysts or operators for the
audience. They are not copied into the repo.

**Open: shared slicers in the model.** Each page lists its own `slicers`, so a shared set is repeated on every page. A plan-level
`slicers_on_every_page` list (each page's `slicers` then holding only extras, `["None"]` still allowed) would say it once. Recommend it
if most dashboards share their slicers, and skip it if pages usually differ.

## 4. The coding agent's planning guidance

For a plan of kind `dashboard`. A `code` plan keeps `SANDBOX_SYSTEM_PROMPT` as it is.

### 4a. System prompt addendum for a dashboard task (replaces "WHERE YOU WORK" and "CHECKING YOUR WORK")

```
WHERE YOU WORK
You work on the Power BI dashboard in {WORK_DIR}/dashboard/pbip: the report (the folder ending in .Report) and the semantic model
(the folder ending in .SemanticModel). Change only files under dashboard/. Do not run git commit or git push. You have two dashboard skills:
/skills/powerbi-report for the report (pages, visuals, filters, themes) and /skills/semantic-model for the model (measures, columns,
tables, relationships, functions). Use the one the plan needs, or both: a plan may change only the model, or only the report. Put scratch files in /tmp, never in the repo, and keep there any note you will need later in a
long task (a design, a model inventory): your context can be summarized, files cannot. Re-read them before the step that needs them.

BEFORE YOU BUILD
Read the plan, then the dashboard: list its pages and visuals (powerbi-report-author preview-pages and preview-visuals) and query
the model (run_dax_query with INFO.VIEW.TABLES(), COLUMNS(), MEASURES()). Check that every page, measure and field the plan names
exists, or that you can build it from the model and the data in BigQuery. Stop with blocking_questions, as soon as you notice, if:
the plan has pages and does not say who the dashboard is for, or what a page should answer; a field or measure it names does not
exist and its meaning is not clear enough to build; it needs data that is not in BigQuery yet; or an error or missing capability keeps you from meeting its core requirements. Do not ship a weaker
substitute that quietly drops a requirement. For anything smaller, choose the sensible option, match the existing dashboard, and
list it under assumptions.

LAYOUT
Place things where each page's layout says. Use /skills/powerbi-report/references/layout.md for the geometry: the 8px grid, gaps,
bounds, and slicer sizing. You may move or resize existing visuals to make room.

HOW YOU WORK
Before you edit, call write_todos with one group per page: model changes first, then the page's visuals, then validate, publish and
render. Edit one file at a time. Copy an existing visual of the same type and keep its formatting. Test every new measure with
run_dax_query (a query-scoped DEFINE MEASURE) before you write it into the model.

CHECKING YOUR WORK
After each batch of edits run validate_dashboard and fix what it reports. When a page is ready, publish_dashboard, then
render_dashboard that page and compare the image with the plan: every card, chart and slicer it lists should be there, readable,
in priority order, with nothing clipped or overlapping. Render again after your last edit: a page is not done until you have seen it
rendered after that edit. If the plan names a bookmark or slicer state, render that state too. Say in your report what you could
not render or check.
```

The default reading path in the plan's `layout` text (left to right and down, first chart top left) is Microsoft's Z pattern for
sparse pages; its F pattern suits dense analytical pages with a filter rail. `layout.md` covers both. The main agent writes the
default, so a user who wants an F-style filter rail says so and it goes into the page's `layout`.

**Where the process lives.** Neither skill is the entry point: which one opens comes from the skill descriptions, and each one's
description names the other. The modes below belong to the report skill, so a model-only plan never goes through them and follows the
`semantic-model` router's own short workflow instead. The report skill is built from Microsoft's own structure: a dispatcher (`SKILL.md`) that sends the agent
through three mode files, `planning.md` (check the plan against the dashboard and the model, inventory, blocking questions, todos),
`design.md` (new pages and visuals only) and `authoring.md` (the validate, publish, render loop and the topic table). The prompt
therefore carries only the task's own rules, and "BEFORE YOU BUILD", "HOW YOU WORK" and "CHECKING YOUR WORK" above can shrink to
what the skill does not say: the blocking-question triggers, the last-publish rule and the report. Microsoft's `management` mode is not
used: publishing is a step inside the authoring loop, because we render the published report.

### 4b. `TASK_PREAMBLE` for a dashboard task

> The task below is a plan, as JSON. Build the pages it lists, make the model changes it lists and the ones its pages need, and follow its decisions: choices
> the user already made. Its `suggested_code` was drafted by another agent and was tested only if it says so; the model and the
> dashboard are the source of truth, so if a snippet conflicts with them or with a decision, follow those and say so in your
> report. The `files_to_imitate` show what to copy. The `answers` of each page say what it is for, and its `layout` says where things
> go; choose the chart types yourself. A `slicers` list of ["None"] means the user wants no slicers.

### 4c. Final answer

The same structured answer as today. The `report` field description gains, for a dashboard task: per page, what changed and what
is on it; the model changes made; which pages and states you rendered; and what you could not check (a drillthrough, for example).

### 4d. Checks the code makes, so the reviewer does not have to

These are deterministic, in the same places the coding agent app already checks code tasks:

| Check | Where | Replaces |
|---|---|---|
| **Path guard.** A dashboard task's patch may change only files under `dashboard/`; a code task's may not touch `dashboard/`. A violation fails the task, like today's blocked paths. | `validate_patch` / `BLOCKED_PATTERNS` in `coding_agent_app/patch.py`, given the plan's kind | The reviewer criterion "nothing outside `dashboard/`" |
| **`validate_dashboard` passes on the final patch**, run by our code on the clean clone, as the compile is for a code task. | A dashboard branch in `build_final_checks` (`apply_and_verify.py`) | The reviewer criterion "validate passed after the last edit" |
| **The last publish is the final state, and every changed page was rendered after it.** `publish_dashboard` returns a content hash of the files it sent (the same function that reads the report and model folders out of the sandbox, so the hash is over the same bytes). When the agent says it is done, the middleware hashes the sandbox's dashboard files the same way and compares it with the hash of the last successful publish. A difference is a fixable violation: "You changed the dashboard after your last publish: publish it, then render the changed pages." It also requires a `render_dashboard` of each changed page after that publish. The agent is sent back to fix it, so the last state is always one it has published and looked at. | `PatchCheckMiddleware.aafter_agent` in `coding_agent.py`, which already sees the messages and the sandbox | The reviewer criterion "a render after the last edit" |

The app does not publish the final version itself: the agent has to, because publishing is how it sees the result, and a final
publish the agent never rendered would ship unseen. Comparing hashes catches an edit after the last publish without relying on
timestamps. Line endings can make two trees differ in bytes only, so both hashes normalize them the same way (the sandbox already
restores line endings when it builds the patch).

This means the reviewer's input function (`compact_transcript`) does not change for these three. It still sees the task, the todos,
the dry runs and the report.

The tool set follows the kind: only a dashboard task is given `validate_dashboard`, `publish_dashboard`, `render_dashboard` and
`run_dax_query`. The skills do not: every task gets every skill, since an unused skill costs only its short frontmatter.

## 5. Later (notes, not for now)

- **A dashboard reviewer rubric.** `CODING_RUBRIC` is written for SQL and Python. A second rubric, selected by the plan's kind, for the
  judgment criteria the code cannot check: every page the plan lists is present with every card, chart, slicer, bookmark and button it
  names; every field a visual uses exists in the model or is added by the diff; new model objects have a `///` description and a
  `formatString`; no bookmark, interaction or slicer sync points at something the diff removed; the diff changes nothing under
  `dashboard/` the plan did not ask for; the report matches the diff. `review`'s description lists the criteria so the main agent can
  tell the user what is checked, so with two rubrics it lists both under headings.
- **Give the reviewer the final render of each changed page**, so it can judge the picture against the plan. That needs
  `compact_transcript` to include the images, which is a larger change than the rubric alone.
- **The refresh tool** (build-plan step 6c) and the dashboard dataset decision (6g, item 5), both needed before the code-plan-then-
  dashboard-plan flow works without manual steps.

## 6. Everything that reads the plan, and what has to change

Found by searching every file for `CodingAgentPlan`, `plan.<field>`, `["plan"]`, `get_plan_draft`, `coding_plan` and the functions
below. Line numbers are as of 2026-11-20.

### 6a. The plan model: `app/coding_task_models.py`

| Where | Change |
|---|---|
| `CodingAgentPlan` (42-93) | Replace as in 2b: shared fields stay, `requirements` and `tests` move into `CodeWork`, add `DashboardWork`, `DashboardPage`, `DEFAULT_PAGE_LAYOUT`, the exactly-one validator. |
| `review` field description (85-93) | It embeds `CODING_RUBRIC`. The dashboard rubric is built before the first dashboard task is tried (a first pass is written), and `review`'s description then lists both rubrics under headings (section 5). No validator needed. |
| `CodingPlanDraft`, `CodingAgentTask` docstrings (96-100, 191) | No change: they hold the plan as a dict. |

### 6b. The main agent's tools and prompt

| Where | Change |
|---|---|
| `coding_tools.py` 39-46 `SAVE_CODING_PLAN_DESCRIPTION` | Replace with the text in 3b. |
| `coding_tools.py` 49-61 `SaveCodingPlanArgs` | Add `page` and `remove_page`; the validator becomes "exactly one of plan, page, remove_page, discard" (2d). |
| `coding_tools.py` 103-131 `save_coding_plan` | Three new branches. `plan`: as today, plus "may not drop a saved page" when a dashboard draft exists. `page` and `remove_page`: load the saved draft, merge or remove by name, re-validate the whole plan (this applies the 3-page cap and every field rule), save as a new version. `check_base_branch` runs only for `plan`. The version, `saved_at` and `first_saved_at` logic is shared by all three. The reply uses `plan.name` and `plan.review` (125-129), so it must read them from the merged plan, and it ends with the pages now in the plan. |
| `coding_tools.py` 152-163 `check_start_coding_task` | No change: `model_validate` of the stored dict validates the nested plan. |
| `coding_tools.py` 268-269, 284, 312, 345, 356, 460 | No change: they read only `name`, `goal` and `base_branch`, which stay top level. `get_coding_task(include_plan=True)` returns the nested dict as is. |
| `orchestrator.py` 108 `ANTHROPIC_NON_STRICT_TOOLS` | Already lists `save_coding_plan`, so the larger nested schema is validated at dispatch, not in a strict grammar. Check it still binds. |
| `orchestrator.py` 112, 455, 636 (round rules), 818 (`SOURCE_LABELS`) | No change. One page per turn fits "once per round". |
| `orchestrator.py` 938-944 `render_coding_plan` | No change in code: it dumps the plan as JSON into the prompt each turn. A dashboard plan is longer (three pages), and it stays attached until the task starts. |
| `context.py` 139-145 `LARGE REQUESTS` and `CODE CHANGES` | Add the sentences in 3a and 3d. |
| **Process** | `.claude/rules/orchestrator.md` says orchestrator changes are built in `notebooks/phase3_graph.ipynb` first and ported after. `coding_tools.py` and `context.py` are under that rule, so the main agent changes are built and tried in the notebook first. |

### 6c. Showing the plan to the user: `app/markdown_display_formatting.py`

| Where | Change |
|---|---|
| 187-236 `PlanMarkdown`, `render_plan_markdown` | Per kind. A code plan renders as today. A dashboard plan renders the audience, then each page (what it answers, slicers, cards, charts, field parameters, bookmarks, buttons, layout), then model changes and style. The shared sections (constraints, decisions, files to imitate, suggested code) are unchanged. Used by `gateway.py` 590 (the plan window), so that endpoint needs no change. |
| 167-180 `coding_approval_message` | No change: name and goal only. |
| 262-312 `build_pull_request_body`, line 272 | Reads `task["plan"]["requirements"]`. A dashboard task lists its pages instead. The task is a stored dict, so the check is `"dashboard" in task["plan"]` with a non-null value. |

### 6d. The coding agent app: it has to know the kind

The plan reaches `run_coding_task` as a stored dict and is validated at `orchestration.py` 239. Everything below needs the kind (or
the plan) passed down. Today none of it does.

| Where | Change |
|---|---|
| `orchestration.py` 37-38 `TASK_PREAMBLE`, 252 | One preamble per kind (4b). The JSON sent to the agent still excludes `review` and `base_branch`. |
| `orchestration.py` 155-224 `execute_coding_run` | New `kind`. It selects the agent's tools and prompt (170, via `build_coding_agent`), the rubric (181), the clean-sandbox verification (216) and the final checks (221). |
| `orchestration.py` 29, 139-152 `SKILL_FILES`, `prepare_sandbox` | Uploads skills by a fixed file list per skill. Replace with an upload of the whole `skills/` tree (build-plan step 7), which every task gets, including the nested `references/` and `assets/` folders. |
| `coding_agent.py` 59-80 `SANDBOX_SYSTEM_PROMPT` | Stays the code prompt. Add the dashboard prompt (4a), chosen by kind. |
| `coding_agent.py` 211-275 `build_coding_agent` | New `kind`. For a dashboard task: the dashboard tools instead of `list_tables, get_table_schema, dry_run_sql` (but still `list_tables` and `get_table_schema`, now able to read `ml_model_results`, which `CODING_AGENT_DATASETS` lacks), the dashboard prompt, and the kind passed to `PatchCheckMiddleware`. |
| `coding_agent.py` 91-108 `StructuredCodingAgentResponse` | Shared by both kinds. The `report` description gains the dashboard wording (4c). |
| `coding_agent.py` 131-193 `PatchCheckMiddleware` | Takes the kind. Passes it to `validate_patch`. For a dashboard task, adds the "last publish is the final state, and every changed page was rendered after it" check as a fixable violation (4d). It reads the messages and the patch already. |
| `patch.py` 12, 83-127 `BLOCKED_PATTERNS`, `validate_patch(patch_bytes)` | Becomes `validate_patch(patch_bytes, kind)`. Common blocks (`.git/`, `.github/`, `images/`, secrets) apply to both. `dashboard/*` is blocked for a code task. For a dashboard task everything outside `dashboard/` is blocked, and inside it `.pbix` and `.pbi/` stay blocked (build-plan step 8). |
| `apply_and_verify.py` 113-157 `verify_patch_in_clean_sandbox` | Runs `dataform compile`, dependency install, ruff and pytest. A dashboard patch has no SQLX or Python, so for that kind it only applies the patch, then the report and model folders are read out for `validate_dashboard`. |
| `apply_and_verify.py` 161-239 `build_final_checks` | Dashboard branch: the apply checks, plus "validate_dashboard passes on the patched files". The compile, dependency, ruff and pytest checks are skipped. `compiled_tables` and `compile_errors` stay empty. The docstring says task-specific checks do not belong here, so the dashboard branch is its own function that this one calls by kind. |
| `coding_agent_app/tools.py`, `config.py` | New: the dashboard tools (`validate_dashboard`, `publish_dashboard`, `render_dashboard`, `run_dax_query`), `POWER_BI_RENDER_TARGETS`, the coding agent's Power BI secrets, and `ml_model_results` for the coding agent's datasets. These are build-plan steps 2 to 6b. |
| Sandbox snapshot | Node and the pinned report CLI for the `powerbi-report-author` lookups (build-plan step 7). |
| `coding_agent_app/telemetry/` `schema.py` 34-39, `writer.py` 33-51 | No change needed: the row stores `plan_name`, `plan_goal`, `base_branch`, `review_requested` (all top level) and the whole plan as `plan_json`, so the kind is readable from it. A `plan_kind` column is optional and would need a table change. |
| `orchestration.py` 66-112 `build_coding_result`, `CodingAgentResult` | No change: it maps outcome, checks, files and the agent's answer. |

### 6e. Tests and scripts

| File | Change |
|---|---|
| `tests/test_orchestrator.py` 509-520 | The plan constructors (`requirements=`, `tests=`) move inside `code=CodeWork(...)`. Add a dashboard rendering case. Line 147 builds a draft dict with only `name` and `review`, so it is unaffected. |
| `tests/test_coding_tools.py` 23, 48-58 | The stored-task fixture has `requirements` at the top. Move it under `code`, and add a dashboard task to `test_build_pull_request_body`. |
| `tests/test_patch.py` (6 calls) | `validate_patch` gains the kind argument. Add: a code patch touching `dashboard/` fails, a dashboard patch touching anything else fails, `.pbix` inside `dashboard/` fails. |
| `tests/test_apply_and_verify.py` | A dashboard branch of the final checks. |
| `tests/test_coding_task_store.py`, `test_coding_task_telemetry.py`, `test_entry_exit.py`, `test_gateway.py` | Fixtures hold only `name`, `goal`, `base_branch`, `review`, or no plan: no change. |
| `scripts/coding_agent_tasks.py` | Ten plans use `requirements=` and `tests=`: move inside `code=CodeWork(...)`. The module-level piece lists stay. |
| Notebooks | `notebooks/phase6_coding_agent.ipynb` is parked and keeps its own copy; it is not updated. |

**New tests, lean:** the exactly-one validator; the 3-page cap and the slicer and card caps; the page merge as a pure function (add,
replace by name, remove, drop refused, last page refused); the dashboard plan view; the path guard by kind; the render-after-last-edit
check. The tool-set and prompt selection by kind is wiring: Layer 2.

### 6f. Docs and rules

| File | Change |
|---|---|
| `docs/coding-agent.md` | The plan section (130-163) and the tool table (118, 121-122): the nested plan, `page` and `remove_page`, the 3-page cap. The patch rules (260-261, 401). The final checks and clean sandbox (755, 819). The rubric section (1113+): one rubric per kind, the dashboard one later. The skills upload. |
| `docs/dashboard-editing.md` | Patch rules and steps 8 and 9 (a kind-aware guard, not a narrowed `dashboard/*`), the dashboard checks (validate as a final check, render after the last edit), step 9b. |
| `docs/frontend.md` (539-560) | The plan window renders markdown, so no change; a line saying a dashboard plan shows its pages. |
| `.claude/rules/tools.md`, `gateway.md`, `orchestrator.md` | Each mentions `save_coding_plan` or `coding_plan`: the tool's arguments, the plan window, the state field. Check and update. |
| `docs/build-order.md` | The status block and the Phase 6 dashboard item. |

### 6g. Things the audit turned up that the draft did not cover

1. **The reviewer.** `execute_coding_run` hands every task `CODING_RUBRIC` when `review` is on, so a dashboard task needs its own rubric selected by kind (section 5). Not a blocker for planning: the rubric, with a first pass already written, is finished before a dashboard task is tried.
2. **The kind has to travel through six functions** (`run_coding_task`, `execute_coding_run`, `build_coding_agent`,
   `PatchCheckMiddleware`, `validate_patch`, `build_final_checks`). Passing the validated plan, or a `kind` string, is the choice. A
   string is smaller and the callers only branch on it.
3. **`StructuredCodingAgentResponse` is shared**, so its field descriptions cannot be written for one kind. The dashboard wording goes in
   as a conditional sentence ("for a dashboard task...").
4. **The dashboard tools do not exist yet** (build-plan steps 2 to 6b). Nothing in 6d can be run end to end before them. The plan model
   and the main agent side (6a to 6c) can be built and tried first.
5. **Dataset access for dashboard tasks is undecided.** `CODING_AGENT_DATASETS` is `instacart`, `building_blocks` and `ml_model_feature_table`, but the dashboard's tables live in `ml_model_results`, so a dashboard task cannot check that a table it is told to bring in exists. Either give it `ml_model_results`, or a new dataset for dashboard-bound tables, with guidance on which dataset to use when. In the build plan (step 6d of `docs/dashboard-editing.md`).
6. **`save_coding_plan` is under the notebook-first rule** (6b, last row).

### 6h. A build order

1. The plan model, its validators, the page merge as a pure function, and their tests (`app/coding_task_models.py`, new tests).
2. The main agent side, in the notebook first: `save_coding_plan`'s arguments and description, the system prompt sentences, the plan
   view and PR body per kind. Then port.
3. The coding agent's kind plumbing: the path guard, `TASK_PREAMBLE` and prompt by kind, the skills tree upload.
4. The dashboard tools (build-plan steps 2 to 6b), then the dashboard final check and the last-publish-and-render check.
5. An end-to-end dashboard task, then the dashboard rubric and reviewer input (section 5).
