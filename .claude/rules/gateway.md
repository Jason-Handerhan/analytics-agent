---
paths:
  - 'app/gateway/**'
  - 'app/config.py'
  - 'tests/test_gateway.py'
  - 'tests/test_static_context.py'
  - 'tests/test_entry_exit.py'
---

# Gateway: auth, API surface, response formatting

> **Deep dives:** `docs/approval-workflow.md` for the approval pause/resume
> flow. `docs/auth.md` for the full request-time auth chain,
> `docs/frontend.md` for Power Apps response rendering. `local-dev-environment-setup.md`
> Step 14 for the Entra app registrations this depends on.

## Core principle — the gateway is dumb code, the agent is the translator

- **Gateway** = deterministic infrastructure boundary: authN/authZ, token
  validation, request validation, assembling raw materials, logging.
  (**Rate limiting deliberately not built** — no realistic abuse scenario at
  this scale; component reference §3 trade-offs.) **Never an LLM** — non-deterministic, slower, a prompt-injection
  surface, unauditable.
- **Agent** = semantic translation (deciding what the user means).
- The gateway *does* do deterministic **format marshalling** (building prompt
  structure from known fields). The rule is "no interpretation in the gateway,"
  not "no translation."

## Security

**Authorization model.** One shared service account (`agent-sa`) authorizes
all backend access — its grants are the ceiling for every user alike. Access
is gated at the app level, not per tool: whoever can open the app can reach
every tool. The gateway authenticates *that the caller is a legitimate user*,
not what they're individually allowed to do.

### Two factors, both enforced

```python
from fastapi import Header, HTTPException
import jwt
from jwt import PyJWKClient

_jwks_client = PyJWKClient(
    f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
)

def validate_entra_token(authorization: str) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.removeprefix("Bearer ")
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=EXPECTED_AUDIENCE,
            issuer=f"https://sts.windows.net/{TENANT_ID}/",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid token: {e}")

def validate_api_key(x_api_key: str) -> None:
    if x_api_key != get_gateway_api_key():
        raise HTTPException(401, "Invalid API key")

# Route handlers extract headers via `= Header(...)` and call these two
# directly with the resulting strings — not `Depends(validate_entra_token)`.
# Decided (2026-09-15): plain function calls read the same to anyone who
# knows Python; Depends() only means something if you already know
# FastAPI's DI model. Not needed for testability either — tests
# monkeypatch get_firestore_client()/get_gateway_api_key() directly, which works
# identically either way.
```

**Issuer is the v1 endpoint format (`sts.windows.net`), not v2** — Power
Platform's "Azure Active Directory" connector auth provider issues v1
tokens regardless of anything configured on our side, confirmed empirically
(2026-09-16) against a real token. JWKS signing keys are shared across
v1/v2, so the discovery URL is unaffected.

The API key proves *knows a shared secret*; the JWT proves *is this person,
right now* (~1h expiry, revocable per account). `claims["oid"]` feeds
telemetry. `jwt.decode()` is local cryptography — `PyJWKClient` caches
Entra's public keys, but only for 5 minutes (its default `lifespan`), so
it re-fetches on that schedule too, not just on an actual key rotation.
Wider chain: `docs/auth.md`.

That re-fetch is a blocking call (no async JWKS client in PyJWT) —
accepted as-is, roughly every 5 minutes on a warm instance, not just cold
starts or real rotations.

**Deviation from the snippet above:** don't call `get_secret(...)` inline on
every request as shown — wrap it (and any other module-level GCP client,
e.g. Firestore) in an `@lru_cache`-decorated getter instead, called lazily
on first use. Two reasons: it avoids a live Secret Manager round-trip per
request, and — the real forcing reason — `firestore.AsyncClient()`
constructed eagerly at import time raises `DefaultCredentialsError` with no
credentials configured, which breaks importing the module at all under
Layer 1 (`docs/testing.md`).

**Caching `gateway-api-key` means a rotation needs a redeploy to take effect** — decided, not a gap; no TTL-based refresh.

**`--allow-unauthenticated` on deploy is required.** Cloud Run's IAM invoker
check needs a Google-issued token, which Power Platform can't present — the
gate is the code above.

### Ownership — the token says who you are, not which conversations are yours

Auth validates identity on every request, but a `conversation_id` is just a
string in a URL — nothing stops a caller sending someone else's. **Every
endpoint taking one verifies the token's `oid` matches the stored
`user_id`** before returning or acting: `POST /ask`, `GET /ask/status`,
`POST /ask/cancel`.

