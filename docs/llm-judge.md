# LLM-as-a-Judge Evaluation Pipeline

> **Not the same thing as the golden-dataset suite
> (`docs/golden-dataset.md`).** That one is hand-curated and event-triggered
> and checks whether the number is *correct*; this one is automated,
> scheduled, and checks whether an answer is *faithful to what it cited*.
> Deliberately separate mechanisms — don't merge them.

Two decoupled stages — neither runs inside the live-serving gateway.

**Stage 1 — telemetry logging.** Every turn, cheap, synchronous, built in
Phase 1. Full field list and the phase-by-phase what's-real-vs-empty
breakdown live in `.claude/rules/telemetry.md`.

**This doc is about Stage 2** — the judge that consumes what Stage 1 already
logs. Two things about Stage 1 matter for how the judge reads that data:
- **One `INSERT` per turn, not per-step tracing.** No independent spans, no
  separate tracing system — but real per-tool timing and sequence still
  exist, via `tool_calls` as a nested, repeated `RECORD` field within that
  one row, which is exactly what the judge queries against for
  faithfulness and per-tool latency.
- **The write must be awaited, not backgrounded** — Cloud Run's default
  request-based billing throttles CPU to near-zero the instant a response is
  sent, so a still-running background write freezes silently and can be lost
  entirely if the instance then scales to zero. A streaming insert is well
  under 100ms — awaiting it is a rounding error against 5–15s turns.

**Stage 2 — judging (scheduled, fully separate deployment).** A **Cloud Run
Job** (bills for full execution time, no request-based option — a different
product from Cloud Run *services*), triggered via **Cloud Scheduler**
(nightly is reasonable). Queries turns logged since the last run, judges each
against a judge LLM (can be a stronger/different model than the one serving
live traffic — latency/cost don't matter for offline batch), writes scores to
a separate `agent_eval_scores` table. Separate from the live gateway because
it scales on a completely different axis (log volume, not user traffic) and
a bug there should never be able to degrade live serving.

**What it scores — LLM-judged (semantic reasoning needed):**
- **Faithfulness** — numbers in the answer match real tool results this turn
  (the verification check, `.claude/rules/orchestrator.md`, scored as a
  metric here, not enforced as a gate).
  Checks internal consistency only — not whether the query itself was the
  right one; that's `docs/golden-dataset.md`, not this.
- **Narrative groundedness** — verification only hard-enforces *numeric*
  claims; a `search_docs`-sourced narrative explanation could be subtly wrong
  with zero numbers in it, uncaught by anything deterministic. This is the
  judgment the LLM layer is specifically for.
- **Compound-question completeness** — does the answer cover *every* part of a
  multi-part question, validating whether multi-tool-calling is actually
  earning its keep, not just capable of it.
- **Scope/refusal correctness** — does the agent correctly decline what it
  should (forecasts, excluded-table topics — `.claude/rules/orchestrator.md`, `docs/data-pipeline.md`).
  A different axis from "is the answer correct."
- **Chart-type appropriateness** — was the *type* chosen a good fit (vs.
  chart-data accuracy below, which checks the data, not the type choice).
- **Chart-data accuracy** — does the chart correctly reflect its data.
- **Tool-selection accuracy** and **rule adherence** (no-numbers-from-docs
  honored without a retry).

**What it scores — deterministic, straight from telemetry, no judge call:**
- **Iteration distribution, and exhaustion rate by tool** — `iteration_count`
  gives the percentile spread that sets the real `max_iterations`
  (`.claude/rules/orchestrator.md`); `iteration_cap_hit` gives the exhaustion
  rate, which answers the open item (§11) on whether DAX disproportionately
  burns the budget. Counts, not judgments, and possible as an aggregate across
  full history precisely because the job runs as a batch, not per-turn.
- **Latency and cost per turn** — flags inefficiency independent of
  correctness.

**Why re-check faithfulness here when it's already enforced live:** an
independent QA check on whether the *verification code itself* works, not
just the agent — same defense-in-depth principle as the budget alert under
the cost gates (`.claude/rules/orchestrator.md`).
