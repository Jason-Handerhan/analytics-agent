# Power Apps Frontend

> Controls, what each turn sends, and how a response renders. **Phase 1** for
> the basic app; the approval card and cancel button land with their features.
>
> The request contract itself is in `.claude/rules/gateway.md`; the connector
> that carries it is in `docs/auth.md`.

**License: Power Apps Premium is required, unconditionally, from Phase 1.**
Power Apps classifies *every* custom connector as Premium regardless of what
it wraps — even a connector calling a plain REST endpoint. This is a Phase 0
blocker, not something deferred like the Power BI licensing question. Try the
free **Power Apps Developer Plan** first (an individual build/test
environment); fall back to a paid Premium seat (~$20/user/month — the cheaper
per-app tier was discontinued January 2026) only if the free plan doesn't
support custom connectors in practice (documented sources disagree; verify
empirically).

## Layout & controls

**App type:** Canvas app — needed for layout control alongside an embedded
report.

- **Power BI report control** (embedded).
- **`TextInput` + send `Button` + `Gallery`**, the Gallery bound to a
  `colChat` collection.
- **Custom connector call on send:**
  `AnalyticsAgent.Ask({question, image_base64, filter_context, active_page, conversation_id})`.

**The Gallery template needs two shapes, not one.** A user's message is plain
text and must render in a `Label`; an agent's message is HTML from the gateway
and must render in an **HTML text control**. Putting user text through the HTML
control would interpret anything angle-bracketed as markup.

```
colChat record:  { role: "user" | "agent",
                   body: <plain text for user, HTML for agent>,
                   chart_url: <optional, agent only> }
```

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
