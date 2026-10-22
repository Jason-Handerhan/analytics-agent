# Power Apps Frontend

> Controls, what each turn sends, and how a response renders. The app was
> rebuilt from scratch partway through Phase 4 (flexible-height gallery,
> approval workflow, cancel button) — this doc reflects the rebuilt app, not
> the original Phase 1 sketch.
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
no licensing or DLP prompt anywhere in the flow. The real connector (OAuth
2.0 + static `x-api-key`) hit no additional licensing gate either, confirmed
once it was actually built (`docs/auth.md`).

**A licensing-terms question, not a technical one:** Microsoft's Developer
Plan FAQ frames the environment as for "development and test," with a paid
plan required for genuine "production" use — a distinction about
intent/terms, not something enforced by the environment itself. Worth being
aware of for how this project is described (a portfolio/demo build run
inside the developer environment, not a commercial product with paying
customers), not a blocker to building anything.

## App creation and embedding

**Embedding direction: the chat app is a Power Apps visual embedded inside
the Power BI report — not a Power BI report embedded inside Power Apps.**
The reverse direction doesn't work for this dashboard: a `Power BI tile`
control only pushes filters one-way through a `TileUrl` OData query string,
which doesn't reach field parameters, and an `HTML text` control with a raw
`<iframe>` doesn't render at all — tested directly, even with a plain
non-Power-BI URL.

- In the report, add the **Power Apps visual** and click **Create new** —
  not an existing app selected in. Selecting an existing app leaves
  `PowerBIIntegration.Data` (below) permanently empty; only an app created
  this way gets a working data connection. An existing app's screen can
  still be ported in afterward by copying its controls between two Studio
  tabs open side by side — formulas, including custom connector calls, carry
  over cleanly since the connector is an environment-level resource. This is
  exactly how the rebuilt app was populated with proven formulas from the
  original one.
- **`PowerBIIntegration.Data`** is a live, read-only table reflecting
  whatever fields/measures are dragged into the visual's **Data** well —
  configured independently per report page, and used for all dashboard-state
  capture (filters, parameters, slicers, active page — see "Visual
  grounding" below).
- **Browser support:** Edge and Chrome only. Safari is view-only with a
  privacy caveat; Firefox and other browsers are unsupported.
- **Screen size doesn't auto-match the visual's placed size.** Set the
  Screen's own `Width`/`Height` properties directly (Tree view → select the
  Screen → formula bar → pick the property, same mechanism as any other
  control) to match the visual's pixel size in the report's Format pane. A
  mismatch shows as a hard crop, not a proportional shrink.
- **Fixed vs. Responsive layout (App Settings → Display):** this app uses
  **Fixed** — Responsive screens don't take an explicit size at all; they
  fill whatever space their host gives them, which for an embedded visual
  means the *Power BI visual's own bounding box* becomes the real sizing
  control instead of anything set in Studio. Fixed mode is what exposes the
  Screen's own `Width`/`Height` as directly settable properties. Either way,
  the live embed's actual appearance is governed by the visual's size on the
  report canvas (resized in Power BI Desktop, not Studio) — keep the
  Screen's size and the visual's placed size in the same ballpark regardless
  of which mode is chosen.

## The flexible-height gallery — core architecture

**One outer Gallery (`Flexible height` layout, not `TemplateSize`), `Items`
bound to a local collection, `colChat` — one row per turn.** Confirmed
directly: a plain Gallery has no "fit to content" mode; a `Flexible height`
Gallery sizes each template to the tallest `AutoHeight`-marked control inside
it, which is the one mechanism everything else in this layout depends on.

**Not a nested Gallery, and not a Container per sub-element — both were
tried conceptually and ruled out before building:**
- A Gallery control has no native way to report its own rendered height back
  to an outer Gallery's flexible-height calculation, so nesting one gallery
  inside another was never confirmed to propagate sizing correctly, and was
  abandoned in favor of keeping everything at one level.
- This app's `Container` controls (Horizontal/Vertical/Grid — the modern
  Auto-layout set) were confirmed, by direct inspection of their Properties
  panel, to have **no "fit to content" height mode at all** — `Height` is
  just a plain settable number, same as any other control. Containers are
  still used where they're actually useful (arranging a few buttons side by
  side via `Direction`/`Gap`), but never relied on for auto-sizing.

**So every row within one turn is a flat stack of sibling controls inside
the Gallery's single template, positioned by chaining each one's `Y` off
the control before it:**
```
nextControl.Y = priorControl.Y + If(priorControl.Visible, priorControl.Height, 0)
```
This is the one pattern that recurs through the whole template. `AutoHeight`
on each control (confirmed to work reliably on `Label` and `HTML text`, not
on `Button`) is what makes `priorControl.Height` meaningful, and the
`If(...Visible...)` guard is what makes a row collapse to zero without
leaving a gap when it has nothing to show.

**`colChat`'s real shape**, one record per turn:
```
{ Question: Text,        -- the question text, or "Approved"/"Rejected" for
                          -- an approval-resolution turn (see below)
  Answer: Text,           -- answer_html, rendered server-side
  ThinkingLog: Text,       -- pre-joined string, not the raw {seq, text} table
                          -- (see "Live status" below for why)
  Sources: Text,           -- comma-joined, not the raw table
  FollowUps: Table,         -- raw table of {Value: Text}, kept as a table
                          -- since the 3 follow-up controls need indexed access
  IsPending: Boolean,       -- true from Collect until this turn's first
                          -- PostAsk call resolves
  NeedsApproval: Boolean }  -- true while this row's approval card should show
```