```python
async def assert_owns_conversation(conversation_id: str, claims: dict) -> None:
    snap = await db.collection("sessions").document(conversation_id).get()
    if not snap.exists or snap.to_dict().get("user_id") != claims["oid"]:
        raise HTTPException(404, "Conversation not found.")
```

**404, not 403.** A 403 confirms the conversation exists, which leaks
information to anyone probing IDs. 404 is indistinguishable from a made-up
ID.

No schema change — `sessions` already stores `user_id`, and
`POST /conversation` writes it at creation, so every conversation has an
owner before any turn runs.

## The endpoints

| Endpoint | Purpose | Blocking? |
|---|---|---|
| `POST /conversation` | Mint a `conversation_id`, create its `sessions` doc | No — milliseconds |
| `POST /ask` | Run one turn end to end, **or** resume/resolve a paused one (`approval_decision`); returns `AgentResponse` | **Yes** — the long one (rejection returns fast, no graph run) |
| `GET /ask/status/{conversation_id}` | Read the current status string and accumulated `thinking_log` | No — polled every 1–2s |
| `POST /ask/cancel/{conversation_id}` | Set the cancel flag (Phase 3) | No — returns immediately |

**Every endpoint except `POST /conversation` takes a `conversation_id` and
must check ownership** (see below). `POST /ask` and `GET /ask/status` run
*concurrently* — that pairing is why the ID can't come from `POST /ask`.

- `POST /conversation` — no body. Mints a UUID, creates
  `sessions/{conversation_id}` with `user_id` from the token's `oid`, empty
  `history_messages`, and `last_activity_at`. Returns `{conversation_id}`. No
  LLM, no tools — returns in milliseconds, and **must stay that way**: it's
  called from `App.OnStart`, which blocks the first screen (`docs/frontend.md`).

  **Separate from `POST /ask` because status polling runs concurrently with
  it** — an ID that only arrived with the answer would leave turn one
  unpollable.
