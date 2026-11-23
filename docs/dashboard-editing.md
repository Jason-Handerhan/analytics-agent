# Dashboard editing and the shared page-render tool

The coding agent edits the Power BI dashboard's PBIP files, publishes to the Agent Sandbox workspace, and looks at
the result through a headless screenshot of the **published** report. The render step is one shared
tool: the coding agent uses it on the Agent Sandbox workspace, and the main agent can use it on production for
visual grounding. Decided 2026-11-19 after an experiment (proven items below); nothing here is built yet.

## The loop

Edit the PBIR and TMDL files in the sandbox, `validate_dashboard`, publish the model then the report to the Agent Sandbox
workspace, render the published report, look at the screenshot, fix, then open a pull request. A human
reviews the PR and promotes to production by hand.

- **Publishing is not gated.** The only gate is the workspace the agent can reach: its identity is a
  Member of the Agent Sandbox workspace and nothing else.
- **Render the published report, never local files.** The Microsoft CLI's `preview` renders local
  files only; it was kept out of the design because a local render still needs a new measure
  published first (`Missing_References`), cannot apply bookmarks or slicers, and shows the edit
  instead of what is live.

## Proven (2026-11-18 and 2026-11-19, Power BI Pro, no capacity)

- PBIR is editable by an agent; `validate` (the report CLI, `@microsoft/powerbi-report-authoring-cli`
  0.5.0) needs no identity, but it fetches the JSON schemas over HTTPS from `developer.microsoft.com` and
  `schemas.microsoft.com` (in-memory cache only), and returns `schema-missing` when it cannot. It caught
  six dangling bookmark references after a deleted visual, so a deleting edit must be scrubbed or done in
  place.
- **`validate` in the sandbox cannot check JSON schemas (probe, 2026-11-20).** Both schema hosts reset the connection
  from inside the sandbox (LangSmith's organization policy), so `validate` there skips schema checks and emits
  `PBIR_SCHEMA_UNREACHABLE` warnings. A visual whose width was set to a string went undetected in the sandbox and was
  caught (`PBIR_SCHEMA_VALIDATION_ERROR`) on a machine with network, while the CLI's own checks (a bookmark pointing at a
  deleted visual, textbox heights, unknown visual types) ran in both. Even with network, one visual's schema
  (`visualContainer/2.14.0`) is not on the mirror and is skipped. `doctor` passes in the sandbox and does not detect this.
- **The coding agent's identity can run DAX against the Agent Sandbox model (2026-11-20).** `executeQueries` accepted a plain
  `EVALUATE ROW(...)`, an existing measure (`[Champion Precision]` returned 0.8536), and a query-scoped
  `DEFINE MEASURE ... EVALUATE ...` that is not in the model. So a draft measure can be tested before it is added to the TMDL,
  and any published column or measure can be queried straight after a publish.
- **The DAX `INFO.VIEW.*` metadata functions work for the coding agent's identity (2026-11-20).** Against the Agent Sandbox
  model they returned 28 tables, 112 columns, 76 measures and 9 relationships, and `INFO.VIEW.MEASURES()` carries each
  measure's table, description, format string and hidden flag. Its `Expression` column is null, so a measure's DAX
  still comes from the TMDL file. The non-`VIEW` `INFO.*` functions may need more permission and are untested.
- A service principal can create and update a **semantic model** (about 14 s to create) and a
  **report** (about 2 s) through the Fabric item APIs. An update replaces the whole definition, so the
  tool always sends every file. The report's `definition.pbir` must be `byConnection` to the Agent Sandbox model.
- The Desktop-published Agent Sandbox report (a Desktop publish of the repo's `.pbip`) is stored as **PBIR** (248
  parts, no `report.json`), and `updateDefinition` with the repo's files, `definition.pbir` rewritten to a
  connection to the Agent Sandbox model in the payload only, returned 202 and `Succeeded` and left the report
  unchanged in the browser (2026-11-19).
