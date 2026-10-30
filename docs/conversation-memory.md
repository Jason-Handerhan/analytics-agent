# Conversation memory: findings ledger, compaction, recall, search

**Status (2026-10-29): built and live-verified** — the findings ledger, the
recall tool `get_conversation_findings`, per-turn tags (`title`, `topics`,
`entities`), compaction (`app/orchestrator/compaction.py`, the
`turn_range_summaries` table, the gateway pre-step, the telemetry fields), embeddings
at write time, and the search tool `search_conversation_findings`. **Designed, not
built:** the recall tool's summary-aware versions (overview as a table of contents).
Not yet redeployed. Update this line and `docs/build-order.md` as steps land.

Goal: a long EDA conversation can be summarized, searched, and kept within a
healthy prompt size. `sessions.history_messages` keeps turns with tool results
redacted and, with compaction folding older turns into a summary, so without a
ledger "summarize what we found" sees only the newest turns.

## Design

- **Code** lives in `app/orchestrator/conversation_memory/` (`schema.py`,
  `create_table.py`, `writer.py`, `reader.py`), next to its only callers —
  same shape as `app/gateway/chat_history/`. The tool and its helpers are in
  `app/orchestrator/tools.py` with the other local tools. Compaction is a plain
  async function in `app/orchestrator/compaction.py`.
- **Two BigQuery tables** in `agent_chat_history`: `conversation_findings`
  (built) and `turn_range_summaries` (schema, create script and writer built; filled by compaction). `turns` (the UI restore
  table) is not used or changed. Table names are `app/config.py` constants.
- **Everything is scoped to one conversation**, by the `conversation_id` the
  orchestrator injects. No `user_id` filter, no soft-delete handling —
  ownership is checked at `/ask`, and a deleted conversation can't receive
  messages. Revisit only if search goes cross-conversation.
- **Prompt memory** (compaction) and **long-term memory** (the ledger) are
  separate: the compaction summary is lossy working memory in the prompt; the
  ledger is the lossless source the recall tool reads.
- **A cancelled turn is discarded** (`finalize` always returns
  `CANCELLED_MESSAGE`): no history entry, no ledger row. So the ledger holds a
  subset of history's turns — history also keeps unverified turns, the ledger
  does not. Ledger turn numbers are therefore not positions in the model's
  message history. Clarifying-question turns stay in the ledger: they are real
  turns and give the agent context for what happened.
- **Standing context** (definitions the user set, a settled scope, the goal of
  the analysis) is a different kind of memory — deliberate, not tied to turns,
  possibly cross-conversation — and is not part of compaction. A future
  feature: agent-written notes through a small tool, with a short pinned block
  in the prompt, separate from the summaries.

## Verification contract extension

`MEMORY_SOURCE_TOOLS` (`app/config.py`) is a second branch in
`build_numeric_pool`, beside `NUMERIC_SOURCE_TOOLS`. For these tools the pool
takes **only the `verified_numbers` of each returned turn**
(`extract_verified_numbers`) — every other number in the result (`turn`,
`approx_tokens`, a decoded `args`) is ignored. Those numbers already passed
verification in an earlier turn and were written by gateway code the model
can't touch, so every number still traces to a live tool call.