**The seed row.** `App.OnStart`/the New Chat button both run:
```
ClearCollect(colChat, {Question: "seed", Answer: "seed", ThinkingLog: "seed",
                        Sources: "seed", FollowUps: Filter(Table({Value: "seed"}), false),
                        IsPending: false, NeedsApproval: false});
```
Confirmed necessary, not just defensive: Studio's design-time formula
checker has no way to know a column's type from a genuinely empty
`ClearCollect(colChat, [])` — referencing `ThisItem.ThinkingLog` against an
empty, shapeless collection produces a real "Expected Text value" error, not
just a cosmetic warning. One concrete seed record gives Studio a shape to
check every other formula against. The Gallery's `Items` filters it back
out: `Filter(colChat, Question <> "seed")`.

### Per-row controls, in stack order

1. **Question label** — plain `Label`, `Text: ThisItem.Question`.
2. **Thinking log label** — `AutoHeight` `Label`, `Text: ThisItem.ThinkingLog`.
   `Visible`: `Not(IsBlank(ThisItem.ThinkingLog)) And ThisItem.ThinkingLog <> ""`
   — deliberately based on content, not on the global waiting flag (an
   earlier version bound this to `varWaitingForAnswer`, which hid the row
   again the instant the turn finished — wrong, since the whole point is for
   it to persist once the turn is done).
3. **Answer** — `AutoHeight` `HTML text` control, `HtmlText: ThisItem.Answer`.
4. **Sources** — `AutoHeight` `HTML text` control (not cards — plain text was
   the deliberate choice, since `sources` isn't tightly bounded in size the
   way follow-ups are, and reusing the renderer avoids re-solving formatting
   already solved for the Answer control):
   ```
   HtmlText: "<b>Sources:</b> " & ThisItem.Sources
   ```
   `Visible`: `Not(IsBlank(ThisItem.Sources)) And ThisItem.Sources <> ""`
5. **3 follow-up question Labels** (not a horizontal Container of cards — a
   fixed count of individually-positioned `AutoHeight` Labels, chosen since
   each needs its own `OnSelect` and `AutoHeight` (the length isn't bounded
   tightly enough for a fixed-size card)). Per card `N` (1–3):
   - `Text`: `If(CountRows(ThisItem.FollowUps) >= N, Index(ThisItem.FollowUps, N).Value, "")`
     — the guard matters: `Index()` past the end of the table errors even on
     a *hidden* control, since `Visible = false` doesn't stop the formula
     from being evaluated.
   - `Visible`: `CountRows(ThisItem.FollowUps) >= N` — safe to collapse
     bottom-up only, since `suggested_follow_ups` is always front-loaded (an
     index-2 entry implies index 1 exists too).
   - `Y`: chained off the previous follow-up (or off Sources, for card 1).
   - `OnSelect`: `Set(varPrefill, Index(ThisItem.FollowUps, N).Value); Reset(TextInput1)`
   - Styling: no border, `Fill: Transparent`, `HoverFill: RGBA(128, 128, 128, 0.2)`.
6. **Approval Container** (`Horizontal` direction, holding Approve/Reject
   buttons) — `Y` chained off the last follow-up; `Visible: ThisItem.NeedsApproval`;
   `Height: If(ThisItem.NeedsApproval, 60, 0)` (explicit, since Containers
   don't auto-size — see above).
7. **Trailing spacer** — any control with no visible content, `Height: 20`,
   `Y` chained off whatever is currently the *last* element in the stack.
   **Must be re-chained every time a new row type is inserted before it** —
   hit twice during the build (once when follow-ups were added after
   Sources, once when the approval Container was added after follow-ups).
   This is purely trailing padding between one turn and the next; nothing
   else chains off it.

## Live status while a turn is in flight

**One `Timer`, polling `GET /ask/status/{conversation_id}` once a second,
feeding two independent things from the same response:**
- `Timer` properties: `Duration: 1000`, `Repeat: true`, `Start: varWaitingForAnswer`.
- `OnTimerEnd`:
  ```
  Set(varStatusResponse, AnalyticsAgent.GetAskStatus(varConversationId));
  Set(varCurrentStatus, varStatusResponse.status);
  Set(varCurrentThinkingLog, varStatusResponse.thinking_log);
  UpdateIf(colChat, IsPending = true, {ThinkingLog: Concat(Sort(varCurrentThinkingLog, seq), text & Char(10) & Char(10))})
  ```

**`varCurrentStatus`** (a single, overwritten-each-poll string) feeds the
standalone status popup, entirely outside the Gallery. **`varCurrentThinkingLog`**
(the growing `{seq, text}` array) feeds the in-Gallery thinking-log row for
whichever turn is currently `IsPending` — joined into one string via
`Concat(Sort(..., seq), text & Char(10) & Char(10))` at write time, not kept
as a raw table, since `ThinkingLog`'s column type in `colChat` needs to stay
consistently `Text` (the seed row forced this — an earlier version stored
the raw table and hit a real "Invalid argument type" error on the seed row's
plain-text value).

**`IsPending` marks the one row currently waiting on its first `PostAsk`
call** — `true` from the moment it's `Collect`-ed, `false` once that call
resolves. Exactly one row should ever be `IsPending = true` at a time.

**A real Gallery-rendering quirk, confirmed live, not assumed: the Gallery
doesn't visibly reflect a freshly-`Collect`-ed row until a *second* write
touches `colChat`.** The Send button's `OnSelect` therefore does an
immediately-following, otherwise-redundant `UpdateIf` right after the
initial `Collect`:
```
Collect(colChat, {Question: varQuestion, Answer: "", ThinkingLog: "", IsPending: true});
UpdateIf(colChat, IsPending = true, {Question: varQuestion});
```
The second write's content doesn't matter — only that it's a second
distinct write to the same row.

## The status popup, icon, and TurnTimer

Entirely separate from the Gallery — a fixed element near the input box.

- **Status text**: `Text: varCurrentStatus`, `Visible: varWaitingForAnswer`.
- **Icon**: `Visible: varWaitingForAnswer`. Pulses via `Transparency`, driven
  continuously off a second Timer's live `.Value` (not a toggled boolean —
  a dedicated boolean-blink Timer was tried first and discarded once this
  continuous approach proved smoother):
  ```
  Transparency: Max(0, Sin(TurnTimer.Value / 300)) * 0.5
  ```
  `Max(0, Sin(...))` clamps the falling half of the wave to fully opaque, so
  the icon holds fully visible for roughly half of each cycle rather than
  spending equal time at both extremes. **`Transparency` is a 0–1 scale, not
  0–100** — confirmed live the hard way: an initial `* 70` and a later `* 30`
  produced *visually identical* results, which only makes sense if both were
  clamping to the same maximum past a 0–1 ceiling.