- An in-place model update **keeps the Import data**. A model created through the API has no data.
- A service principal can mint its own embed token (`GenerateToken`, datasets, reports and
  `targetWorkspaces` in one call) only as a **Member or Admin**; Contributor fails with 403, though a
  role change can take about 40 minutes to show. Tokens are reusable until they expire.
- The published report embeds as `type: "report"` with that token and renders headless in Chromium.
  A Report object has `bookmarksManager` (**bookmarks apply**: "Barplot" switched the right chart to
  the SHAP bars), and slicers can be read (`getSlicerState`) and set (`setSlicerState`). Setting the
  Model and Data Split slicers and the Evaluation Metric field parameter (value
  `'_Model_Evaluation_Measures'[NDCG]`) changed the cards and charts as expected. A create-mode embed
  has neither bookmarks nor slicers.
- With a 5-second wait before the screenshot, renders were crisp and matched the browser.
- A new measure renders only after the model is published; an unpublished one fails
  `Missing_References`.
- The sandbox cannot reach Power BI hosts (connection resets); it reaches npm, apt and GitHub. Debian's
  `chromium` works as Playwright's `chrome` channel there. So the render runs in the coding agent app,
  not the sandbox, and the sandbox never holds a token.

## Where each part runs

| Part | Runs in | Holds |
|---|---|---|
| Edit PBIR/TMDL, the CLI's offline lookups, git, the PR | Sandbox | No credentials |
| `validate_dashboard`, `publish_dashboard`, `render_dashboard`, `run_dax_query` (coding agent) | Coding agent app (open egress) | The coding agent's Power BI secret (validate needs none) |
| The render function | A shared module, imported by the coding agent app now and the gateway later | Nothing; callers pass an identity |
| `get_page_info`, extended with the render (main agent, later) | Gateway | The production render identity's secret |

## The shared render tool

One function, written as a tool from the start so it can move behind an MCP boundary later without
changing its interface.

- **Input:** the target report (resolved from the caller's config, never from the model), `page`,
  `bookmark` (name), `slicers` (a slicer's title and the values to select), `scale`.
- **Behavior:** mint or reuse an embed token for the report and its dataset, fetch the report's
  `embedUrl`, embed it with the filter and page-navigation panes hidden and `pageName` set, wait for
  `rendered`, apply the bookmark and slicer states, wait a settle delay, screenshot, and crop the grey
  "free trial version" strip the embed adds at the top (inside the embed, so it cannot be hidden).
- **Output:** the PNG, plus the applied state and the available bookmark and slicer names.
- **Errors the agent can act on:** an unknown bookmark or slicer lists the real ones;
  `Missing_References` says to publish first if a measure or column was added, otherwise check the
  spelling in the TMDL; a 401 or 403 says the identity or the embed allowance is the problem, and the
  agent reports that it could not render instead of retrying.
- **Which workspace a caller can reach is credentials and config**, not a tool argument.
- **Numbers.** A screenshot is visual grounding only and sits outside the verification contract. The
  tool's description tells the model never to quote numbers from it; a number read from an image that no
  approved tool returned fails verification, as it does today.
- **One tool for the main agent: `get_page_info` gains the render.** The info pages' text (formulas, how
  to navigate) lives in scrollable HTML visuals that a screenshot cannot fully capture, and the main agent
  keeps its tool count low. So the render module returns an image only, and `get_page_info` stays the
  single main-agent tool: it still returns the page text, and takes optional `bookmark` and `slicers`
  inputs that make its screenshot a live render of that state instead of the static reference image
  (the static image stays as the fallback). The coding agent gets a separate `render_dashboard` in its
  own app, images only: the visuals' real text is in the report files it edits. New optional parameters
  add to the tool's schema, and all strict schemas share one compiled-grammar size limit, so check the
  size when adding them.

The page's slicers (read 2026-11-19): Evaluation Metric (a field parameter), Pie Chart (a field
parameter), Model (`model_name`) and Data Split (`dataset_split`), all basic `In` filters. Bookmarks:
Beeswarm, Barplot, and open and close pairs for the pie chart, donut chart, assumptions and table.

