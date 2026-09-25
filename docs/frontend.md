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

- **Embedding direction: the chat app is embedded as a Power Apps visual
  inside the Power BI report — not a Power BI report embedded inside Power
  Apps.** The reverse direction doesn't work for this dashboard: a
  `Power BI tile` control only pushes filters one-way through a `TileUrl`
  OData query string, which doesn't reach field parameters (Microsoft's own
  docs and multiple independent reports confirm this), and an `HTML text`
  control with a raw `<iframe>` doesn't render at all — tested directly, even
  with a plain non-Power-BI URL.
  - In the report, add the **Power Apps visual** and click **Create new** —
    not an existing app selected in. Selecting an existing app leaves
    `PowerBIIntegration.Data` (below) permanently empty; only an app created
    this way gets a working data connection. An existing app's screen can
    still be ported in afterward by copying its controls between two Studio
    tabs open side by side — formulas, including custom connector calls,
    carry over cleanly since the connector is an environment-level resource.
  - **`PowerBIIntegration.Data`** is a live, read-only table reflecting
    whatever fields/measures are dragged into the visual's **Data** well —
    configured independently per report page, and used for all dashboard-state
    capture (filters, parameters, slicers, active page — see "Visual
    grounding" below).
  - **Browser support:** Edge and Chrome only. Safari is view-only with a
    privacy caveat; Firefox and other browsers are unsupported.
  - **Screen size must be set manually to match the visual's placed size —
    it doesn't auto-scale.** Check the visual's pixel width/height in the
    report's Format pane, and set the Screen's `Width`/`Height` properties to
    match exactly. A mismatch shows as a hard crop (part of the layout
    visible, the rest cut off), not a proportional shrink.
- **`TextInput` + send `Button` + `Gallery`**, the Gallery bound to a
  `colChat` collection.
- **Custom connector call on send — positional arguments, not a record:**
  `AnalyticsAgent.PostAsk(question, conversation_id)` — a flat request-body
  schema like `AskRequest`'s gets flattened into separate positional
  parameters by Power Apps' connector generation, not exposed as one inline
  record. Optional named parameters follow as a trailing options record:
  `{filter_context: ..., active_page: ...}` — see "Visual grounding" below for
  how those two are built. `image_base64` is omitted entirely until Phase 4.

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

**Gallery uses the Flexible height layout, not the standard `TemplateSize`
gallery.** Confirmed against Microsoft's own docs (2026-09-25) — supersedes
an earlier, incorrect claim here that Power Apps galleries can't auto-size
per row; a dedicated Flexible height gallery variant exists for exactly this.
Each message's row grows to fit its actual content, so the `HtmlText` formula
no longer needs an internal `max-height` + `overflow-y: auto` scroll div —
long messages and variable-length status text (below) both render without
truncation or an inner scrollbar.

**Known caveat, documented by Microsoft — verify in Studio, chosen anyway.**
Scrolling a Flexible height gallery before all items finish loading can push
the item currently in view out of frame once loading completes. Real risk
here specifically: new messages keep appending mid-conversation while a user
may be scrolled up reading earlier ones. Accepted trade-off — the auto-height
win is worth it regardless of whether this surfaces in practice — but
confirm the actual behavior once built rather than assuming either way.

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

  **A third tier, additive to both: the model's own summarized thinking, one
  gallery row per round.** `GET /ask/status` also returns `thinking_log`
  (`.claude/rules/gateway.md`) — a growing list of `{seq, text}` entries, one
  per `agent` round that actually produced thinking (adaptive thinking means
  not every round does; a round with none just adds nothing). Track a local
  `varThinkingRowsRendered` counter, reset to `0` per turn; each `Timer` tick,
  `Collect` one `colChat` row for every entry past that count, then advance
  it — an unchanged `thinking_log` since the last poll naturally collects
  zero rows, no special-casing needed. The final answer is unaffected: still
  its own row, `Collect`ed once when the blocking `PostAsk` call resolves.

  **Style thinking rows distinctly from a real answer** — grey, italic, a
  serif fallback stack, smaller than body text:
  `style="color: #6b7280; font-style: italic; font-family: Georgia, 'Times New Roman', serif; font-size: 0.9em;"`.
  Same "verify visually once built" caveat as every other styling choice for
  this control (`pending_query`, below).

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

- **Agent-suggested follow-up chips:** bound to `suggested_follow_ups` on the response envelope; system prompt asks for 1–3 short suggestions (e.g. "Would you like to see a graph?"). Natural because `generate_chart` (`.claude/rules/tools.md`) is a real callable tool, not a promise the agent can't keep.

## Visual grounding — filter context and the optional screenshot

**Design: manual screenshot upload, not automatic capture.** Automatic capture
of an embedded Power BI report was the original plan but was confirmed not
viable (cross-origin/iframe boundary — flagged as a risk before it was hit).
Resolution: a standard `Add picture`/`Attachment` control — simpler to build
than the original plan, not harder.

**What's sent each turn:** the question (text) · an *optional* uploaded
screenshot (base64) · current filter/parameter/slicer context, read from
`PowerBIIntegration.Data` · the active report page · user identity.

**Capture is entirely manual, field by field — nothing is automatic.**
`PowerBIIntegration.Data` only ever reflects whatever DAX measures are
dragged into the Power Apps visual's Data well on the current report page;
there's no equivalent of a "give me every active filter" call. Two kinds are
captured, matching `model_schema.json`'s `parameters[]`:
- **Numeric what-ifs** — the model already exposes a `SELECTEDVALUE(...)`
  value measure for each one (`.claude/rules/data-pipeline.md`); drag those
  in directly.
- **Dimensional slicers** — a raw column can't be dragged in safely (it fans
  the well's single-row table out to one row per selected value); instead,
  add one small `SELECTEDVALUE(column)` measure per slicer and drag that in.
  Blank on multi-select is a real "no single value" signal, not a bug.

**Field parameters are excluded, permanently — not a gap to revisit.** Any
`SELECTEDVALUE(...)` measure referencing a field-parameter table (e.g.
`Evaluation Metric Parameter`) throws "This might be caused by a capacity or
license issue" when added to the visual's Data well — isolated specifically
to field-parameter tables, not a total-field-count limit; everything else in
the well is unaffected. No `Selected_*` measures for these exist in the
model. **Querying the measure directly via `run_dax_query` isn't a
workaround either:** that call runs under the service principal through a
stateless `executeQueries` request, with no attachment to any specific
user's live report session. `SELECTEDVALUE()` reflects the filter context of
*that query*, never what a separate, already-open report tab currently has
selected — there's no shared "current user selection" state living in the
semantic model at all. Which evaluation metric or ensemble combination is
currently displayed is simply unrecoverable with this architecture.

**`filter_context` is a list of `{filter_column, value}` pairs**, reusing
`model_schema.json`'s `filter_column` naming so the agent can match a
captured value straight to its parameter:
```json
[
  {"filter_column": "Conversion Rate[Conversion Rate]", "value": "0.06"},
  {"filter_column": "dataset_split_dimension[dataset_split]", "value": "Test"}
]
```
Every value is sent as text, numeric ones included — a Power Fx `Table()`
literal mixing numeric and text values in one column errors rather than
widening silently, so each is `Text(...)`-coerced first. The agent composes
DAX from these regardless of JSON type.

**Built as `{Value: ParseJSON(JSON({filter_column: ..., value: ...}))}` per
row, not the record directly.** `filter_context` is `list[dict]` in
`AskRequest` — a schemaless array — and Power Apps' connector import
surfaces that as one `Value` column typed `Dynamic`. Power Fx has no literal
syntax for `Dynamic`, so each row's real filter object is round-tripped
through `ParseJSON(JSON(...))` to produce one.

**`active_page` can't be a measure — DAX has no concept of report pages.**
Captured instead via one hardcoded constant measure per page (e.g.
`Active_Page_ModelEval = "Model_Evaluation"`), each dragged into only that
page's own Data well. Read with duck-typed field access, since each page's
real `PowerBIIntegration.Data` schema differs:
```
Set(
    varActivePage,
    With(
        {raw: ParseJSON(JSON(First(PowerBIIntegration.Data)))},
        Coalesce(Text(raw.Active_Page_ModelEval), Text(raw.Active_Page_FinancialImpact))
    )
);
```
`Coalesce()` treats blank and empty string alike, so whichever page's measure
is actually present wins. `active_page` is its own request field, not part of
`filter_context` — a page name isn't filterable; filters describe the *data*,
the active page describes what the user is *looking at*.

**Computed in `btnSend.OnSelect`, not `App.OnStart`.** `OnStart` runs once at
launch; capturing there would freeze a stale snapshot for the rest of the
session. Both variables are rebuilt fresh on every send, then passed as named
connector parameters:
```
Set(varAnswer, IfError(
    AnalyticsAgent.PostAsk(localQuestion, varConversationId,
        {filter_context: varFilterContext, active_page: varActivePage}),
    Blank()
));
```

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
