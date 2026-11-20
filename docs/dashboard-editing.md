# Dashboard editing and the shared page-render tool

The coding agent edits the Power BI dashboard's PBIP files, publishes to the Agent Sandbox workspace, and looks at
the result through a headless screenshot of the **published** report. The render step is one shared
tool: the coding agent uses it on the Agent Sandbox workspace, and the main agent can use it on production for
visual grounding. Decided 2026-11-19 after an experiment (proven items below); nothing here is built yet.

## The loop

Edit the PBIR and TMDL files in the sandbox, `validate`, publish the model then the report to the Agent Sandbox
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
  0.5.0) runs offline with no identity. It caught six dangling bookmark references after a deleted
  visual, so a deleting edit must be scrubbed or done in place.
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
| Edit PBIR/TMDL, `validate`, git, the PR | Sandbox | No credentials |
| `publish_dashboard`, `render_dashboard` (coding agent) | Coding agent app (open egress) | The coding agent's Power BI secret |
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
- **Patch rules** (`coding_agent_app/patch.py`): reject a patch that adds a new file under the model's
  `definition/tables/` (a new table is a blocking question: "this needs a BigQuery table; plan a SQLX
  change first?"). A plain blocked path would also block adding measures, which live inside the table
  files. Narrow `dashboard/*` in `BLOCKED_PATTERNS` and keep the `.pbix` and `.pbi/` blocked.
- **`publish_dashboard` runs `validate` in the sandbox first** and refuses on errors, so the check is
  not left to the prompt. A cap on publishes per task.
- **Prompt and skill guidance:** when adding a visual, find free space from every visual's absolute
  box (a visual inside a group is positioned relative to the group, and groups nest; the page has almost
  no free space); a cloned visual keeps its container padding, so check it against the new size;
  deleting a visual means scrubbing its references from bookmarks and the page's interactions.
- New data stays human-driven: the agent adds the SQLX, the user builds the table and brings it into the
  model in Desktop. A promoted model change needs a schema rebuild and a gateway redeploy.

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

- **The numeric sensitivity sliders** (what-if parameters on the Financial Impact page). They feed the
  assumptions panel (the "Open Assumptions" bookmark), not the profit charts. Setting Churn Rate to 0.4
  and Conversion Rate to 0.07 changed the slider textboxes (0.40, 0.070) and moved the knobs, but whether
  the values reach the panel's derived numbers is untested: render with the bookmark applied and the sliders
  set, and compare the panel with the unset one. The stored values carry float noise (`0.3900000000000002`),
  so a typed value may need to match the table's exact float.
  Report-level filters (`setFilters`) are also untested.
- **Chromium inside the coding agent app on Cloud Run.** Playwright's default flags already include
  `--no-sandbox` and `--disable-dev-shm-usage`; confirm with one real render, and size memory (2 GiB is
  a config change; the app runs at 1 GiB today).
- **How short the settle delay can be.** 5 seconds worked; it has not been rendered without one since.
- **The embed-token allowance.** The free allowance is limited and documented as "development testing
  only". Accepted for a portfolio project; a real deployment would buy a capacity.
- **Refresh for new data-bearing objects** (about 8 a day on shared capacity; not tested).
- Whether Contributor is enough for the item APIs (Microsoft's docs say it is; Member was used).
- **Open item: should the main agent also render Agent Sandbox?** It could show a user a pending dashboard change
  in chat. The identity would be a Member of both workspaces (embed tokens need Member; there is no
  read-only role that can mint one), so the limit is code: the gateway has no publish path and the tool
  only returns images. Decide at step 11, with an optional `environment` input on `get_page_info`.

## Build plan

One step at a time, checked in after each; each ships with Layer 1 tests where something is testable.

1. **Decisions and setup.** Decided 2026-11-19: the shared module is `app/powerbi_render.py`, a plain
   function module that the coding agent's `render_dashboard` and the main agent's `get_page_info` each
   call from their own tool; the render is our own Python Playwright function with the `powerbi-client`
   script from npm (vendored), not the patched Microsoft CLI (eight separate patches by the end of the
   experiment, fragile to upstream versions, no filter API). Both new dependencies are approved.
   The module's target is an enum (`production` or `agent_sandbox`) mapped to workspace and report IDs in
   config, never an ID the model passes. Still to do: where the vendored script lives, and the coding
   agent's Power BI service principal and its secrets (a Member of Agent Sandbox only; created and verified
   2026-11-19, setup guide Step 24). The Agent Sandbox workspace holds the Agent Sandbox model (`71ea68dc`) and the
   Agent Sandbox report (`b84e7228-9955-4f40-87df-b8fc3dac692d`, named "Instacart ML Model Evaluation"),
   both published from Desktop from the repo's `.pbip` project; `updateDefinition` on the report is proven.
   `agent-test-report` and `agent-test-model`, created through the Fabric API during the experiment, are
   throwaway and will be deleted. No code.
2. **Render core.** `RenderRequest` and `RenderResult` models, the token mint with a cache, the embed
   URL lookup, embed, bookmark, screenshot and crop, and the actionable errors. Layer 1: request
   validation, token expiry, crop, error mapping against a fake page. Layer 2: a notebook against the
   Agent Sandbox report.
3. **Slicers and filters.** Resolve a slicer by title, build its state from the current state, test the
   field-parameter slicers. Layer 1: the state builder. Layer 2: live.
4. **Chromium in the coding agent app.** The image, memory, and one real render on Cloud Run; measure
   latency, memory and the settle delay.
5. **`publish_dashboard`.** Validate in the sandbox, read the files out, rewrite `definition.pbir` to
   `byConnection`, `updateDefinition` for the model then the report, poll the operation, cap per task.
   Layer 1: the body builder (every file, no `.platform` or `.pbi`), the rewrite, the cap.
6. **`render_dashboard` for the coding agent.** The tool wrapper that returns the image to the agent,
   with the `Missing_References` guidance.
7. **The sandbox snapshot and skill.** The report CLI for `validate`; the Microsoft report skill (MIT)
   plus our guidance above.
8. **Patch rules.** The new-file rule for model tables, the narrowed `dashboard/*`, `.pbix` and `.pbi/`.
9. **Prompts and the plan.** Dashboard tasks in the coding agent's preamble and the blocking-question
   path for new tables.
10. **End to end.** A real dashboard task from the main agent's chat to a PR, with before and after
    screenshots (where they are stored is open).
11. **Main agent.** Extend `get_page_info` (no new tool) with optional `bookmark` and `slicers`, rendering
    production live, with `filter_context` mapped to slicers; the gateway image, memory and a render
    concurrency cap; the schema-size check; telemetry.
12. **Later.** Regenerate `get_page_info`'s reference screenshots with the tool, as a scheduled or
    publish-triggered job.