## Identities and guards

- **Coding agent identity:** a new service principal, a Member of the Agent Sandbox workspace only, not the
  analytics agent's `power-bi-sp-*` (which can query production). Its secret lives in Secret Manager and
  reaches only the coding agent app. The analytics service principal is currently a Member of production,
  kept for the main agent's render later.
- **Patch rules** (`coding_agent_app/patch.py`): narrow `dashboard/*` in `BLOCKED_PATTERNS` and keep the `.pbix` and
  `.pbi/` blocked. A new table file under the model's `definition/tables/` is allowed (decided 2026-11-20); measures
  live inside table files, so a path block there would stop them too.
- **One `validate`, in the coding agent app (decided 2026-11-20).** `validate_dashboard` pulls the report folder out of
  the sandbox (about 0.2 MB compressed) and runs the CLI where the schema hosts are reachable, so a result always
  includes the schema checks (about 2 s for the CLI itself on a machine with network; the sandbox-to-app hop is not yet
  measured). `publish_dashboard` runs the same check and refuses on any error. The CLI's own `validate` stays in the
  sandbox but is not used: it passes silently when it cannot fetch schemas, and the skill says to call the tool instead.
  A cap on publishes per task.
- **Prompt and skill guidance:** when adding a visual, find free space from every visual's absolute
  box (a visual inside a group is positioned relative to the group, and groups nest; the page has almost
  no free space); a cloned visual keeps its container padding, so check it against the new size;
  deleting a visual means scrubbing its references from bookmarks and the page's interactions.
- **New data is staged, two tasks, owned by the main agent (decided 2026-11-20).** A new table must be sourced from the
  Instacart ML BigQuery project and aggregated in SQLX first. If the table does not exist yet or needs aggregating, the
  main agent submits a plan for the SQLX change only; the coding agent does that and stops. The user builds the tables
  (Dataform), then the main agent plans the dashboard change as a second task, where the coding agent adds the table to
  the model (copying an existing BigQuery table's partition block) and the visuals. The skill carries the same rule as a
  backstop. A promoted model change needs a schema rebuild and a gateway redeploy.

## The repo stays openable in Desktop

A user must be able to open the repo's `.pbip` file in Power BI Desktop at any time, including on an
agent's branch. So the agent's edits keep every file Desktop-valid, and the publish tool never changes
the repo's files: it rewrites `definition.pbir` from a relative path to a connection to the Agent Sandbox model
**in the payload it sends**, leaving the committed file as it is. `validate` checks the schemas but not
Desktop itself, so opening the branch in Desktop is part of the human review before the PR is merged.

## One-time setup (human)

Publish the model and report to the Agent Sandbox workspace from Desktop, and set the BigQuery credentials on the
model's data source there. Everything after that is the agent's.

## Unverified, confirm before building

- **DAX user-defined functions in the model's files (found 2026-11-20).** Microsoft Learn says they are generally available but need
  `compatibilityLevel` 1702 or higher; the model is at 1606, so the first function needs that raised in `database.tmdl`. Two things
  to confirm with a throwaway publish to Agent Sandbox: that the service accepts the raised level, and the declaration form in
  `functions.tmdl` (Learn shows `createOrReplace` around it, which is how a TMDL script is written; the skill's example omits it, as
  other TMDL files do). The plugin's own `tmdl-guidelines.md` shows `function DoubleValue = DoubleValue(Value) = Value * 2`, which
  contradicts Learn's `function Name = (params) => body`, so the build script replaces that section with Learn's syntax, naming
  rules and limits, then the plugin's type system and best practices.
- **The ML repo's `.pbip` is behind the analytics agent's dashboard (decided 2026-11-20).** The repo copy has no
  descriptions (zero `///` lines) while all 67 measures in the live model have one, so publishing the old files would
  drop them. The ML repo's `.pbip` is replaced by the analytics agent's dashboard, Agent Sandbox is republished from
  it, and the ML repo's README notes the change; the `.pbix` stays the original ML dashboard and is excluded from both
  agents. Every new table, column and measure gets a `///` description. Check after the upload that the page names
  match `get_page_info`'s (see the next item).
- **The updated dashboard's `validate` baseline (2026-11-20).** It had 6 errors, all `PBIR_BOOKMARK_VISUAL_REF_MISSING`:
  the Open Agent and Close Agent bookmarks (both pages) still named three visuals that no longer exist in their
  `options.targetVisualNames`. They were removed from the working copy (uncommitted until committed to `dev`), which now
  reports 0 errors and 10 warnings: 6 textbox heights under the 34 px floor, 2 Power Apps custom visuals the CLI does not
  know, a scatter chart with no grouping, and 1 unreachable schema. The loop blocks on errors and passes warnings through.
- **Dashboard-specific fields in the coding plan: decide when we reach them.** The main agent's turns are short (about two
  minutes), so its planning stays high level and the coding agent works out the details. Still, a few structured prompts in
  the plan would force it to think through a dashboard change: candidates are the number of pages, charts per page,
  formatting to match, and an optional `dashboard_pages` field that also lets the app pin the dashboard skills.