- `POST /ask` — body `{question, image_base64, filter_context, active_page,
  conversation_id, approval_decision}`. **`approval_decision` is
  `"approved" | "rejected" | None`, mutually exclusive with `question` —
  exactly one of the two is set** (`AskRequest`'s own validator). A fresh
  question omits it; resolving a pending approval omits `question` instead.
  There is no separate respond endpoint — this is the return path for the
  approval card too (`docs/approval-workflow.md` has the full resume/reject
  flow). Returns the `AgentResponse` model **serialized as-is — snake_case,
  no field-name conversion** (`.claude/rules/orchestrator.md` is the schema
  of record).

  **Deliberately no camelCase translation.** Power Apps reads property names
  from the connector's OpenAPI schema, which FastAPI generates from the
  Pydantic model — so `answer_markdown` binds in a gallery exactly as
  `answerMarkdown` would. A rename layer buys nothing and adds a second list
  of field names to keep in sync; that drift already bit once, when a field
  was removed from the model but left in the conversion. One model, one
  shape, all the way to the screen.
- `GET /ask/status/{conversation_id}` — `{status: string | null, thinking_log: list[dict]}`,
  backs progress indication. `null` when no turn is running. Reads `live_turns/{conversation_id}` in Firestore (see below).
- `POST /ask/cancel/{conversation_id}` — Phase 3. No body; returns
  `{cancel_requested: true}` as soon as the flag is written. **Writes a flag,
  doesn't kill a task directly** — see the Firestore section below for why.
  The response confirms the write landed, not that the turn has stopped; the
  turn ends when the loop next checks (`.claude/rules/orchestrator.md`).

### Request validation — before any turn work starts

```python
MAX_QUESTION_CHARS = 2000  # generous for a real question, not a paste

if len(body.question) > MAX_QUESTION_CHARS:
    raise HTTPException(400, "Question too long — please shorten it.")
```

**Reject, don't truncate** — a silently shortened question could ask something
different from what the user meant. This never reaches `agent_telemetry` (no
turn started, so no row shape to fill); Cloud Run's request logs capture the
400 already (`.claude/rules/telemetry.md`).

## What arrives in a request

- Assembling the multimodal prompt — image block when present + the
  **authoritative** filter-context block. The image is layout/attention only,
  **never** a source of numeric values.
- **`filter_context` must include slicer state, which `getFilters()` does not
  return.** The client-side capture sequence is in `docs/frontend.md` — it's
  Power BI JavaScript running in Power Apps, not gateway code. What matters
  here: the payload shape is uniform (slicer selections are ordinary filter
  objects), so parsing needs no special-casing. A capture that skips slicers
  produces an *absent* value, not a wrong one, and the turn answers against
  the wrong scenario silently.

- **`active_page` is a separate field, not part of `filter_context`.** A page
  name isn't filterable — there's no table to target — so putting it among
  the filters would either invite the model to use it as one or force every
  consumer to special-case the entry that isn't like the others. Filters
  describe the *data*; the active page describes what the user is *looking
  at*. They feed different tools.
- **Pass them through to `run_dax_query` like any other filter.** Power BI
  treats a parameter-table filter identically to a dimensional one, so no
  special-casing is needed — but omitting one is *not* harmless: the
  disconnected table would fall back to its own default rather than the
  on-screen selection, returning a different scenario with no error and no
  visible sign anything was off.
## Conversation state — Firestore, never process memory

**Why this isn't optional.** Cloud Run runs **multiple concurrent instances**
of one container under ordinary load — each with its own separate memory — and
the load balancer routes each request to whichever is free. A Python dict
would be per-instance, so a `POST /ask` writing status on instance A and a
`GET /ask/status` poll landing on instance B would silently miss. This needs
nothing unusual to happen: just two requests in one conversation, routed
normally.

| Collection | Lifetime | Holds | Read by |
|---|---|---|---|
| `live_turns/{conversation_id}` | One in-flight turn; cleared at turn end | `status` (string), `thinking_log` (list of `{seq, text}`), `cancel_requested` (bool), `pending_approval` (**nested object, 12 fields — schema below**) | `GET /ask/status`, the cancel check, `POST /ask`'s approval-decision branch |
| `sessions/{conversation_id}` | Whole conversation; 30-day TTL | `user_id`, `history_messages`, `last_activity_at` | Prompt assembly, the ownership check |

**`pending_approval` is a field, not a third collection** — it has its own
schema and its own consumer, which makes it look like a peer of the other two
documents. It isn't. Keeping it inside `live_turns` is what preserves the
**wholesale clear at turn end**: one delete drops everything turn-scoped, so
"did we leak state?" is never a question. Splitting it would buy slightly
cheaper status polls (which don't need the approval blob) at the cost of two
writes at pause time and a two-step cleanup — not worth it at this scale.

Telemetry is **not** here — it's `agent_telemetry` in BigQuery, append-only,
never read by serving code (`.claude/rules/telemetry.md`).

**`sessions` retention is a declarative TTL policy, not code.** Firestore
deletes documents whose `last_activity_at` is 30+ days old — configured once
at setup (`local-dev-environment-setup.md` Step 13), same pattern as the chart
bucket's lifecycle rule. The app's only job is writing `last_activity_at` on
each turn, which it's already doing in the same write that trims the array.

**Two documents, split by lifecycle — not by convenience.**

```
live_turns/{conversation_id}          # scoped to ONE in-flight turn
{
  "status": "Querying the database...",   # written per node by the astream loop
  "thinking_log": [{"seq": 0, "text": "..."}],  # appended per agent-node round
                                                # that produces summarized thinking
  "cancel_requested": false,               # written by POST /ask/cancel
  "pending_approval": {...} | null         # PendingApproval, schema below;
                                           # written on hitting the cost threshold
}

sessions/{conversation_id}            # spans the whole conversation
{
  "user_id": "...",                        # claims["oid"] — same source as telemetry
  "history_messages": [                    # FIFO, last HISTORY_TURN_COUNT
    {
      "messages": [...],                   # messages_to_dict(state["messages"]), non-submit_answer
                                           # tool results already redacted
      "timestamp": ...
    }
  ],
  "last_activity_at": <timestamp>          # updated every turn; TTL target
}
```

## Conversation history — `sessions.history_messages`

**Each entry stores the turn's real message sequence, not a paraphrase.**
`messages_to_dict(state["messages"])` (`langchain_core.messages`) —
the `HumanMessage`, any `AIMessage(tool_calls=...)`/`ToolMessage` rounds, and
the final `AIMessage` — serialized as-is; `messages_from_dict` reconstructs it
at read time. Confirmed live: round-trips a real `tool_calls`-bearing
`AIMessage` and its `ToolMessage` cleanly. The model then sees genuine
`tool_use`/`tool_result` structure for prior turns instead of prose
describing them — nothing to imitate, no query text leaking into a new
answer's body — and it's still what makes follow-ups work: adapting a
working DAX/SQL query beats re-deriving one from schema chunks.

**Every tool call the turn made is stored, but every successful result
except `submit_answer`'s is redacted first — a deliberate broadening from an
earlier numeric-tools-only design, after a real transcript showed the model
mistaking stale prior-turn data for this turn's.** `build_updated_history`
(`app/gateway/entry_exit.py`) replaces a successful non-`submit_answer`
`ToolMessage`'s content with a fixed `STALE_RESULT_MESSAGE`, keeping its
`name`/`tool_call_id`/`status` intact:

```python
STALE_RESULT_MESSAGE = ("Result removed -- this tool call is from a prior turn, not this one. "
                         "Re-run it if you need this data now.")

def build_updated_history(history_messages: list[dict], state: AgentState) -> list[dict]:
    redacted = []
    for msg in state["messages"]:
        if isinstance(msg, ToolMessage) and msg.name != "submit_answer" and msg.status != "error":
            msg = ToolMessage(content=STALE_RESULT_MESSAGE, name=msg.name,
                               tool_call_id=msg.tool_call_id, status=msg.status)
        redacted.append(msg)
    new_entry = {"messages": messages_to_dict(redacted), "timestamp": datetime.now(timezone.utc)}
    return (history_messages + [new_entry])[-HISTORY_TURN_COUNT:]
```

**Includes `generate_chart`, deliberately — no per-tool exclusion.** A stale
`chart_url` sitting in history would look exactly as current as a real one;
redacting it the same way as every other result is simpler and safer than
relying on `resolve_chart_data`'s `ToolError` (`docs/chart-tool.md`) to catch
a replayed `source_ref` downstream. An error-status `ToolMessage` is left
untouched — it was never live data to begin with, so there's nothing stale
to remove.

**Retry-loop messages (`check_length`, `verify`) are kept too.** No
correctness risk: `verify_response` checks whatever numbers appear in *this*
turn's `answer_markdown` against *this* turn's tool pool regardless of what's
in history, so a resurfaced number from a rejected attempt fails verification
again on its own. A standard checkpointer would carry these forward anyway,
and seeing what didn't work last time is plausibly useful, not just inert.

**No row cap on stored tool results — redaction makes one unnecessary.** An
earlier design row-capped each `ToolCallRecord.result` before storing it
(`HISTORY_ROW_CAP`); that was never built, and once every non-`submit_answer`
result is replaced with a short fixed string instead, there's no real row
data left to cap. Document size is bounded by `HISTORY_TURN_COUNT` ×
a handful of fixed-size redaction strings, comfortably under Firestore's
1MB document cap.

**No separate answer truncation.** Redacted tool results are now small and
fixed-size, so there's no size pressure left for a character cap on the
final answer's text to relieve. Revisit if real turns show otherwise.

**No separate `filter_context` field.** The live `HumanMessage` each turn
already has the filter-context block folded into its content (see "What
arrives in a request" above) — `messages_to_dict` captures that verbatim, so
a parallel copy would just be the same data stored twice.

**`HISTORY_TURN_COUNT` (`app/config.py`, default 5)** governs both the FIFO
trim on write and how many turns get read back — tunable without a schema
change.

**No `question`/`answer` fields.** Both are `messages[0].content` /
`messages[-1].content` if ever needed — `agent_telemetry` already keeps a
cheap copy of the answer for that purpose (`.claude/rules/telemetry.md`).

**No turn ID.** Nothing looks up a single turn; the list is read whole, in
order. `timestamp` gives ordering and already serves the TTL logic.

**Written by the gateway's `run_agent_turn`, alongside `live_turns` cleanup —
not by `finalize`.** Matches the existing split: `finalize` only sets
`AgentState` fields and writes `agent_telemetry`
(`.claude/rules/orchestrator.md`).

**Reading `history_messages` back and including it in the prompt.** Storing
chat history is only half the job — it has to actually reach the model, or
a follow-up like *"what about last quarter?"* has no antecedent. **Fails
silently:** the Firestore write succeeds, the data looks right, and the
agent just answers as if every turn were the first. Read from
`sessions/{conversation_id}` at the start of each turn, before assembling
the prompt; append the completed turn after.

**Split into two functions, deliberately — Firestore I/O stays out of
`app/gateway/entry_exit.py`.** `fetch_history_messages` (`app/gateway/gateway.py`)
does the actual read; `build_history_messages` (`app/gateway/entry_exit.py`)
is a pure reconstruction over whatever list it's handed, no client, no
`conversation_id`. Same split as `write_history_messages`/`build_updated_history`
on the way out, and the same reason `app/orchestrator/state.py` stays free of
heavier imports (`.claude/rules/orchestrator.md`): either module can be
imported, and in `entry_exit.py`'s case unit-tested
(`tests/test_entry_exit.py`), with no live credentials.

```python
# app/gateway/gateway.py -- Firestore I/O
async def fetch_history_messages(conversation_id: str) -> list[dict]:
    snap = await get_firestore_client().collection("sessions").document(conversation_id).get()
    return snap.to_dict().get("history_messages", []) if snap.exists else []

# app/gateway/entry_exit.py -- pure reconstruction, no I/O
def build_history_messages(history_messages: list[dict]) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    for turn in history_messages:
        result.extend(messages_from_dict(turn["messages"]))
    return result
```

**The result seeds `AgentState.history_messages`, a separate field from
`messages`** — call it once, before `graph.astream(...)`, and pass it in as
part of the initial state: `{"history_messages": history, "messages": [...]}`.
Never merge it into `messages` itself (`.claude/rules/orchestrator.md`'s
`AgentState` — the reason is the write-back above: `messages` becomes one new
`history_messages` entry per turn, and a history-seeded `messages` would make
that entry recursively contain every prior turn too).

**Tell the model which results are still current**, in the system prompt
(`app/orchestrator/context.py`): every prior turn's tool result except
`submit_answer`'s carries the removal notice; any tool result without it is
from this turn. A prior turn's `submit_answer` numbers are visible but
explicitly not a live source — the model is told to re-run the call instead
of reusing them.

**The split exists because these two get cleared at opposite times.** All
three `live_turns` fields die together the moment a turn ends (normally, via
approval, or via cancel) — so wiping that document wholesale is safe.
`sessions` must *survive* that exact moment, growing turn by turn. Keeping
them physically separate makes turn-end cleanup a one-line delete with no risk
of erasing history. No single operation needs both documents at once, so
splitting costs no extra round trip.

## The approval handoff — `PendingApproval`

**`pending_approval` is a purpose-built handoff object, not a state dump.**
How it's produced and consumed — the batch cost gate, the full resume
sequence — is in `docs/approval-workflow.md`; this is the schema and the
carry rule. A resume arrives as a *new* `POST /ask` call — the graph run
that paused already ended, so `AgentState` is gone. Resume can't re-run the
agent loop (that could pick a different query than the human reviewed), so
everything it needs must be carried here explicitly. Defined in
`app/orchestrator/state.py`, built by `build_pending_approval()`
(`app/gateway/entry_exit.py`):

```python
from datetime import datetime
from typing import TypedDict
# ToolCallRecord: .claude/rules/orchestrator.md

class PendingApproval(TypedDict):
    conversation_id: str
    question: str                            # carried -- see rule below
    filter_context: list[dict]               # frozen copy — see rule below
    active_page: str | None                  # which page the user was on
    messages: list[dict]                     # messages_to_dict(state["messages"]),
                                             # redacted -- this turn so far, dangling
                                             # AIMessage included; rebuild_paused_messages
                                             # restores numeric results on resume
    pending_queries: list[dict]              # BigQuery — {id, query, estimated_bytes},
                                             # display/telemetry only, not load-bearing
    deferred_dax: list[dict]                 # vestigial -- never populated; see
                                             # docs/approval-workflow.md
    tool_calls: list[ToolCallRecord]         # same name and shape as
                                             # AgentState.tool_calls — seeds it
                                             # directly on resume, no translation
    iteration_count: int                     # carried — see rule below
    bytes_consumed: int                      # carried — see rule below
    estimated_cost: str
    paused_at: datetime                      # correlates this object to its
                                             # telemetry pause row, and dates
                                             # the pause when a response arrives
```

**What carries and what doesn't:** carry anything that bounds total turn
consumption or is needed to resume reasoning; reset anything scoped to a
single attempt; omit anything already recorded elsewhere.

- `iteration_count` and `bytes_consumed` **carry** — both are guardrail
  inputs. Resetting `bytes_consumed` would let a turn spend a full budget on
  each side of the pause, making approval a way *around* the absolute cap it
  belongs to. (The response row doesn't double-count this on top of the
  pause row — `.claude/rules/telemetry.md`'s `bytes_consumed_baseline`.)
- `verification_retry_count` and `length_retry_count` **reset** — they're
  scoped to one synthesis attempt, and post-approval synthesis runs against a
  different, larger result set.
- Token counts and `errors` are **omitted** — the pause row already records
  them (`.claude/rules/telemetry.md`); carrying them would double-count.
- `question` **is carried**, unlike the rest of that omitted group — there's
  no other way to recover it on resume (it's folded into the `HumanMessage`'s
  content, not stored separately), and the response row's telemetry needs it.

**Anything not in these twelve fields resets by construction.** That's the
safety property, not an oversight: `needs_approval` carried as `True` would
re-pause the resumed turn immediately, and outputs like `answer_markdown` or
`claims` haven't been produced yet at pause time.

**A minimal checkpointer, scoped to the one point that pauses.** Twelve
fields cover the one case that exists; LangGraph's `interrupt()` needs a
persistent checkpointer with no free GCP-native option. **No `code_version`
check on resume** — a pause outliving a deploy runs against whatever code is
live. Why, and the upgrade path: component reference §9.2a.

## Cancellation, cleanup, and retention

**Cancel is a signal, not stored state.** An `asyncio.Task` is running code in
one process's memory — there's nothing JSON-shaped to persist. So
`POST /ask/cancel` writes `cancel_requested: true`, and the **`astream` loop
checks that flag each iteration** and stops if set. Cooperative cancellation.
Cheap because that loop is already writing status to the same document every
iteration — one more field read, not new plumbing.

**That flag is also checked deeper than this loop.** Breaking out here stops
the gateway advancing, but doesn't kill an already-dispatched BigQuery job —
`run_bigquery_sql` races the query against a cancel watcher and calls
`client.cancel_job()` itself, raising `TurnCancelledError`
(`.claude/rules/orchestrator.md`). Real cancellation for BigQuery,
best-effort for DAX.

**Clear `status` on every exit, not just success** — including error paths. A
poll arriving just after a turn ends would otherwise show a stale status from
a turn that already finished.

**One turn per conversation, enforced only in the UI.** Every turn-scoped
field is keyed on `conversation_id` alone, so two concurrent turns would
interleave statuses and overwrite each other's `pending_approval`. Power Apps
disables the send button while a turn is in flight, and again while a pending
approval's card is showing (`docs/frontend.md`); no server-side guard. A
normal question slipping through anyway degrades safely regardless --
`docs/approval-workflow.md`.

**`user_id` is stored but no logic uses it yet** — deliberately. It makes a
future "this user's recent conversations" query possible (`where user_id == X`)
without committing to building it. The document stays keyed by
`conversation_id`; `user_id` rides along as a field.

**`get_bigquery_schema()`'s process-lifetime `@lru_cache` (no TTL —
`.claude/rules/tools.md`) and Entra's cached JWKS keys stay per-instance, not
in Firestore.** Both are benign despite being
per-instance — each instance just fetches its own copy — so moving them
would trade a harmless redundant fetch for real Firestore traffic with no
actual benefit.

## Live status during a turn

**`POST /ask` runs the turn; `GET /ask/status/{conversation_id}` reads what it writes.**
Power Apps polls the status route about once a second while `POST /ask` is pending.
`POST /ask` itself is one ordinary long request.

**Consume the graph with `astream` and `version="v2"`.** Each chunk is
`{"type": ..., "data": ...}`. `values` chunks carry the full state, and the last one
is the final state. `updates` chunks carry `{node: returned_update}` for the node that
just finished, so each status describes a step that already happened.

**`consume_graph(initial_state)` writes the status**, in `app/gateway/gateway.py`:
- **Start:** creates `live_turns/{conversation_id}` with `status: "Thinking..."` and `thinking_log: []`.
- **`agent`:** `"Thought"`. Any thinking text in the reply is appended to `thinking_log`
  as `{seq, text}` via `ArrayUnion`. `seq` is a per-turn counter, so the front end can
  show only entries it hasn't seen. Adaptive thinking means some rounds have none.
- **`call_tool`:** `call_tool_status(names)`, from the tools the batch dispatched.
  `submit_answer` is ignored. A chart in the batch gives `"Prepared the chart"`, a combine
  gives `"Combined results"`. Otherwise it names up to `STATUS_MAX_NAMED_SOURCES` (3,
  `app/config.py`) distinct sources, such as `"Gathered results from BigQuery, project
  documentation, and semantic model"`, or `"Gathered results from {n} sources"` above that.
  Source names come from `SOURCE_NAMES`.
- **`verify`:** `"Verified results"` only when verification passes. A rejected answer
  writes nothing, so the agent's `Thought` line stays up until the retry shows its own.
- **`finalize`:** `"Prepared your answer"` normally, `"Needs your approval"` when the
  turn just paused (`update.get("needs_approval")`) — otherwise a pause would show the
  same text as a real final answer, which read as misleadingly final in practice.
- **`route_entry`, `check_length`:** no write.

Each status is suffixed with the seconds since the previous write, for example
`"Gathered results from BigQuery (1.5s)"`. Silent nodes don't move that timer, so their time
rolls into the next step.

**The document is deleted when the turn ends, whether it succeeded or failed** (`finally`).
`live_turns` holds only turns that are running. The last few writes can land within a
tenth of a second of the delete, so the final statuses are best-effort: a poll may or may
not catch them. The answer is always in the `POST /ask` response.

**The whole stream runs inside `asyncio.wait_for(consume_graph(...), GATEWAY_TURN_TIMEOUT_SECONDS)`.**
On timeout or error, `POST /ask` returns a plain `AgentResponse`, not an HTTP error.

**`GET /ask/status/{conversation_id}`** validates the key and token, checks ownership
with `assert_owns_conversation`, and returns `{status, thinking_log}` from
`live_turns`. With no document it returns `{status: null, thinking_log: []}`.
`read_live_status(conversation_id)` is the same read, without auth, for the notebook.

**Deferred:** cancellation (item 15) extends this section when it lands --
the approval states (item 14) are built; see `docs/approval-workflow.md`.

## Returning a response — errors and what the caller gets

**Markdown → HTML via `mistune` with a custom renderer using inline styles** —
a `<style>` block isn't reliably honored by the Power Apps HTML control.
**Override the renderer's fenced-code method too, not just prose elements.**
The agent writes ```` ```python ````/```` ```sql ```` blocks when answering
code questions; left to default `<pre><code>` tags they'd hit the same
no-inline-styles failure already solved for `pending_query` — reuse that exact
styling, inline `font-family` monospace plus explicit `white-space: pre-wrap`.
Same 16,384-char control limit applies; the answer-length guardrail already
covers it.

**Return a normal `AgentResponse` wherever possible, not an HTTP error.** Power
Apps has one response-handling path; an HTTP error needs separate UI code for
every failure mode. This is already the rule for timeouts — apply it to
unexpected failures too: catch, log, and return an `AgentResponse` whose
`answer_markdown` says plainly that something went wrong.

**HTTP errors are for pre-turn rejections only** — auth (401) and request
validation (400). Those happen *before* a turn exists, so there's no
`AgentResponse` to return and nothing meaningful to log to `agent_telemetry`.

**Open, not yet decided: what `POST /conversation` returns on an unexpected
internal error.** It has no `AgentResponse` to fall back to like `/ask`
does — confirmed live (2026-09-15) that an unhandled exception there
currently surfaces as a raw, generic `500`. Decide before this matters in
practice.

**Turns that end abnormally still write telemetry** (row schema:
`.claude/rules/telemetry.md`). A turn that times out,
errors mid-loop, or is cancelled has real `tool_calls` already completed —
often the most useful rows to inspect later. Write the row with whatever
completed, `success: False` on the failed call, and the relevant flag
(`cancelled`, `iteration_cap_hit`). The one exception is a crash that takes
the process down before the write; that's the untracked case, and it's
accepted rather than engineered around.

**This applies to error paths too** — see the status-clearing rule above; a
failed turn still needs `live_turns/{conversation_id}` cleared on the way out.

### Telemetry write — awaited, never backgrounded

Row schema, field-by-field population, and the two-row approval model are in
`.claude/rules/telemetry.md`. What matters here is *when* the write happens.

Cloud Run's default request-based billing throttles CPU the instant a response
is sent, so a background write freezes silently and is lost if the instance
scales to zero. A streaming insert is <100ms against 5–15s turns.