- **`TurnTimer`**: `Start: varWaitingForAnswer`, `Visible: varWaitingForAnswer`,
  `Duration: 120000` (a safety ceiling past the gateway's own
  `GATEWAY_TURN_TIMEOUT_SECONDS = 115`, not a real limit), `Repeat: false`.
  Its own default built-in visual (confirmed live) already renders a running
  elapsed-time display — no separate Label needed.
- **`TurnTimer.Reset`**, bound to `varResetTurnTimer`, pulsed `true` then
  `false` at the start of every Send/Approve/Reject, so elapsed time
  restarts at zero each turn instead of continuing to climb.

## Send button

The real, current `OnSelect` — every piece here earned its place through a
real bug or a real design question during the build, not speculative:
```
Set(varQuestion, TextInput1.Text);
Set(varPrefill, "");
Set(varCancelRequested, false);
Reset(TextInput1);
Collect(colChat, {Question: varQuestion, Answer: "", ThinkingLog: "", IsPending: true});
UpdateIf(colChat, IsPending = true, {Question: varQuestion});
Set(varResetTurnTimer, true);
Set(varWaitingForAnswer, true);
Set(varResetTurnTimer, false);

Set(
    varFilterContext,
    Table(
        {Value: ParseJSON(JSON({filter_column: "Conversion Rate[Conversion Rate]", value: Text(First(PowerBIIntegration.Data).'Conversion Rate Value')}))},
        {Value: ParseJSON(JSON({filter_column: "Churn Rate[Churn Rate]", value: Text(First(PowerBIIntegration.Data).'Churn Rate Value')}))},
        {Value: ParseJSON(JSON({filter_column: "Conversion Lift per 10% Recall[Conversion Lift per 10% Recall]", value: Text(First(PowerBIIntegration.Data).'Conversion Lift per 10% Recall Value')}))},
        {Value: ParseJSON(JSON({filter_column: "AOV Lift per 10% Recall[AOV Lift per 10% Recall]", value: Text(First(PowerBIIntegration.Data).'AOV Lift per 10% Recall Value')}))},
        {Value: ParseJSON(JSON({filter_column: "Churn Reduction per 10% Recall[Churn Reduction per 10% Recall]", value: Text(First(PowerBIIntegration.Data).'Churn Reduction per 10% Recall Value')}))},
        {Value: ParseJSON(JSON({filter_column: "dataset_split_dimension[dataset_split]", value: First(PowerBIIntegration.Data).Selected_Dataset_Split}))},
        {Value: ParseJSON(JSON({filter_column: "models_dimension[Model]", value: First(PowerBIIntegration.Data).Selected_Model}))},
        {Value: ParseJSON(JSON({filter_column: "base_models_dimension[model_name]", value: First(PowerBIIntegration.Data).Selected_Base_Model}))},
        {Value: ParseJSON(JSON({filter_column: "Evaluation Metric Parameter[Evaluation Metric Parameter]", value: First(PowerBIIntegration.Data).Selected_Evaluation_Metric}))},
        {Value: ParseJSON(JSON({filter_column: "Ensemble Weight Parameter[Ensemble Weight Parameter]", value: First(PowerBIIntegration.Data).Selected_Ensemble_Weights_Model}))},
        {Value: ParseJSON(JSON({filter_column: "Feature_Importance_Chart_Types[Chart_Types]", value: First(PowerBIIntegration.Data).Selected_Feature_Importance_Plot}))}
    )
);

Set(
    varResponse,
    AnalyticsAgent.PostAsk(
        varConversationId,
        {
            question: varQuestion,
            filter_context: varFilterContext,
            active_page: First(PowerBIIntegration.Data).Active_Page
        }
    )
);

**The `{Value: ...}` wrapper is not stripped by the connector — confirmed
live, 2026-10-13, not assumed.** Each `filter_context` entry arrives at the
gateway as `{"Value": {"filter_column": ..., "value": ...}}`, not the flat
`{"filter_column": ..., "value": ...}` shape an earlier version of this doc
assumed (and never actually verified). Any backend code reading a
`filter_context` entry's fields programmatically has to account for this —
`resolve_image_name` (`.claude/rules/tools.md`) does, via
`fc.get("Value", fc)`. Code that only ever reads `filter_context` as
unstructured text (the model's own prompt, `build_human_message`) is
unaffected either way, since an LLM reading prose tolerates the extra
nesting fine.

Set(varWaitingForAnswer, false);
UpdateIf(
    colChat,
    IsPending = true,
    {
        Answer: varResponse.answer_html,
        ThinkingLog: Concat(Sort(varCurrentThinkingLog, seq), text & Char(10) & Char(10)),
        Sources: Concat(varResponse.sources, Value, ", "),
        FollowUps: varResponse.suggested_follow_ups,
        IsPending: false,
        NeedsApproval: varResponse.needs_approval
    }
)
```