- **The numeric sensitivity sliders** (what-if parameters on the Financial Impact page). They feed the
  assumptions panel (the "Open Assumptions" bookmark), not the profit charts. Setting Churn Rate to 0.4
  and Conversion Rate to 0.07 changed the slider textboxes (0.40, 0.070) and moved the knobs, but whether
  the values reach the panel's derived numbers is untested: render with the bookmark applied and the sliders
  set, and compare the panel with the unset one. The stored values carry float noise (`0.3900000000000002`),
  so a typed value may need to match the table's exact float.
  Report-level filters (`setFilters`) are also untested.
- **Chromium inside the coding agent app on Cloud Run.** Playwright's default flags already include
  `--no-sandbox` and `--disable-dev-shm-usage`; confirm with one real render, and size memory (2 GiB is
  a config change; the app runs at 1 GiB today). Every picture so far was rendered on Windows; fonts on Debian may differ, so compare the first Cloud Run render with a Windows one.
- **The wait before the screenshot (2026-11-20).** The fixed 5 seconds was a guess made early in the experiment and never
  tuned. The vendored `powerbi-client` 2.25.0 has events for `loaded`, `rendered`, `renderingStarted`, `visualRendered`,
  `bookmarkApplied`, `filtersApplied` and `pageChanged`. Plan: register a one-time `rendered` listener before each change
  (page, bookmark, slicer), apply it, and wait for that next `rendered` with a short timeout for a change that triggers no
  render; then a short pixel-stability check (two screenshots a few hundred ms apart until they match) for fade-in. The
  `rendered` event firing after changes, and how long the first render waits on the 90 visuals' queries, are unmeasured:
  log each phase's time in the Layer 2 notebook and keep the shortest wait whose picture matches a long-wait reference.
- **The embed-token allowance.** The free allowance is limited and documented as "development testing
  only". Accepted for a portfolio project; a real deployment would buy a capacity.
- **Refresh for new data-bearing objects** (about 8 a day on shared capacity; not tested). A new table has no data in
  Agent Sandbox until its model is refreshed; step 6c plans the tool, after two unknowns are tested.
- **`ml_model_results` is not in `CODING_AGENT_DATASETS`.** The dashboard's tables live there, so the coding agent's
  `list_tables` and `get_table_schema` cannot confirm a table exists before it adds one. Decided to settle it in step 6d.
