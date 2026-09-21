---
paths:
  - 'app/gateway/**'
  - 'app/config.py'
  - 'tests/test_gateway_auth.py'
  - 'tests/test_static_context.py'
  - 'tests/test_conversation_state.py'
  - 'tests/test_approval_e2e.py'
---

# Gateway: auth, API surface, response formatting

> **Deep dives:** `docs/approval-workflow.md` before building
> `/ask/respond`. `docs/auth.md` for the full request-time auth chain,
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
# monkeypatch get_db()/get_gateway_api_key() directly, which works
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
`POST /ask/respond`, `POST /ask/cancel`.

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
| `POST /ask` | Run one turn end to end; returns `AgentResponse` | **Yes** — the long one |
| `GET /ask/status/{conversation_id}` | Read the current status string | No — polled every 1–2s |
| `POST /ask/respond/{conversation_id}` | Approve or reject a paused query | Yes — resumes the turn |
| `POST /ask/cancel/{conversation_id}` | Set the cancel flag (Phase 3) | No — returns immediately |

**Every endpoint except `POST /conversation` takes a `conversation_id` and
must check ownership** (see below). `POST /ask` and `GET /ask/status` run
*concurrently* — that pairing is why the ID can't come from `POST /ask`.

- `POST /conversation` — no body. Mints a UUID, creates
  `sessions/{conversation_id}` with `user_id` from the token's `oid`, empty
  `recent_messages`, and `last_activity_at`. Returns `{conversation_id}`. No
  LLM, no tools — returns in milliseconds, and **must stay that way**: it's
  called from `App.OnStart`, which blocks the first screen (`docs/frontend.md`).

  **Separate from `POST /ask` because status polling runs concurrently with
  it** — an ID that only arrived with the answer would leave turn one
  unpollable.
- `POST /ask` — body `{question, image_base64, filter_context, active_page,
  conversation_id}`. Returns the `AgentResponse` model **serialized as-is —
  snake_case, no field-name conversion**
  (`.claude/rules/orchestrator.md` is the schema of record).

  **Deliberately no camelCase translation.** Power Apps reads property names
  from the connector's OpenAPI schema, which FastAPI generates from the
  Pydantic model — so `answer_markdown` binds in a gallery exactly as
  `answerMarkdown` would. A rename layer buys nothing and adds a second list
  of field names to keep in sync; that drift already bit once, when a field
  was removed from the model but left in the conversion. One model, one
  shape, all the way to the screen.
- `GET /ask/status/{conversation_id}` — `{status: string}`, backs progress
  indication. Reads `live_turns/{conversation_id}` in Firestore (see below).
- `POST /ask/respond/{conversation_id}` — body `{decision: "approve" | "reject"}`.
  The return path for the approval card. **A new request, not a resumed one**
  — the original `/ask` already returned. Reads the cached `pending_approval`
  from `live_turns/{conversation_id}` (see `.claude/rules/orchestrator.md` for
  the full resume flow) and returns a normal `AgentResponse`.
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
| `live_turns/{conversation_id}` | One in-flight turn; cleared at turn end | `status` (string), `cancel_requested` (bool), `pending_approval` (**nested object, 10 fields — schema below**) | `GET /ask/status`, the cancel check, `POST /ask/respond` |
| `sessions/{conversation_id}` | Whole conversation; 30-day TTL | `user_id`, `recent_messages`, `last_activity_at` | Prompt assembly, the ownership check |

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
  "cancel_requested": false,               # written by POST /ask/cancel
  "pending_approval": {...} | null         # PendingApproval, schema below;
                                           # written on hitting the cost threshold
}