**`PostAsk`'s real call shape — `conversation_id` positional, everything
else in one trailing record — not `(question, conversation_id)` as
originally planned.** `AskRequest.required` is only `["conversation_id"]`
(matching the Pydantic model — `question` has a default, so it isn't
JSON-schema-required even though a runtime validator enforces exactly-one-of
`question`/`approval_decision`), so Power Platform's connector generation
bundles `question` into the optional trailing record rather than giving it
its own positional slot.

**`filter_context: []` doesn't work — needs `Table()`, and even an empty
`Table()` isn't enough on its own.** `filter_context` is `list[dict]` in
`AskRequest`, a schemaless array, which Power Apps surfaces as a table of
`{Value: Dynamic}` records. A bare `[]` or an untyped `Table()` can't be
coerced to that shape — Power Fx needs a real sample record to infer the
type from. The actual empty-array value sent today:
```
Filter(Table({Value: ParseJSON(JSON({filter_column: "", value: ""}))}), false)
```
(used today only as a placeholder in Approve/Reject's call, which omits
`filter_context` from the record entirely instead — see below. The pattern
above is kept here since it's the one way to construct a genuinely empty,
correctly-typed table of this shape, if ever needed again.)

**`Active_Page` and `filter_context` are both read fresh in `OnSelect`, not
`App.OnStart`** — dashboard selections can change between messages.
`Active_Page` is a single DAX measure now (`docs/data-pipeline.md`), read
directly — no `Coalesce()` across multiple per-page constants, which was the
original, abandoned design (see `docs/data-pipeline.md` for why).

## Cancel button

A separate `Button`, overlaid exactly on top of the Send button (not a
distinct control elsewhere on screen) — `X`/`Y`/`Width`/`Height` all mirror
`sendButton`'s own, so it visually reads as "Send transformed into Cancel"
during a turn, and must sit *after* `sendButton` in the Tree view to
actually catch the click.
- `Visible: varWaitingForAnswer`
- `DisplayMode: If(varCancelRequested, DisplayMode.Disabled, DisplayMode.Edit)`
- `OnSelect`:
  ```
  Set(varCancelRequested, true);
  AnalyticsAgent.PostAskCancel(varConversationId)
  ```
  Fire-and-forget, per the endpoint's own design (`.claude/rules/gateway.md`)
  — the original blocked `PostAsk` call resolves on its own shortly after.
- `varCancelRequested` resets to `false` at the top of every new
  Send/Approve/Reject, so Cancel is clickable again for each new wait.

**Send itself is also disabled while waiting** (`DisplayMode.Disabled`
driven by `varWaitingForAnswer`) — belt-and-suspenders alongside the Cancel
overlay physically covering it. **Also disabled while *any* row has an
unresolved approval card showing** — not just while a turn is actively in
flight. See "Approval workflow," below, for why and the exact formula.

## Approval workflow

**The backend's "resume" is a genuinely new graph invocation each time, not
a suspended execution woken back up — confirmed from the real code, and it
changes how the frontend has to model it.** Pausing ends the turn
completely: `finalize_node` runs, a real telemetry row gets written, and the
`live_turns` document is replaced with a `PendingApproval` snapshot. Approving
or rejecting calls `POST /ask` again with `approval_decision` set; the
gateway reconstructs an `AgentState` from that snapshot and calls
`graph.astream(...)` fresh, `START` to `finalize` again, exactly like any
other turn. "Same turn" is the *state* carrying continuity, not the
*execution* — there's no in-place resumption happening anywhere.

**So the frontend treats Approve/Reject as creating a new turn — a new
`Collect`-ed row, not an `UpdateIf` of the paused one** — matching what the
backend actually does (a second, separate telemetry row) and what gives the
best chat transcript anyway: one row per real event, "asked X → needed
approval → approved → here's the answer" readable top to bottom instead of
the pending-approval content vanishing, overwritten in place.