- **Field-parameter tables** have no BigQuery source (they are calculated); the new-table rule does not cover them.
- **A new page's description.** The text is an HTML measure (for example `HTML_Model_Evaluation_Info`) inside the
  model's measures table file, shown on the page's Info page. The main agent reads it with `get_repo_contents`
  from the coding task's branch (`alternate_branch`), or from `dev` once merged; those table files are 20 to 30
  thousand characters, under the tool's 300,000-character cap. `get_page_info`'s hand-kept text in
  `context/page_info/` covers only the pages that exist today. Also: `get_page_info` calls the page "Model
  Performance" while the report's page is "Model Evaluation"; check which name production uses before step 11.
- Whether Contributor is enough for the item APIs (Microsoft's docs say it is; Member was used).
- **Leaning yes (2026-11-19): the main agent also renders Agent Sandbox**, so it can show a user what the coding
  agent changed and how it looks. Its identity (the analytics agent's `power-bi-sp-*`) stays a Member of both
  production and Agent Sandbox; embed tokens need Member, and there is no read-only role that can mint one, so the
  limit is code: the gateway has no publish path, and `get_page_info` only returns images. Agent Sandbox holds a
  copy of the same data, so no new data becomes reachable. Decide the optional `environment` input on
  `get_page_info` at step 11.

## Build plan

One step at a time, checked in after each; each ships with Layer 1 tests where something is testable.

1. **Decisions and setup (done 2026-11-19).** The shared module is `app/powerbi_render.py`, a plain function
   module that the coding agent's `render_dashboard` and the main agent's `get_page_info` each call from
   their own tool. The render is our own Python Playwright function with Microsoft's `powerbi-client`
   JavaScript library, not the patched report authoring CLI (eight separate patches by the end of the
   experiment, fragile to upstream versions, no filter API). Both new dependencies are approved. The
   library is a committed, unmodified copy in `app/powerbi_client_js/` (version 2.25.0, MIT, the file we
   tested, `powerbi.min.js` sha256 `30c97c2a4c02b857e76711369344cb9a7efdb39ed1d66d819b7289dbc8f8c5fa`; to update, copy the new
   `dist/powerbi.min.js` and `LICENSE.txt` from the npm package). The module's target is an enum (`production` or
   `agent_sandbox`) mapped to workspace and report IDs in config, never an ID the model passes. The coding
   agent's Power BI service principal is created and verified (a Member of Agent Sandbox only, setup guide
   Step 24). The Agent Sandbox workspace holds the Agent Sandbox model (`71ea68dc`) and the Agent Sandbox
   report (`b84e7228-9955-4f40-87df-b8fc3dac692d`, "Instacart ML Model Evaluation"), both published from
   Desktop from the repo's `.pbip` project; `updateDefinition` on the report is proven. The throwaway
   `agent-test-report` and `agent-test-model` are deleted. The coding agent's image copies only named files
   from `app/`, so step 4 adds `COPY` lines for the new module and the library folder.
2. **Render core.** `RenderRequest` and `RenderResult` models, the token mint with a cache, the embed
   URL lookup, embed, bookmark, an event-driven wait (not a fixed delay), screenshot and crop, and the actionable errors. Layer 1: request
   validation, token expiry, crop, error mapping against a fake page. Layer 2: a notebook against the
   Agent Sandbox report.
3. **Slicers and filters.** Resolve a slicer by title, build its state from the current state, test the
   field-parameter slicers. Layer 1: the state builder. Layer 2: live.
3b. **Report outline.** A second function in the module, `describe_report(target)`: the published report's
   pages, bookmarks and slicers (title and field) parsed from its definition (Fabric `getDefinition`, no
   browser), cached about 5 minutes per target. The main agent learns what exists from this and never renders
   just to find out. Layer 1: the parsing, against a trimmed saved definition. Layer 2: live. It needs no
   Chromium, so it can be built before step 4.