- A recall is the only way to quote an earlier number: numbers in message
  history never enter the pool (it is built from this turn's tool calls only),
  and history's tool results are redacted.
- The overview view returns a dict, so `extract_verified_numbers` returns `[]`
  for it. Overview numbers (counts, token sizes) can't be cited. The extractor
  handles a flat list of turns or hits; a dict result (the overview, the tags view)
  returns `[]`.
- A turn-range summary is context only, not a numeric source. Its text never
  enters the pool, so a figure in it can't be cited; the label on the stored
  summary says so and points at `get_conversation_findings`, whose raw turns
  carry the verified numbers. The summary row has no `verified_numbers`, and the
  summarizer's output is not checked against the ledger.
- Recall and search results are not in `NUMERIC_SOURCE_TOOLS`, so they stay out of
  the chartable and combinable paths.
- `search_conversation_findings` is in `MEMORY_SOURCE_TOOLS`; with `scope="user"` it
  can return numbers verified in other conversations, on the same terms.
- Derived numbers (differences, rankings) are still rejected; a summary quotes
  findings, it doesn't compute.
- Known limitation, as before: matching is set-based, so it can't catch a real
  number attached to the wrong label. `docs/llm-judge.md` is the backstop.

## Tables

**`conversation_findings`** — one row per verified turn, written in
`finalize_node` when `verified and not cancelled`, concurrently with the
telemetry write (`asyncio.gather`; awaited, never backgrounded — Cloud Run
throttles CPU after the response). The writer logs its own failure instead of
raising, so a ledger gap never costs the user an answer. Clustered on
`user_id`, `conversation_id`; written as `turn-writer-sa` (data editor on the
dataset). Schema: `schema.py`; create with
`uv run python -m app.orchestrator.conversation_memory.create_table`. A new
nullable column is added in place with `ALTER TABLE ... ADD COLUMN` (the tag
columns were); a removed nested field means drop and recreate.

| Column | Notes |
|---|---|
| `conversation_id`, `user_id` | `user_id` for clustering and future cross-conversation search |
| `answered_at` | Order; turn numbers come from `ROW_NUMBER()` at read |
| `question` | The real question (`state["question"]`, not "Approved") |
| `answer_markdown` | As the user saw it, signed chart URLs included. Those URLs are valid for `CHART_URL_EXPIRATION_HOURS` (1 week); a URL's position in the markdown is what tells the model which chart it is, so it is left raw |
| `verified_numbers` | Repeated FLOAT64: `all_prose_numeric_claims` + `extract_table_values(answer_markdown)`, computed by `finalize` (the helper lives in `orchestrator.py`, which imports this package) |
| `approx_tokens` | `(len(question) + len(answer_markdown) + len(json.dumps(verified_numbers))) // 4`, computed in `build_findings_row`. Tool calls and tags are not counted — the tool returns tool calls only with `include_tool_calls`, and tags are tens of tokens |
| `filter_context` (JSON string), `active_page` | A finding means little without the filters it was made under |
| `sources` | Repeated STRING, from `build_sources` |
| `title`, `topics`, `entities` | Per-turn tags, written by the answering model (see below). `title` NULL and the lists empty for rows written before the tags existed |
| `tool_calls` | Repeated STRUCT `{name, args, ref_id}`: successful calls only, no `submit_answer`, no results. `args` is the full args as a JSON string (the SQL stays in it); `ref_id` keeps a chart's `source_ref` link |
| `embedding`, `embedding_model`, `embedded_text` | Built, live-verified 2026-10-29. `write_findings_row` embeds `embedded_text` (title, topics, entities, question and answer, chart links reduced to their label) with `gemini-embedding-001`, `RETRIEVAL_DOCUMENT`, default dimension. A failed embedding is logged and the row is written with an empty vector, so search skips it. Rows written before this have no vector |

**`turn_range_summaries`** (schema, create script and writer built; filled by compaction) — non-overlapping chunks, one per
compaction, so the ledger never holds summaries of summaries.

`conversation_id`, `user_id`, `covers_from`, `covers_to` (`answered_at` bounds
of the folded ledger rows), `first_turn` and `last_turn` (the ledger turn
numbers it covers, usable as `start_turn` and `end_turn`), `title`, `summary_markdown`, `topics`
(repeated STRING), `approx_tokens`, `created_at`.

## Per-turn tags (built)

`title`, `topics` and `entities` are optional fields on `submit_answer`
(`SubmitAnswerArgs`), written by the model that just wrote the answer — full
turn in view, no extra call, no extra latency. They flow
`submit_answer` → `AgentState` → `build_findings_row` → three columns on
`conversation_findings`. `title` is a short label, `topics` short noun phrases,
`entities` the specific names the answer is about, as they appear in the data;
all without numbers. They are strings, so they never reach the numeric pool.
A normalizer on the two lists strips whitespace, drops empties, dedupes in
order (case-sensitively) and caps each list at 6. Tags are kept on
clarifying-question turns too. The overview shows each turn's tags and doubles
as the tag vocabulary for search.

Not building derived columns (`tables_queried`, `has_chart`, `tools_used`): that
information is already in each row's `tool_calls`, `sources` and
`answer_markdown`, and a conversation's rows are few enough to scan.

## Recall tool — `get_conversation_findings`

One local tool in `tools.py` with two views; `conversation_id` is injected by
`dispatch_other_call` (not in the model's schema). Reads go through
`get_turn_writer_client()` with fixed parameterized SQL (`reader.py`) — the
model supplies only ints and a bool, and `agent-sa` (which runs
`run_bigquery_sql`) has no grant on this dataset, so the model's own SQL can't
reach other conversations' findings.

- **`view="overview"`**: totals (`total_turns`, `total_approx_tokens`),
  `token_budget`, `max_recent_turns_within_budget`, first/last `answered_at`,
  and one entry per turn (`turn`, `answered_at`, `approx_tokens`, `sources`,
  `title`, `topics`, `entities`, `question` truncated). No answer text; not
  citable.
- **`view="turns"`**: full rows (`turn`, `answered_at`, `question`,
  `answer_markdown`, `verified_numbers`, `sources`, `title`, `topics`,
  `entities`, `filter_context`, `active_page`). Select with `last_n`, or
  `start_turn` + `end_turn` (1-based, inclusive), or neither for every turn.
  `include_tool_calls` adds each call's `name`, decoded `args` and `ref_id`.
- **Argument rules** are a model validator on `GetConversationFindingsArgs`,
  branched on `view` (not `Field(ge=...)` — strict mode rejects `minimum`):
  overview takes no other arguments; for turns, `last_n` excludes a range, and
  a range needs both bounds, ordered and ≥ 1. Out-of-range against the
  conversation is checked in `resolve_turn_range`.
- **Budget**: `MEMORY_RECALL_TOKEN_BUDGET` (config, placeholder 20,000). The
  range is checked on `approx_tokens` from the overview rows before any text is
  fetched; with `include_tool_calls` the fetched size is re-checked. Over
  budget is an error that carries the overview, which includes how many of the
  newest turns fit. The budget is in the tool description and the overview.
- Badge "Searched Memory"; live status "Gathered results from memory".

## Compaction (built)

A plain async function, `compact_history_pipeline(conversation_id, user_id,
history_entries)` in `app/orchestrator/compaction.py`, called by the gateway in
`run_agent_turn` after the history fetch and before the graph, on non-resume
turns only. Not a graph node: a turn that timed out after an in-graph
compaction would lose it (history and the summary row are written on success), and the
retry would compact again. Run first and persisted before the answer starts,
compaction survives any outcome of the turn.

- **Interface**: JSON-able in and out — stored entry dicts in; `(entries,
  summary_row, result)` out. No Firestore and no gateway imports, so the
  gateway persists: `write_history_messages(...)` and `write_summary_row(...)`
  in one `asyncio.gather`, before the graph starts. This also keeps a later
  gateway/orchestrator container split to wrapping it in an endpoint.
- **Trigger**: either measure from `measure_history_size`, checked inline in the
  gateway on non-resume turns. The estimated tokens (characters / 4, of the
  content the prompt would carry, not stored metadata) exceed
  `COMPACTION_HISTORY_TOKEN_THRESHOLD`, or the stored bytes (the JSON size of
  the whole stored history, a proxy for its Firestore size) exceed
  `COMPACTION_HISTORY_MAX_BYTES`. History only: the static block is constant.
  No provider usage is read, so the estimate is the same on every provider, and
  the thresholds are tuned against those same measures. The gateway writes the
  status "Compacting..." before the pipeline runs.
- **Split**: a leading entry whose first message carries `summary: True` is
  the previous summary, not a turn. Every other stored entry is one turn
  (it starts at a `turn_start` marker). Keep the last `COMPACTION_KEEP_TURNS`
  turns; fold the rest. Skip if nothing would fold.
- **Boundaries**: the `turn_start` marker's value is the turn's start time (an
  ISO string, not `True`; still truthy, so the cache breakpoints are unchanged).
  The summary message carries `{"summary": True, "covers_to": <iso>,
  "last_turn": <n>}`.
- **Ledger range**: the folded turns are the ledger rows with `answered_at`
  after the previous `covers_to` (none if there is no summary) and before the
  first kept turn's start. Failed turns have no ledger row and are cut without
  a trace in the summary. An empty range is an error: it is logged and the history is left as it was.
  The summary's ledger turn numbers are carried forward, not recounted:
  `first_turn` is the previous summary's `last_turn` + 1 (1 for the first), and
  `last_turn` is `first_turn` + the number of rows summarized - 1. The model
  never sees or writes turn numbers.
  A conversation stored before the marker carried a timestamp (`True` instead)
  can't compact: reading the time fails, the pipeline logs it and carries on
  with the history as it is, until a new conversation replaces it.
- **Summarizer**: `COMPACTION_MODEL` (config, not in `AVAILABLE_MODELS`), a
  plain chat model, one structured call returning a `TurnRangeSummary`
  (`summary_markdown`, `title`, `topics`). Input is each row's title, tags,
  question and answer (as stored), then one deduplicated list of every
  verified number across the rows, and a time label per row, never a turn
  number. The output is one synthesis grouped by subject, not a turn-by-turn
  log; definitions, preferences and scope are the separate notes feature, not
  summary content. Findings are described in words; a figure appears only as
  the headline of a finding and only if it is in the verified list, but nothing
  is checked. Charts are named by their label, never copied as a URL; `title`
  and `topics` carry no numbers.
- **Persist**: only the latest summary is kept. The new summary entry replaces
  the previous one as the first stored entry: a user-role message labeled as a
  recap of verified turns `first_turn`-`last_turn` (not a live result, so its
  figures can't be cited), with the exact call for those turns:
  `get_conversation_findings` with `view="turns"`, `start_turn`, `end_turn`.
  The earlier summaries stay in the table for the recall tool.
- **Failure and time**: no separate timeout. Compaction runs under
  the gateway's turn deadline and the graph gets whatever
  remains. Compaction takes priority over the answer: if it uses the whole
  budget, the turn returns the normal timeout message and the user re-asks
  (it persists as soon as it finishes). An error (not a timeout) logs and
  returns the original history, so it can't fail a turn. A hung summarizer call
  would time out every turn until it recovers. A live status line
  ("Compacting...") covers the wait.
- **Telemetry** (`agent_telemetry`, four nullable columns, set on the initial
  state and written by `finalize`): `compacted`, `compaction_failed`,
  `compaction_prompt_tokens`, `compaction_completion_tokens`.

## Next versions of the recall tool (after turn-range summaries)

- **The overview becomes a table of contents**: one entry per summary (turn
  range, `title`, `topics`, size) plus per-turn lines for uncompacted turns.
- **A recall of a compacted range returns the covering summary by default**
  (text only; its figures can't be cited); raw rows only on request.
- **Turns still in the message history** return lean rows (`turn`, `question`,
  `verified_numbers`, no `answer_markdown`) — their text is already in context,
  but their numbers must still reach the pool. They are the turns after the
  latest summary's `covers_to`.
- **Fallback**: if a compacted range has no summary (the summary write failed),
  raw rows stay available, so no turn becomes unreachable.

## Search tool — `search_conversation_findings` (built, live-verified 2026-10-29)

- **Embedding**: `embedding.py` computes it in Python at write time, inside
  `write_findings_row`, which is already in `finalize`'s `gather`. Rows written
  before 2026-10-29, or whose embedding failed, have none and are skipped by
  search. Reads must never select the `embedding` column.
- **One local tool in `tools.py`**, flat args with a model validator (the
  recall tool's pattern, not a union): `mode` (`search` | `tags`), `scope`
  (`conversation` | `user`), `query`, `top_k` (default 5, max 10), `topics`,
  `entities`, `include_tool_calls`. `conversation_id` and `user_id` are injected
  by `dispatch_other_call` (`user_id` from the token, never the model).
- **`mode="search"`** embeds the query (`RETRIEVAL_QUERY`) and runs
  `COSINE_DISTANCE` over the rows that have an embedding. It returns a flat
  list of hits: the `view="turns"` fields plus `conversation_id`, `answered_at`
  and `distance`, with no turn numbers. No token-budget check: a hit is bounded
  by `MAX_ANSWER_CHARS` and the count by `top_k`.
- **`mode="tags"`** returns the most used topics and entities (300 of each,
  lowercased) with counts, so the agent filters with real wording.
- **`scope="user"`** searches all of the user's conversations, soft-deleted ones
  excluded (a delete only writes a marker; the findings rows stay). Hits carry
  their date and `filter_context`; the description tells the model to give the
  date, since the data may have changed and old chart links may have expired.
- **Tag filters** are case-insensitive substring matches, any of the given tags,
  `topics` and `entities` combined with AND, and never applied by default: a
  hard filter on free-text tags silently hides turns worded differently. The
  tool description says to filter only when unfiltered hits miss a named
  product, department or model.
- **Pool**: the tool is in `MEMORY_SOURCE_TOOLS`; only a hit's
  `verified_numbers` reach the pool (not `distance`), and the tags dict adds
  nothing. Numbers from other conversations were verified when their turn ran.
- **Errors**: an embedding failure and a BigQuery `GoogleAPICallError` become a
  `ToolError`, so a memory failure doesn't end the turn.
- **Binding**: bound outside Anthropic's strict mode with the recall tool
  (`ANTHROPIC_NON_STRICT_TOOLS`); strict schemas share a size limit on the
  compiled grammar and these two were the cheapest to give up
  (`.claude/rules/orchestrator.md`).
- **Fallback if testing shows misses on rare exact names**: add a lexical match
  on `entities` as an OR (similarity top-k plus exact matches) — it can only add
  hits. Build it only if the agent doesn't retry reliably.
- **Later**: embed summaries too and search them as a coarse index before
  drilling into a range's turns.

## Starting values (placeholders, tune)

| Constant | Start | Basis |
|---|---|---|
| `MEMORY_RECALL_TOKEN_BUDGET` | 20,000 | Built. A guess; tune from real conversations |
| `COMPACTION_HISTORY_TOKEN_THRESHOLD` | 8,000 (testing) | History only (the static block is constant), estimated as characters / 4. Low so a test conversation compacts early; tune it for production against the same estimate |
| `COMPACTION_HISTORY_MAX_BYTES` | 150,000 (testing) | Stored-history bytes (JSON size). Low for testing: a stored turn measured about 23 KB on average, so roughly 6-7 turns. Production is about 600,000 (about 25 heavy turns) |
| `COMPACTION_KEEP_TURNS` | 3 | Counts turns, not messages |
| `COMPACTION_MODEL` | `claude-haiku-5-5` | |

## History prompt caching (built 2026-10-26)

- Two Anthropic breakpoints, on the previous and newest stored turn
  (`mark_anthropic_history_cache`, found via the `turn_start` marker already in
  `build_human_message`). Details: `.claude/rules/orchestrator.md`.
- Needs byte-stable history: `build_history_messages` sorts map keys because
  Firestore returns them in varying order (`.claude/rules/gateway.md`).
- A full FIFO window defeats it. Compaction keeps the prefix stable between
  compactions, with one re-write per compaction; the summary entry is first, so
  it stays cached until the next fold.
- Cache lifetime (5 minutes vs 1 hour): staying at 5 minutes; decide at the end
  of this feature build.

## Build order

1. **Built:** findings table, row builder and writer, written in `finalize`.
2. **Built:** `get_conversation_findings` (overview and turns), the
   `MEMORY_SOURCE_TOOLS` pool branch, dispatch injection, badge and status.
3. **Built:** per-turn tags (`submit_answer` fields, columns, overview).
4. Compaction, each a check-in: config constants; turn_range_summaries table, schema
   and writer (built); the marker timestamp; the pure functions in `compaction.py`; the
   summarizer call; the pipeline function; gateway
   wiring (call, persist, status, remaining budget, safety cap); telemetry
   columns; live test and timing; consolidated tests and docs.
5. **Built:** embedding at write time, then `search_conversation_findings` (search and tags modes, conversation and user scope).
6. The recall tool's summary-aware versions: the overview as a table of contents, then `turns` returning a compacted range's summary.

## Unverified — check live

- OpenAI and Gemini ignore the `turn_start` key (the marker round-trips
  Firestore and Anthropic ignores it, confirmed live), and accept two user
  messages in a row (the summary, then the first kept turn's question).
- The recall tool's over-budget error as the model sees it (lower
  `MEMORY_RECALL_TOKEN_BUDGET` to force it).
- Streamed ledger rows are readable by the time compaction reads them (the
  folded turns are older than the kept turns, so they should be).
- Summarizer plus ledger-read latency, and whether compaction plus the graph's
  remaining budget stays under the Power Platform call ceiling (~120 s, itself
  unconfirmed).
- `COSINE_DISTANCE` works over streamed rows; the Gemini embedding package
  works as installed.
- A redeploy re-writes the prompt cache once (the tools block changed).

## Open

- Whether `approx_tokens` should include tool-call size (a SQL-computed
  `LENGTH(args)` in the overview query would make the pre-check exact with
  `include_tool_calls`). Revisit only if that flag is hit often.
- Prompt-cache lifetime, 5 minutes vs 1 hour: deferred.
- A system-instruction line on when to recall, only if the tool description
  proves insufficient.
- Whether the summaries table's `topics` and tags need a shared vocabulary note,
  once real tags have been looked at across several conversations.
- `test_gateway_timeout_cancellation_reaches_bigquery_job` fails intermittently;
  drop or fix later.