**Approve button's `OnSelect`** (Reject is identical, swapping the literal
string and the `varQuestion` label):
```
UpdateIf(colChat, NeedsApproval = true, {NeedsApproval: false});
Set(varQuestion, "Approved");
Set(varCancelRequested, false);
Collect(colChat, {Question: varQuestion, Answer: "", ThinkingLog: "", IsPending: true});
UpdateIf(colChat, IsPending = true, {Question: varQuestion});
Set(varResetTurnTimer, true);
Set(varWaitingForAnswer, true);
Set(varResetTurnTimer, false);

Set(
    varResponse,
    AnalyticsAgent.PostAsk(varConversationId, {approval_decision: "approved"})
);

Set(varWaitingForAnswer, false);
UpdateIf(
    colChat,
    IsPending = true,
    {
        Answer: varResponse.answer_html,
        ThinkingLog: Concat(Sort(varCurrentThinkingLog, seq), text & Char(10) & Char(10)),
        Sources: Concat(varResponse.sources, Value, ", "),
        FollowUps: varResponse.suggested_follow_ups,
        IsPending: false,
        NeedsApproval: varResponse.needs_approval
    }
)
```

**The first line is its own fix, not boilerplate** — without it, the
*original* paused row's `NeedsApproval` stays `true` forever (nothing else
in this flow ever touches that row again), leaving a dead, unclickable
approval card showing permanently. Matching on `NeedsApproval = true` finds
whichever row is currently showing a card and clears it the instant the
decision is made, independent of whether the new turn has resolved yet.

**`filter_context`/`active_page` are deliberately omitted from this call** —
`PendingApproval` already carries the original turn's captured values
server-side; resending them would be silently ignored, not harmful, just
dead weight.

**Send and New Chat are both disabled while any row has `NeedsApproval:
true`** — deliberately forcing a response rather than letting the user
abandon or route around an unresolved card. Not a second piece of state:
derived directly from `colChat` itself, so it can't drift out of sync with
what's actually showing:
```
DisplayMode: If(varWaitingForAnswer Or CountRows(Filter(colChat, NeedsApproval)) > 0, DisplayMode.Disabled, DisplayMode.Edit)
```
Cancel doesn't need this — it's already hidden in this state (`Visible:
varWaitingForAnswer`), since there's nothing in flight to cancel once a
pause has landed.

**This isn't primarily a UX nicety — `live_turns` has no TTL
(`.claude/rules/orchestrator.md`), so an abandoned pause is a genuine,
permanent Firestore orphan if nothing ever touches that `conversation_id`
again.** Traced through the real code: a normal question skips the
approval-resume branch entirely (gated on `approval_decision is not None`),
so it doesn't explicitly clean up a stale pause — but `consume_graph`'s own
end-of-turn write does it as a side effect either way: a normal completion
`delete()`s the whole document, and pausing again does a **non-merged**
`set()` that replaces it outright. So *any* later turn in that same
conversation cleans up the stale one — the orphan only becomes permanent if
the user closes the app mid-pause and never returns to that exact
conversation again. With Send/New Chat both locked while a card is showing,
that's now hard to do from *within* a live session — the realistic path is
closing the app entirely.

**Decided: no TTL/scheduled cleanup for this.** Considered and rejected —
the scenario that actually leaves a permanent orphan is narrow (close
mid-pause, and never return to that specific conversation, and if restored
from history, never send another message in it), and a leftover Firestore
document costs nothing meaningful. Keeping the option to resume open
indefinitely is a property worth having, not a gap to close.

**What chat history restoration actually needs, decided and built without a
live `live_turns` check at all — simpler than originally planned here.**
The original plan assumed a row's `needs_approval` could go stale between
write and restore (resolved in a later session), requiring a live lookup
against `live_turns/{conversation_id}` to confirm which row is genuinely
still actionable. Tracing through the write path again during the actual
build: every resolution (`approved` or `rejected`) is itself a new `/ask`
call, which always produces its own new, later chat-history row — so a
conversation's **last** row always reflects true current state by
construction, with no window where it can go stale. Every *other* row's
`needs_approval` is real history (it genuinely was `true` when written) but
not current status, and must be forced `false` regardless of its own stored
value. See "Chat history," below, for the actual restore formula — a live
`live_turns` check was considered and dropped once this was traced through,
not left for later.

**The enriched pending-approval message (cost + query) is built entirely on
the backend, inside `finalize_node`'s `needs_approval` branch** — appended
to `answer_markdown` as a fenced SQL block, then rendered through the same
`answer_html` pipeline as any other answer. No separate frontend display
work needed for it; `htmlAgentResponse` just shows it like any other answer.

**A real cosmetic bug, fixed, 2026-10-14:** clicking Approve/Reject could
briefly show the *previous* turn's leftover thinking-log text on the new
row. Root cause, confirmed by reading the real code: not a Firestore race
(the backend's reject path deletes `live_turns` immediately, and the
pause-time write already drops `thinking_log` from the document via a full,
non-merged replace) — it's `varCurrentThinkingLog`, a local variable, only
getting overwritten when the polling Timer actually ticks. Reject resolves
fast enough that the Timer's first ~1s tick often never fires before the
response returns, so the final `UpdateIf` built the new row from whatever
was still sitting in that variable from the *original* turn's last real
poll. Fixed by clearing `varCurrentThinkingLog` locally at the top of
Approve/Reject's `OnSelect` (`Set(varCurrentThinkingLog, Blank())`), before
anything else.

## Icons