sessions/{conversation_id}            # spans the whole conversation
{
  "user_id": "...",                        # claims["oid"] — same source as telemetry
  "recent_messages": [                     # FIFO, last 5
    {
      "question": "...",
      "answer": "...",                     # truncated ~500 chars, sentence boundary
      "queries": ["EVALUATE ...", "SELECT ..."],
      "filter_context": [...],
      "timestamp": ...
    }
  ],
  "last_activity_at": <timestamp>          # updated every turn; TTL target
}
```

## Conversation history — `sessions.recent_messages`

**Queries, not results — and no `tool_calls` at all.** A turn entry holds the
question, a truncated answer, the query text, and `filter_context`. Full
`tool_calls` with raw results live only in `AgentState` (one turn, in memory)
and `PendingApproval` (only while paused, cleared at turn end) — never here,
where entries persist. A stored query never goes stale: the SQL that computed
last quarter's recall is still correct SQL after a data refresh or a slicer
change; only its *output* was time-sensitive. So no freshness bound, no
result-invalidation rule, and no change to the verification contract. Queries
also help where the agent struggles most: adapting a working DAX query beats
re-deriving one from schema chunks.

**`filter_context` is required alongside them, not optional.** A query
composed under `Region = West` is silently wrong if adapted blind under
today's slicers. It also fixes follow-up interpretation — *"what about
East?"* only parses if the prior turn's filters are visible.

**Five turns, not ten** — each entry carries more now, and follow-ups rarely
reach past the last turn or two. A judgment call, tunable once real
conversations exist.

**No turn ID.** Nothing looks up a single turn; the list is read whole, in
order. `timestamp` gives ordering and already serves the TTL logic.

**Truncate the stored answer with `pysbd`, not a character cut or a regex.**
A hard cut can slice `0.367` into `0.36` — a *wrong* number in history. A
`[.!?]\s` regex breaks on abbreviations (*"Approx. 0.367"* → *"Approx."*).
`pysbd` is pure Python, no model download.

```python
import pysbd
_seg = pysbd.Segmenter(language="en", clean=False)

