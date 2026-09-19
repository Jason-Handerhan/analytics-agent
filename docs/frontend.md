# Power Apps Frontend

> Controls, what each turn sends, and how a response renders. **Phase 1** for
> the basic app; the approval card and cancel button land with their features.
>
> The request contract itself is in `.claude/rules/gateway.md`; the connector
> that carries it is in `docs/auth.md`.

**License: no Power Apps Premium purchase needed. Confirmed empirically
(2026-09-13) — the free Power Apps Developer Plan fully supports custom
connectors, creation and live use both.** Power Apps classifies *every*
custom connector as Premium regardless of what it wraps, even a connector
calling a plain REST endpoint — but the Developer Plan's feature table
already listed "Use premium connectors" and "Create custom connectors" as
included, and a real test proved it: created a connector from blank
(no-auth GET) inside a Dataverse solution (path: **Solutions** → **+ New**
→ **Automation** → **Custom connector** — not the plain Connections page,
which doesn't surface it), and **Test operation** against a real endpoint
(`api.github.com/zen`) returned a genuine `200`, with Microsoft's own
`x-ms-environment-id`/`x-ms-tenant-id`/`x-ms-dlp-re` headers on the response
confirming the call actually transited Power Platform's connector gateway —
no licensing or DLP prompt anywhere in the flow.

**One caveat, not yet tested and worth confirming when Phase 1 builds the
real connector:** this proved a **No authentication** connector. The real
connector needs **OAuth 2.0** (delegated) plus a static `x-api-key` header —
a different security configuration, untested here. No specific reason to
expect the auth type itself to be Premium-gated when a no-auth connector
wasn't, but don't assume; confirm it when it's actually built.

**Second caveat, a licensing-terms question rather than a technical one:**
Microsoft's Developer Plan FAQ frames the environment as for "development
and test," with a paid plan required for genuine "production" use — a
distinction about intent/terms, not something enforced by the environment
itself as far as this test showed. Worth being aware of for how this project
is described (a portfolio/demo build run inside the developer environment,
not a commercial product with paying customers), rather than a blocker to
building anything.

## Layout & controls

**App type:** Canvas app — needed for layout control alongside an embedded
report.

- **Power BI report embedding — via the `Power BI tile` control's `TileUrl`
  property override, not the standard Workspace/Dashboard/Tile dropdowns and
  not an `HTML text` control.** Confirmed empirically (2026-09-13):
  - **The Workspace/Dashboard/Tile dropdown path only works for actual
    Dashboard objects** (Power BI's pinned-tile-board artifact type) — it
    can't target a Report (the interactive, multi-page artifact people
    usually mean by "dashboard"). If the dropdown's Dashboard list is empty,
    that's why — check the content's actual type in Power BI first.
  - **An `HTML text` control with a raw `<iframe>` does not work** — tested
    directly (a plain `<iframe src="https://example.com">` also rendered
    blank), confirming the control doesn't render iframes at all, not a
    Power-BI-specific auth failure. Don't reach for this approach again.
  - **The fix: drop a `Power BI tile` control, but don't touch its
    Workspace/Dashboard/Tile properties.** In the formula bar's property
    dropdown (top-left, not the right-hand pane), select **`TileUrl`** and
    set it directly to the report's secure embed link — from the report in
    Power BI Service: **File → Embed report → Website or portal**, copy
    the plain link (**not** the `<iframe>` HTML, and never "Publish to
    web," which is unauthenticated and would make the data public). This
    renders the full interactive report through a native control, still
    respecting RLS and view permissions, with no PCF custom component
    needed.
- **`TextInput` + send `Button` + `Gallery`**, the Gallery bound to a
  `colChat` collection.
- **Custom connector call on send — positional arguments, not a record:**
  `AnalyticsAgent.PostAsk(question, conversation_id)` — confirmed empirically
  (2026-09-18): a flat request-body schema like `AskRequest`'s gets flattened
  into separate positional parameters by Power Apps' connector generation,
  not exposed as one inline record. Optional fields (`image_base64`,
  `filter_context`, `active_page`) can be omitted entirely.

**Both roles render through an HTML text control — not the Label-for-user
split originally planned here.** A plain Label can't support the
scrollable-long-message fix below, and user questions turned out not to be
reliably short (confirmed against real usage, not assumed). The
markup-injection risk a Label would have sidestepped is instead closed by
**manually HTML-escaping user text before rendering**:
`&` → `&amp;` first, then `<` → `&lt;`, then `>` → `&gt;` — that order
matters, since escaping `<`/`>` before `&` would double-escape the entities
just created.

```
colChat record:  { role: "user" | "agent",
                   body: <escaped plain text for user, HTML for agent>,
                   chart_url: <optional, agent only> }
```

**Gallery rows are fixed-height (`TemplateSize`), not auto-growing.** Both
message types wrap their content in a `max-height` + `overflow-y: auto` div
in the `HtmlText` formula rather than relying on the row itself growing —
Power Apps galleries don't natively support per-row auto-height.

**`TextInput` uses `TextMode.MultiLine` for word-wrap, with typed newlines
flattened to spaces before use.** Power Apps ties visual wrapping and
Enter-inserts-a-newline together in one property — there's no wrap-only
mode. Stripping `Char(13)`/`Char(10)` to spaces before storing or sending
decouples them: the box wraps visually, but a manually-typed line break
never becomes semantically meaningful data.

**Store the gateway's HTML, not `answer_markdown`.** Power Apps can't convert
Markdown, so the conversion happens once in the gateway and the client holds
what it renders. Re-deriving it client-side isn't an option.

## Conversation state

**`colChat` and the server's history are two different things.** `colChat` is
the *visible transcript* — everything the user has scrolled through this
session, client-side only. The model sees `sessions.recent_messages`, capped
at the **last 5 turns** (`.claude/rules/gateway.md`). So a user can be looking
at twenty messages while the model is working from five. Don't send `colChat`
to the server; it isn't the history, and the server already has its own.

- **Conversation lifecycle — the ID comes from the server, at app start.**
  `App.OnStart` calls `POST /conversation` and stores the result in
  `varConversationId`; every subsequent request sends it. **A session is the
  app lifecycle** — closing and reopening mints a new ID, so history doesn't
  carry across. That makes the 30-day Firestore TTL pure cleanup, not
  functional session expiry.

  Microsoft's `App` object reference lists *"API calls through a connector"*
  as a valid `OnStart` use, with one condition: *"make sure the call returns
  quickly."* `POST /conversation` does nothing but mint an ID and write an
  empty document — **keep it that way**, since `OnStart` blocks the first
  screen. (Also: `OnStart` variables aren't available in `StartScreen`, so
  `varConversationId` can't feed navigation logic.)

  **Cold starts aren't the risk here** — 2–10s against the connector's 120s
  ceiling, the same ceiling `POST /ask` already lives under. Transient
  failures are: wrap the call in `IfError` per Microsoft's "handle connector
  failures explicitly" guidance, and disable the chat with a visible message
  if `varConversationId` ends up empty, rather than letting the app open in a
  state where sending fails cryptically.

- **New chat button — a real feature that doubles as the recovery path.**
  Calls the same `POST /conversation`, swaps `varConversationId`, and clears
  the visible transcript. **Confirm first when the transcript is non-empty**
  — an accidental click otherwise discards the visible conversation, and with
  no chat-list UI the old history is unreachable even though it still exists
  in Firestore. **No confirmation when empty or after a failed start**:
  nothing to lose, and retry should be one click. The send button is disabled
  whenever `varConversationId` is empty, alongside the in-flight disable
  below.

  **A deliberate step toward multi-chat, not a workaround.** This is the
  generation half; the client-side list and picker are deliberately not built.
  Adding them later means storing IDs and a selector, not restructuring how
  conversations get created.

- **Disable the send button while a turn is in flight** —
  `DisplayMode.Disabled`, driven by the same in-flight variable the thinking
  icon uses. **This is the only thing enforcing one turn per conversation**,
  and it needs to be: everything turn-scoped is keyed on `conversation_id`, so
  two concurrent turns would interleave statuses and overwrite each other's
  pending approval — the user could approve one card and execute the other
  turn's query. No server-side guard; the realistic failure is an impatient
  double-click, which a disabled button stops completely.

## While a turn is in flight

- **Progress indication (both tiers, combined not either/or):** an always-visible thinking icon, plus real staged status — the gateway writes a status string to `live_turns/{conversation_id}` in **Firestore**, polled by a `Timer` via `GET /ask/status/{conversation_id}`
  (`.claude/rules/gateway.md`). Icon is the fallback if the status layer has a gap.

  **Why polling rather than streaming.** Power Apps has no websocket or SSE
  support, so a `Timer` hitting a plain endpoint is the mechanism available —
  and it's what the control is built for. The gateway writes the status
  string as a side effect of running the turn; the mechanism, the status
  vocabulary, and why status lives in Firestore rather than process memory
  are all in `.claude/rules/gateway.md`.

- **User-cancel button (Phase 4 — the endpoint lands in Phase 3):** a `Cancel` button shown alongside the thinking icon, `Visible` exactly while a response is in flight. `OnSelect` fires `POST /ask/cancel/{conversation_id}`
  (`.claude/rules/gateway.md`) — fire-and-forget; the original blocked call resolves on its own shortly after. Complements, doesn't replace, the automatic gateway timeout — gives the user control without changing the underlying guardrail.

- **Streaming vs. request/response:** synchronous request/response, for a documented reason — Power Platform's synchronous-call ceiling is commonly ~120s (unconfirmed exact figure for a direct canvas-app connector call) and isn't extendable by any setting. The real escape valve if ever needed is converting to true async polling, reusing the same status-endpoint plumbing already built for progress indication — not needed now given `max_iterations` + the gateway timeout
  (`.claude/rules/orchestrator.md`) keep turns well under the ceiling.

## Helping the user ask

- **Sample questions on open:** hardcoded buttons, `Visible = CountRows(colChat) = 0`. Zero backend cost, meaningfully improves the demo.

- **Agent-suggested follow-up chips:** bound to `suggested_follow_ups` on the response envelope; system prompt asks for 1–3 short suggestions (e.g. "Would you like to see a graph?"). Natural because `generate_chart` (`.claude/rules/mcp-tools.md`) is a real callable tool, not a promise the agent can't keep.

## Visual grounding — filter context and the optional screenshot

**Design: manual screenshot upload, not automatic capture.** Automatic capture
of an embedded Power BI report was the original plan but was confirmed not
viable (cross-origin/iframe boundary — flagged as a risk before it was hit).
Resolution: a standard `Add picture`/`Attachment` control — simpler to build
than the original plan, not harder.

**What's sent each turn:** the question (text) · an *optional* uploaded
screenshot (base64) · current filter/slicer context (structured JSON, read
directly from Power Apps controls — entirely unaffected by the screenshot
question) · user identity.

**Field-parameter / what-if slider values are part of that same filter
context — not a separate capture path.** The financial-impact page uses
sensitivity sliders to adjust modeling assumptions. Underneath, *both*
Power BI mechanisms that produce them are filters on a synthetic table:
older **what-if parameters** are a disconnected table read via
`SELECTEDVALUE()`, and newer **field parameters** are a calculated table
tagged as a parameter table, where the slicer places a filter on it and
Power BI detects the selection.

**Slicer state needs its own capture call — `getFilters()` does not return
it.** This is the thing most likely to be got wrong, because the half that's
true is convincing: slicer selections *are* expressed as ordinary filter
objects (`IBasicFilter` and friends), so the payload shape stays uniform. But
they're reached through a different method. A capture built only on
`getFilters()` returns **nothing** for the sensitivity sliders — not a wrong
value, an absent one.

Four calls against the embedded report, packed into the request body:

```js
const activePage    = await report.getActivePage();      // -> active_page
const reportFilters = await report.getFilters();         // report-level
const pageFilters   = await activePage.getFilters();     // page-level
const slicers       = await activePage.getSlicers();     // slicer visuals
const slicerStates  = await Promise.all(
  slicers.map(s => s.getSlicerState().catch(() => null))
);
```

- **`getSlicers()` is page-scoped** — slicers on pages the user isn't viewing
  aren't captured. Correct for an agent answering about the current view, but
  a deliberate scope choice, not an oversight.
- **`getSlicerState()` throws `visualConfigIsNotInitialized`** on a visual
  that hasn't rendered yet. The `.catch()` is required, not defensive.
- **`getActivePage()` returns `displayName`** — that's what matches
  `get_page_info`'s page names, not the internal `name`.
- **`active_page` is its own request field, not part of `filter_context`.** A
  page name isn't filterable; filters describe the *data*, the active page
  describes what the user is *looking at*. They feed different tools.

**A missing parameter filter is worse than a missing dimensional one.** A
missing dimensional filter gives the right number for the wrong slice; a
missing parameter filter makes the disconnected table fall back to its
default, so the answer is **a different sensitivity scenario entirely** —
plausible and unsignalled. Passing it through needs no special-casing;
omitting it is the failure.

**This is not the declined-forecasting rule reopening**
(`.claude/rules/orchestrator.md`)**.** That rule
exists because a forecast means the *agent* generating a number with no
live tool call to cite. This is the opposite: a real, pre-built DAX
measure computed by Power BI's engine from a parameter someone already
modeled — as governed and citable as any other `run_dax_query` result.

**Resolved, was an open item:** the concern was that a scope mismatch would
silently drop the sensitivity slider. It's worse than scope — slicer state
isn't in `getFilters()` at all. Now handled by an explicit
`getSlicers()`/`getSlicerState()` pass (`.claude/rules/gateway.md`).

## Response formatting

**Pipeline:** agent produces Markdown → gateway converts to HTML (`mistune`,
custom renderer with **inline styles**, since a `<style>` block isn't reliably
honored by the HTML text control) → Power Apps renders via an **HTML text
control**, not a plain `Label` (which shows raw Markdown literally).

**Beyond plain conversion (cheap, worth it):**
- Structured response envelope (`AgentResponse`, `.claude/rules/orchestrator.md`)
  reused for rendering,
  not a separate schema — `sources` badges (plural; a turn can use more than
  one), a distinct approval card for `needs_approval`, charts as a native
  `Image` control bound to `chart_url`. **`sources` arrives render-ready** —
  ordered, grouped with counts, failed calls excluded. Built in `finalize`
  (`.claude/rules/orchestrator.md`), not the client, so the React rebuild
  doesn't reimplement it.
- **The approval card needs two buttons, not just a display.** `Approve` and
  `Reject` both fire `POST /ask/respond/{conversation_id}` with their
  decision (`docs/approval-workflow.md`), then replace the card with whatever
  `AgentResponse`
  comes back — a real answer on approve, a partial answer on reject.
  Show `estimated_cost` (the dollar string) prominently alongside
  `pending_query`; that's the number the human is actually approving.
- **`cost_cap_exceeded: true` is a third distinct card state** — not an
  approval prompt (there's nothing to approve) and not a normal answer.
  No buttons; just the declined message and `estimated_cost` so the user
  knows how far over it was.
- **`iteration_cap_hit: true` gets the same distinct-card treatment** — a
  visually marked "partial answer" state, not styled like a complete one. The
  prose already says it's partial; the card catches a user skimming.
- **Fenced code inside `answer_markdown` needs the same monospace treatment
  — override mistune's code-block renderer, don't rely on default tags.**
  The agent emits ```` ```python ````/```` ```sql ```` blocks when answering
  code questions. Default `<pre><code>` output hits the exact failure solved
  for `pending_query` below: no inline styles, so no monospace and collapsed
  line breaks. Reuse the same styling — inline `font-family` plus explicit
  `white-space: pre-wrap`.

  Safe to show verbatim: it's this repo's own code, and `CLAUDE.md` forbids
  secrets in code. The answer-length guardrail already bounds it.

- **`pending_query` renders as a monospace code block within the approval
  card, not run through the same prose path as `answer_markdown`.** Verified
  against current Power Apps HTML text control behavior, not assumed:
  - The control has no special handling for `<pre>`/`<code>` — same
    constraint as everything else here, inline styles are what actually
    render, not semantic tags or a `<style>` block.
  - `font-family` set via inline `style=` is confirmed to work — e.g.
    `style="font-family: 'Courier New', monospace; ..."`.
  - **`white-space: pre-wrap` must be set explicitly** — without it, a
    multi-line query's line breaks and indentation collapse into one run-on
    line, defeating the entire point of showing it for review.
  - The control has a **16,384-character total HTML limit.** A single query
    is nowhere near this alone, but worth remembering if a future addition
    ever concatenates several long blocks into one response.
  - **Verify visually once built** — same "confirm, don't assume" status as
    every other HTML-rendering detail already flagged as unverified for this
    control (open items, component reference).