- **`Classic/Icon` controls have no `BorderRadius` property — confirmed by
  checking, not assumed.** For rounded corners on an icon built this way,
  layer a `Rectangle` shape behind it (same `X`/`Y`/`Width`/`Height`,
  `BorderRadius` set there instead, same `Fill` color) with the Icon's own
  `Fill` set to `Transparent` so only the Rectangle's rounded background
  shows through, Icon glyph rendering on top.
- **Custom SVG icon set, built but not yet swapped in** (still on the
  default `Classic/Icon` controls as of this writing): a matched Send/Stop
  pair — 100×100 rounded (`rx="16"`) orange (`#F98B5C`) square with a white
  glyph (an up-arrow for Send, a square for Stop) — plus a separate-style
  New Chat icon: an outline-only chat bubble with a `+` badge, `#808080`
  stroke, transparent/white fill, no colored background square (deliberately
  different treatment from Send/Stop, since it's a secondary action).
- **Swapping an icon file in place, without touching any control's
  formula**, only works for a genuine media asset (`Image` control bound to
  an uploaded file) — Studio's Media pane has a "Replace" option for that.
  It does **not** apply to a `Classic/Icon`'s built-in `Icon.Send`-style enum
  value, which isn't a media asset at all; switching to the custom SVGs
  means building new `Image` controls and porting over `Height`/`Width`/
  `X`/`Y`/`DisplayMode`/`OnSelect` (not `Color`/`Fill`/`HoverFill` — those
  existed only to tint the old vector icon, and the new SVGs already have
  their colors baked in).
- **New Chat button** — `OnSelect` mirrors `App.OnStart` exactly (mints a
  fresh `conversation_id`, re-seeds `colChat`, resets every loose
  variable) — see `App.OnStart`, below. `DisplayMode: If(varWaitingForAnswer, DisplayMode.Disabled, DisplayMode.Edit)`,
  same as every other action button.

## `App.OnStart`

```
IfError(
    Set(varConversationId, AnalyticsAgent.PostConversation().conversation_id),
    Notify("Could not start a new conversation. Please restart the app.", NotificationType.Error);
    Set(varConversationId, Blank())
);
ClearCollect(colChat, {Question: "seed", Answer: "seed", ThinkingLog: "seed", Sources: "seed", FollowUps: Filter(Table({Value: "seed"}), false), IsPending: false, NeedsApproval: false});
Set(varQuestion, Blank());
Set(varResponse, Blank());
Set(varCurrentThinkingLog, Blank());
Set(varCurrentStatus, Blank());
Set(varWaitingForAnswer, false);
Set(varPrefill, "");
Set(varCancelRequested, false);
Set(varResetTurnTimer, false);
```

**`PostConversation()` takes zero visible arguments — `x-api-key` must be
configured as an Internal-visibility, default-valued parameter on every
operation in the connector, not left as a plain visible header.** Confirmed
the hard way: re-updating the connector's Swagger definition through the
Swagger Editor reset this per-parameter setting (it isn't encoded in the
Swagger JSON at all, it's a separate Power Platform-side setting layered on
top), which surfaced as `PostConversation()` suddenly demanding an argument
it never needed before. Fix lives in the connector editor's **Request** step
per operation: set `x-api-key`'s **Visibility → Internal** and **Default
value →** the real secret value, for all 4 operations, every time the
connector definition gets re-imported.

## `answer_html` styling

**Power Apps' `Size` property is in *points*, not pixels — a different unit
than CSS `font-size`.** Tuning sizes by eye against native Power Apps Labels
first, then applying the same number as a CSS pixel value, produces visibly
smaller text than intended. Conversion used: `1pt ≈ 1.33px`. Current values
(`app/config.py`), all derived this way, not chosen as pixel values
directly:
```
ANSWER_FONT_FAMILY = "Segoe UI, sans-serif"
ANSWER_BODY_FONT_SIZE = "24px"
ANSWER_HEADING_FONT_SIZE = "32px"
ANSWER_SUBHEADING_FONT_SIZE = "27px"
ANSWER_TABLE_FONT_SIZE = "24px"
ANSWER_CODE_FONT_SIZE = "22px"
```

**Every element needs its own explicit inline font styling — confirmed
nothing can be left to inherit, not just for headings/tables but for list
items and inline code too.** The control doesn't reliably honor CSS
inheritance: a real bug surfaced where `<li>` text and inline `<code>` had
no explicit `font-size` at all and silently fell back to the browser's
default size, visibly mismatched against everything around it. The renderer
(`_AnswerRenderer`, `app/gateway/entry_exit.py`) now styles `paragraph`,
`heading` (levels ≤2 get `ANSWER_HEADING_FONT_SIZE`, levels 3+ get
`ANSWER_SUBHEADING_FONT_SIZE`), `list_item`, `codespan`, `block_code`, and
every table cell — nothing is left to the surrounding context.

**Chart display: inline `<img>`, decided and built.** `answer_html`'s
mistune renderer embeds chart images directly in the flowing answer text
(`<img src="..." style="max-width: 100%;" />`), confirmed live to render
correctly from a real signed GCS URL. `MAX_ANSWER_CHARS` is 7000 to absorb
the signed URL's query string. **No platform character limit exists on the
control at all** — confirmed against Microsoft's own Power Fx docs; the
earlier-assumed "16,384-character limit" was never real.

## Response formatting — pipeline

Agent produces Markdown → gateway converts to HTML (`mistune`, custom
renderer, `.claude/rules/gateway.md`) → Power Apps renders via an **HTML
text** control, never a plain `Label` (which shows raw Markdown literally).
`answer_markdown` is still sent alongside `answer_html` on every response —
debugging/notebook value — but the HTML control always binds to `answer_html`.

## Conversation lifecycle

**The ID comes from the server, at app start** (`App.OnStart`, above),
stored in `varConversationId`. **A session is the app lifecycle** — closing
and reopening mints a new ID, so history doesn't carry across today (see
"Chat history," below, for the planned fix).