def truncate_at_sentence(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    out = ""
    for sentence in _seg.segment(text):
        if len(out) + len(sentence) > limit:
            break
        out += sentence
    return (out or text[:limit]).rstrip() + " […]"
```

**Truncate in `finalize`, when writing to Firestore — nowhere upstream.**
`answer_markdown` stays full in the response and in telemetry; only this copy
is shortened. The 6,000-char answer-length guardrail is unrelated — that's a
Power Apps rendering ceiling, not a storage cost.

**Reading `recent_messages` back and including it in the prompt.** Storing
chat history is only half the job — it has to actually reach the model, or
a follow-up like *"what about last quarter?"* has no antecedent. **Fails
silently:** the Firestore write succeeds, the data looks right, and the
agent just answers as if every turn were the first. Read from
`sessions/{conversation_id}` at the start of each turn, before assembling
the prompt; append the completed turn after.

**`queries` is derived from `tool_calls` at write time, not accumulated
separately — and not at read time.** The query is already in each record's
`args`, and `name` distinguishes SQL from DAX, so a parallel list would be a
second copy that can drift. Deriving at *read* time isn't an option: it would
mean persisting `tool_calls` — raw results included — in a document that grows
permanently. Deriving here and discarding the source is what keeps `sessions`
small:

```python
queries = [tc["args"].get("query") or tc["args"].get("dax")
           for tc in state["tool_calls"]
           if tc["name"] in ("run_bigquery_sql", "run_dax_query")]
```

**Firestore stores dicts; the model reads messages** — convert at read time:

```python
async def build_history_messages(conversation_id: str) -> list[BaseMessage]:
    session = await get_session(conversation_id)
    messages = []
    for turn in session.get("recent_messages", []):
        messages.append(HumanMessage(content=turn["question"]))
        content = turn["answer"]
        if turn.get("queries"):
            content += "\n\nQueries run:\n" + "\n".join(turn["queries"])
        if turn.get("filter_context"):
            content += f"\n\nFilters active: {turn['filter_context']}"
        messages.append(AIMessage(content=content))
    return messages
```

**Queries and filters go in the `AIMessage` content, not a separate state
field.** The model only reads the message list, so a parallel field would
need this same formatting step anyway — and keeping them in the message
preserves which query belongs to which answer, with no re-linking by index.

**Tell the model what they're for**, in the system prompt: prior queries are
patterns to adapt, not results to cite. It still runs whatever it composes.

**The split exists because these two get cleared at opposite times.** All
three `live_turns` fields die together the moment a turn ends (normally, via
approval, or via cancel) — so wiping that document wholesale is safe.
`sessions` must *survive* that exact moment, growing turn by turn. Keeping
them physically separate makes turn-end cleanup a one-line delete with no risk
of erasing history. No single operation needs both documents at once, so
splitting costs no extra round trip.

## The approval handoff — `PendingApproval`

**`pending_approval` is a purpose-built handoff object, not a state dump.**
How it's produced and consumed — the batch cost gate, deferred DAX, the full
resume sequence — is in `docs/approval-workflow.md`; this is the schema and
the carry rule. `POST /ask/respond` is a *new request* — the graph run that paused already
ended, so `AgentState` is gone. Resume can't re-run the agent loop (that
could pick a different query than the human reviewed), so everything it needs
must be carried here explicitly:

```python
from datetime import datetime
from typing import TypedDict
# ToolCallRecord: .claude/rules/orchestrator.md

class PendingApproval(TypedDict):
    conversation_id: str
    question: str                            # for the resumed reasoning
    filter_context: list[dict]               # same
    active_page: str | None                  # which page the user was on
    conversation_history: list[dict]         # same
    pending_queries: list[str]               # BigQuery — priced, awaiting approval
    deferred_dax: list[str]                  # DAX — deferred so its results
                                             # never touch Firestore; runs on approve
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
  belongs to.
- `verification_retry_count` and `length_retry_count` **reset** — they're
  scoped to one synthesis attempt, and post-approval synthesis runs against a
  different, larger result set.
- Token counts and `errors` are **omitted** — the pause row already records
  them (`.claude/rules/telemetry.md`); carrying them would double-count.

**Anything not in the twelve fields resets by construction.** That's the
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
disables the send button while a turn is in flight (`docs/frontend.md`); no
server-side guard.

**`user_id` is stored but no logic uses it yet** — deliberately. It makes a
future "this user's recent conversations" query possible (`where user_id == X`)
without committing to building it. The document stays keyed by
`conversation_id`; `user_id` rides along as a field.

**The `bigquery_schema` TTL cache and Entra's cached JWKS keys stay
per-instance, not in Firestore.** Both are benign despite being
per-instance — each instance just fetches its own copy — so moving them
would trade a harmless redundant fetch for real Firestore traffic with no
actual benefit.

## Live status during a turn

**Applies to the two endpoints that run the graph — `POST /ask` and
`POST /ask/respond`.** `GET /ask/status` only reads what they write;
`POST /ask/cancel` and `POST /conversation` never touch the graph.

**Consume with `astream`, never `ainvoke` — build it in from the start.**
Retrofitting invocation style across a working loop is avoidable churn.
`astream` yields after every node; `ainvoke` blocks until the end and gives
you nothing to report progress from. Node names come from the graph in
`.claude/rules/orchestrator.md`; the status mapping below must track it.

**Two concurrent HTTP calls, not one streaming call** — this is what lets
"no streaming to the client" and "live status" both be true:
- `POST /ask` — one ordinary long request. Internally consumes an async
  generator; externally indistinguishable from any blocking call.
- `GET /ask/status/{conversation_id}` — many short polls from a Power Apps `Timer`,
  reading the Firestore document the first call is concurrently writing to.

Power Apps never needs websockets, SSE, or streaming support.

```python
# Serves POST /ask — returns AgentResponse to that caller. The set_status()
# calls are a SIDE EFFECT for a different endpoint: GET /ask/status polls the
# Firestore document these writes update. One function, two consumers.
async def run_agent_turn(question: str, conversation_id: str) -> AgentResponse:
    # Write BEFORE invoking: on a resumed turn, live_turns may still hold
    # content from the paused turn, and a poll could land before the first node.
    set_status(conversation_id, "Starting...")

    # Both modes are needed: "updates" carries node names (for status), "values"
    # carries full state — the only way to get the final state back, since the
    # loop IS the return path. version="v2" is the unified StreamPart shape
    # (LangGraph >= 1.1); without it you get the legacy bare {node: update}.
    final_state = None
    async for chunk in graph.astream(
        {"question": question},
        stream_mode=["updates", "values"], version="v2", ...
    ):
        if chunk["type"] == "values":
            final_state = chunk["data"]        # last one wins = final state
            continue
        if chunk["type"] != "updates":
            continue
        node = next(iter(chunk["data"]))
        if node == "call_tool":
            # A batch may dispatch SEVERAL tools concurrently — this is a list,
            # not a single value. Assuming one silently breaks compound questions.
            tools = chunk["data"][node]["dispatched_tools"]
            if len(tools) == 1:
                set_status(conversation_id,
                           f"{FRIENDLY_TOOL_NAMES.get(tools[0], 'Working')}...")
            else:
                set_status(conversation_id,
                           f"Gathering information from {len(tools)} sources...")
        elif node == "execute_approved":
            set_status(conversation_id, "Running the approved query...")
        elif node == "agent":
            set_status(conversation_id, "Thinking...")
        elif node == "verify":
            set_status(conversation_id, "Verifying results...")
        elif node == "finalize":
            set_status(conversation_id, "Preparing your answer...")
        # route_entry and check_length write nothing — both are sub-millisecond,
        # and Firestore holds the last value, so a poll during them reads
        # stale-but-accurate rather than blank.
    return build_agent_response(final_state)
```

**Enforce the ~90s timeout by wrapping this whole loop in `asyncio.wait_for(...)`**,
not `ainvoke` — the `astream` consumption above already has to exist for
status reporting, so the timeout wraps it rather than needing a second
invocation path. On timeout, return a normal `AgentResponse`, not an HTTP
error — Power Apps needs no new UI code for it. Cancellation propagation
details are in `.claude/rules/orchestrator.md`.

**Starting point, deliberately flexible** — tune the granularity once you see
real turns. Not a contract:

| Stage | Status string |
|---|---|
| Reasoning, pre-tool | *(none — the always-visible thinking icon covers it)* |
| Dispatching **one** tool | `f"{FRIENDLY_TOOL_NAMES[tool]}..."` |
| Dispatching **several** (batch) | `f"Gathering information from {n} sources..."` |
| Verification running | `"Verifying results..."` |
| Verification retry | `"Double-checking the answer..."` |
| Final synthesis | `"Preparing your answer..."` |

```python
FRIENDLY_TOOL_NAMES = {
    "run_bigquery_sql": "Querying the database",
    "run_dax_query":    "Querying the semantic model",
    "search_docs":      "Searching project documentation",
    "get_page_info":    "Reading the dashboard page info",
    "get_measure_dax":  "Looking up a measure",
    "list_repo_files":  "Looking through the code",
    "read_repo_file":   "Reading the code",
    "generate_chart":   "Generating a chart",
}
```

**Batches get a generic count, not a list of names.** Joining friendly names
("Querying the database and Searching documentation and...") needs real grammar
handling past two items for no real gain. One honest line covers it.

**Retry wording is abstracted on purpose** — `"Double-checking the answer"`,
not `"retrying failed verification"`. Internal mechanics aren't the user's
concern.

**The approval pause needs no status string.** When a query trips the cost
threshold, `/ask` returns immediately with the approval card — polling covers
the *waiting* period, and the waiting is over. Status polling and the approval
card are two different UI states, not one flowing into the other.

**`set_status` writes to `live_turns/{conversation_id}`** — the same Firestore
document backing `/ask/status`, which also holds `pending_approval` and the
`cancel_requested` flag. Not process memory, and not an asyncio `Task`: cancel
is a polled flag (see above).

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