4. **Chromium in the coding agent app.** The image, memory, and one real render on Cloud Run; measure
   latency, memory and the settle delay.
5. **`validate_dashboard` and `publish_dashboard`.** Both read the report files out of the sandbox with one shared
   function (one archive, one read). `validate_dashboard` runs the CLI in the app and returns its diagnostics, with
   `PBIR_SCHEMA_UNREACHABLE` treated as a failed check, not a warning. `publish_dashboard` runs the same validation,
   refuses on any error, rewrites `definition.pbir` to `byConnection`, calls `updateDefinition` for the model then the
   report, polls the operation, and caps publishes per task. The app image gets Node 24 and the CLI (Debian's Node is
   too old: the CLI needs 20 or newer), copied in from a multi-stage build; this is the first step that needs it.
   Layer 1: the body builder (every file, no `.platform` or `.pbi`), the rewrite, the cap, and the diagnostics handling
   against a saved CLI output.
6. **`render_dashboard` for the coding agent.** The tool wrapper that returns the image to the agent,
   with the `Missing_References` guidance.
6b. **`run_dax_query` for the coding agent.** A tool in the app that runs one `EVALUATE` (or a query-scoped `DEFINE MEASURE`
   plus `EVALUATE`) against the Agent Sandbox model with `executeQueries`, as the coding agent's identity. The target is
   config, never an argument; a row cap and a timeout like the main agent's tool. It cannot reach production. The main agent
   never repeats a number from a coding task's report as data (existing rule). It is not the main agent's MCP tool (that one
   is wired to the analytics agent's identity and the production model); the request logic is a small separate copy in the
   app, or a shared function if sharing does not touch the verified tool.
6c. **`refresh_dashboard_model` for the coding agent (later).** Triggers a refresh of the Agent Sandbox dataset through
   the Power BI refresh REST endpoint as the coding agent's identity, polls to Completed or a failure reason, with a
   timeout and a per-task cap (about 8 refreshes a day on shared capacity). The target is config, never an argument.
   Needed only after a publish that adds or changes data-bearing objects (a new table or column); measures and
   visuals do not need it. Test first, with a throwaway table: (1) whether a new table on the same BigQuery connector
   reuses the dataset's stored credentials, or the refresh fails until they are set in the service; (2) whether
   `updateDefinition` with a new table triggers a refresh or clears the existing data. Compare the table's row count
   before and after. **When this is built, update step 3 of "Add or change a table or column" in
   `coding_agent_app/skills/semantic-model/SKILL.md`**: replace the no-tool fallback with a call to this tool, and add
   the trigger to this tool's description. Also add the refresh to step 7 of the report skill's `authoring.md`
   Quick Start Workflow (after publish, before render).
6d. **Dataset access for dashboard tasks (decide).** `CODING_AGENT_DATASETS` (`instacart`, `building_blocks`,
   `ml_model_feature_table`) does not include `ml_model_results`, where the dashboard's tables live, so a dashboard task cannot
   check that a table it is told to bring in exists. Either give the coding agent `ml_model_results`, or a new dataset for
   dashboard-bound tables, with guidance on when to use which. Needed before the code-plan-then-dashboard-plan flow.
7. **The sandbox snapshot and skill.** The report CLI for its offline lookups (`catalog`, `formatting`, `expr`,
   `theme`); the Microsoft report skill (MIT), built from its own structure by `scripts/build_pbi_skills.py`: a `SKILL.md` dispatcher
   with three modes (planning, design, authoring; Microsoft's management mode is dropped because publishing is a step in the
   authoring loop), each a file in `references/`, and the topic references the authoring mode points to.
8. **Patch rules.** The narrowed `dashboard/*`, `.pbix` and `.pbi/`.
9. **Prompts and the plan.** Dashboard tasks in the coding agent's preamble, including what the final report says for a
   dashboard change (what changed, which pages it rendered); the skills hold no reporting rules. The main agent's planning guidance holds
   the staging rule: SQLX plan first, dashboard plan after the user builds the tables. A new page follows the existing pattern: an Info page with an HTML content visual
   driven by an `HTML_<page>_Info` measure. Describe the dashboard's entry-point files (the report's
   `pages.json` and `bookmarks.json`, the model's measure and table files) in
   `context/file_descriptions/github_file_descriptions.json`, so the main agent finds them when it plans a
   dashboard change with `get_repo_contents`.
9b. **Main agent guidance, taken from Microsoft's references (do not lose).** Approval of a dashboard plan happens at the main
    agent, so the planning spec belongs there too: when the main agent's dashboard planning is built (its `save_coding_plan`
    description or prompt), take what a plan should specify from Microsoft's `planning.md` and `planning-part-02.md` (in the
    `powerbi-authoring` plugin, version 0.3.22, under `skills/powerbi-report-cli/references/`; not copied into the repo) and the
    report skill's `design.md`, `layout.md` and `chart-selection.md`. Also take from the semantic-model skill's `dax-guidelines.md` (and
    `metadata-discovery.md`) for the main agent's MCP DAX query tool description or prompt. `planning.md` and
    `planning-part-02.md` are not in the coding agent's skill: their interview and approval gate are the main agent's job,
    and the coding agent receives the approved plan. The report skill's `design.md` tells the coding agent that a vague
    request (audience, purpose, page count or desired filters/slicers missing) ends with a blocking question, so the main agent's
    plan must supply those four. The plan's dashboard section (draft, user's template): cards at the top of the page; charts
    listed in priority order (top left is highest); page slicers above the cards; colors and style default to matching the
    existing dashboard; field parameters; bookmarks; buttons. The main agent's guidance sets placement defaults for these.