## Chat history — built and live-verified, 2026-10-14

Decided during frontend planning: real but bounded scope, worth building.
Backend built first (`.claude/rules/gateway.md`'s chat-history section,
`docs/testing.md` §4c); this section is the frontend half — the picker UI
and the restore logic — plus two real bugs found only once it was wired up
end to end.

**Storage: a new BigQuery table, not Firestore and not a reuse of
`agent_telemetry`.** Firestore was ruled out early — a long conversation's
full history risks the 1MB document-size ceiling. Reusing `agent_telemetry`
was considered and rejected in favor of a dedicated table: `AgentResponse`
is already built in exactly the shape the frontend needs by the time a turn
finishes (`answer_html` pre-rendered, `sources`/`suggested_follow_ups`
already in final form) — serializing that object plus the question directly
into its own table means the read side is a flat `SELECT`, nothing to
re-derive at read time the way reusing telemetry would require
(re-rendering markdown, re-deriving sources from `tool_calls`). Reached only
via `turn-writer-sa` impersonation, the same identity and reasoning as
telemetry's own writer — no direct `agent-sa` grant on this dataset at all
(`.claude/rules/telemetry.md`).

**Write every turn unconditionally, including ones where `needs_approval`
was `true` at the time.** Skipping those was considered and rejected: the
live session already keeps a pause turn and its resolution as two separate
rows (not one overwritten in place, see "Approval workflow" above), and
history should match what the user actually saw live, not a trimmed version
of it. Safety against a stale, unclickable approval card on a restored
historical turn comes from the restore formula forcing `NeedsApproval:
false` on every row except the conversation's actual last one (below) — not
from omitting the data, and not from a live `live_turns` check, which turned
out to be unnecessary (see the "Approval workflow" section's restore-time
note, above, for why).

**Two endpoints**, matching the existing REST-ish shape:
- `GET /conversations` — list past conversations for the calling user:
  `conversation_id`, first question (truncated at a word boundary, as the
  display title — no model-generated naming needed, same pattern most chat
  products use), last activity timestamp.
- `GET /conversations/{id}/history` — every turn for one conversation,
  ordered oldest first, each already shaped to match what `colChat` expects.

**The picker UI.** A clock icon (plain outline, `#808080` stroke, transparent
background — same treatment as New Chat, a secondary action not a primary
one) toggles a `Container` (`grpConversationPicker`: a title label, an X
close icon, and a flexible-height `Gallery` bound to `colConversations`) via
`varShowConversationPicker`, gating all three controls' visibility at once
rather than setting it per-control. The icon's `OnSelect` refetches on open
only; the X icon's `OnSelect` closes and resets `colConversations` back to
just its seed row — closing without this left stale data from the previous
open visible for however long the next fetch took, a real, confirmed bug
before this reset was added. `colConversations`'s columns are
PascalCase (`ConversationId`/`Title`/`LastActivityAt`, matching `colChat`'s
own convention), mapped from the connector's snake_case response via
`RenameColumns` — column names there are bare identifiers, not quoted
strings, confirmed live after an initial attempt errored.

**Restoring a conversation — the real, final formula**, on the picker row's
`OnSelect`:
```
Set(varShowConversationPicker, false);
ClearCollect(colConversations, {ConversationId: "seed", Title: "seed", LastActivityAt: Now()});

Set(varConversationId, ThisItem.ConversationId);
Set(varQuestion, Blank());
Set(varResponse, Blank());
Set(varCurrentThinkingLog, Blank());
Set(varCurrentStatus, Blank());
Set(varWaitingForAnswer, false);
Set(varPrefill, "");
Set(varCancelRequested, false);
Set(varResetTurnTimer, false);
Reset(TextInput1);

ClearCollect(colChat, {Question: "seed", Answer: "seed", ThinkingLog: "seed", Sources: "seed", FollowUps: Filter(Table({Value: "seed"}), false), IsPending: false, NeedsApproval: false});

With(
    {turns: AnalyticsAgent.GetConversationHistory(ThisItem.ConversationId).turns},
    ForAll(
        turns,
        Collect(colChat, {
            Question: ThisRecord.question,
            Answer: ThisRecord.answer_html,
            ThinkingLog: "",
            Sources: Concat(ThisRecord.sources, Value, ", "),
            FollowUps: ThisRecord.suggested_follow_ups,
            IsPending: false,
            NeedsApproval: false
        })
    );
    UpdateIf(colChat, true, {IsPending: false});
    If(
        Last(turns).needs_approval,
        Patch(colChat, Last(colChat), {NeedsApproval: true})
    )
)
```
Reuses the existing `conversation_id` rather than minting a new one, resets
every loose variable `App.OnStart` resets, re-seeds `colChat`, then restores.
`Sources` is comma-joined (`Concat(..., Value, ", ")`), matching the normal
Send flow — `colChat.Sources` is `Text`, not a raw table, a mismatch the
very first draft of this formula got wrong by assigning the array directly.
`ThinkingLog` is always empty on restore — it was never persisted anywhere,
even in the new table, so this is a known, accepted gap, not an oversight.
The trailing `UpdateIf(colChat, true, {IsPending: false})` is a no-op on
values (every row is already `false`) but a real second write — the same
"doesn't render until a second write" Gallery quirk `colChat` already had,
now hitting a conversation with exactly one turn, which otherwise has
nothing after its own `Collect` to nudge it into rendering.

**Every row's `NeedsApproval` is forced `false` at `Collect` time, and only
the conversation's actual last turn gets patched back to `true`, if the
backend itself says that turn is still pending.** Collecting
`ThisRecord.needs_approval` directly on every row — the first version of
this formula — reproduced exactly the symptom "every approve/reject card in
the whole history shows up at once": every row that *ever* paused keeps
`needs_approval: true` in storage forever, since a resolution never
retroactively corrects the row that paused, it just adds a new one
afterward. Only the very last row's stored value can ever still be live.
`Patch(colChat, Last(colChat), {NeedsApproval: true})` targets that one row
directly — `Patch` on a local collection matches by whole-record value
equality, not identity, so this is only safe because nothing else in
`colChat` is guaranteed byte-identical to the last row; a duplicate-valued
row elsewhere (plausible for two **rejected** turns in the same
conversation, which always share the same canned message) could in
principle cause this to patch more than one row. Not hit in practice, worth
knowing if `NeedsApproval` ever looks wrong on a rejected-heavy history.

**The real bug, found only once a restored conversation had more than one
turn: the Question label's `Text` property was a bare `varQuestion`, not
`ThisItem.Question`.** Every row in the gallery was displaying the exact
same thing — whatever the single global `varQuestion` currently held — not
its own stored value. This had always worked for ordinary live turns purely
by coincidence: there's only ever one turn anyone is looking at at a time,
and `colChat.Question` happens to get written correctly anyway by Send's
own existing `UpdateIf(colChat, IsPending = true, {Question: varQuestion})`
(the same second-write quirk fix, originally built for a different reason).
Nobody was reading that stored value back out, so nobody noticed every row
was really just mirroring one shared variable until restore put several
rows on screen simultaneously: right after restore `varQuestion` is
`Blank()`, so every row showed blank, and the next real question overwrote
all of them at once, including historical "Approved"/"Rejected" rows,
because they were never independent in the first place. Fixed by changing
the label's `Text` to `ThisItem.Question` — safe for live turns too, since
`colChat.Question` is already correctly populated by the time a row is ever
actually rendered on screen.

**Still open:** whether the New Chat button's `OnSelect` (which mirrors
`App.OnStart`) also resets `varShowConversationPicker` to `false` — flagged
during this build, not yet confirmed done.

## Deleting a conversation — built and live-verified, 2026-10-21

Soft-delete, not a real `DELETE` — `DELETE /conversations/{id}` writes a
marker row to a separate `deleted_conversations` table; both read endpoints
exclude soft-deleted `conversation_id`s instead (`.claude/rules/gateway.md`
has the full reasoning, including why a real `DELETE` doesn't work reliably
against BigQuery's streaming-insert buffer). Never touches `sessions` —
chat-history management stays entirely a BigQuery concern, deliberately.

**An X icon inside the existing picker gallery template**, alongside the
title label, not a separate row:
- `DisplayMode: If(ThisItem.ConversationId = varConversationId, DisplayMode.Disabled, DisplayMode.Edit)`
  — disabled specifically on whichever conversation is currently open, so
  there's no "deleted the conversation I'm chatting in" case to handle at
  all; the backend already guarantees a paused turn resumes on the model it
  paused on regardless (`.claude/rules/orchestrator.md`'s `PendingApproval`
  carry rule) — this makes that guarantee visible to the user too, not just
  true underneath.
- `OnSelect`:
  ```
  Set(varPendingDeleteId, ThisItem.ConversationId);
  Set(varPendingDeleteTitle, ThisItem.Title);
  Set(varShowDeleteConfirm, true)
  ```

**A confirm popup (`grpDeleteConfirm`)**, same construction pattern as
`grpConversationPicker`/the model picker's container — a title/warning
label, Yes/No buttons, gated by one `Visible` flag:
- `Visible: varShowDeleteConfirm`
- Warning text references `varPendingDeleteTitle` and states the deletion
  can't be undone.
- **Yes** — `OnSelect`:
  ```
  Set(varShowDeleteConfirm, false);
  AnalyticsAgent.DeleteConversation(varPendingDeleteId);
  RemoveIf(colConversations, ConversationId = varPendingDeleteId);
  Set(varPendingDeleteId, Blank());
  Set(varPendingDeleteTitle, Blank());
  ```
  `RemoveIf` on `colConversations` gives instant UI feedback rather than
  waiting on a full `GetConversations` refetch.
- **No** — `OnSelect`: same `Set(varShowDeleteConfirm, false)` plus clearing
  both pending variables, no connector call.

**`varShowDeleteConfirm` seeded `false` in `App.OnStart`** alongside the
app's other loose variables; `varPendingDeleteId`/`varPendingDeleteTitle`
need no seeding, since they're only ever set immediately before use.