9c. **Dashboard reviewer (later).** A second reviewer rubric, selected by the plan's kind, for the judgment criteria the code
    cannot check (a draft is in `docs/dashboard-planning-draft.md`, section 5), and possibly the final render of each changed page
    as reviewer input. The path, validate and render-after-last-edit checks are deterministic code, not reviewer criteria.
9d. **Page info for the coding agent (idea, not designed).** `get_page_info`'s per-page text lives in display measures in the
    model, so the coding agent can already read it; it also goes stale when the coding agent changes a page. To think through:
    whether the agent should be told those measures exist and keep them current after a page change (a model edit that also
    shows on the dashboard, with no numbers in it); that the main agent only sees the change after `build_model_context.py` runs
    and the gateway is redeployed; a new page needs a new measure and an entry in `PAGE_INFO_MEASURES`; the reference
    screenshots go stale the same way (the shared render tool, step 11, is their fix).
10. **End to end.** A real dashboard task from the main agent's chat to a PR, with before and after
    screenshots (where they are stored is open).
11. **Main agent.** Extend `get_page_info` (no new tool). `page_name` becomes a plain string instead of a
    `Literal` built at startup; omitted, it returns the outline from step 3b as text, with no render; named,
    it behaves as today, plus optional `bookmark` and `slicers` that make the screenshot a live render, with
    `filter_context` mapped to slicers. Set `handle_tool_error`. Also: the gateway image, memory and a render
    concurrency cap; the schema-size check; telemetry.
12. **Later.** Regenerate `get_page_info`'s reference screenshots with the tool, as a scheduled or
    publish-triggered job.
13. **Later, phase 2.** The coding agent builds a new dashboard, not only edits one: the CLI's `scaffold` command (offline
    mode, since the sandbox cannot reach the schema mirror), `publish_dashboard` creating a new report item instead of
    updating one, and all of Microsoft's design and planning references (in the report skill from the start and read
    on demand, so they cost nothing until used). Also web search for the coding agent, outside the sandbox: a search tool in the coding agent app, or the
    model provider's server-side web search.
