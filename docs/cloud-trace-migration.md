# Cloud Trace migration — replacing LangSmith (Phase 7, optional)

> **Scope: replaces LangSmith's role only.** `agent_telemetry`
> (`.claude/rules/telemetry.md`) is untouched — a schema hand-built for the
> LLM judge beats anything a generic tracer produces, and reconstructing
> judge-shaped data from a span tree is more work than what it replaces, not
> less. Decided 2026-09-17, after evaluating and rejecting a broader
> "replace `agent_telemetry` too" version of this plan.

## Why

LangSmith's traces — tool inputs/outputs, real `agent_safe` results — leave
GCP's IAM boundary for a third-party cloud (component reference §3, an
accepted trade-off if this is never built). Cloud Trace keeps the same data
inside GCP.

## Why Phase 7, not built alongside the graph

Both LangSmith and OpenInference work via auto-instrumentation — swapping one
for the other is close to a config change (different exporter, same
LangGraph code untouched), not a retrofit. Little cost to deferring; real
cost to absorbing unverified-endpoint and setup risk during Phase 3.

## Verified before building — don't re-trust the wrong parts

An AI-drafted blueprint for this got some things right and one thing wrong.
Confirmed independently, not taken on faith:

- **OTLP endpoint:** `telemetry.googleapis.com:443` — **not** `googleapis.com`
  (the wrong endpoint the original blueprint had; confirmed against Google's
  own docs).
- **IAM role:** `roles/telemetry.tracesWriter`, confirmed current via Cloud
  Trace's IAM docs. `roles/serviceusage.serviceUsageConsumer` is plausible
  but **unconfirmed** — check at build time.
- **Node-level fidelity — confirmed empirically, not assumed.** A local test
  (`openinference-instrumentation-langchain` + a toy `StateGraph`, run via
  `uv run --with`, no dependency added to the real project) produced real
  per-node spans with `langgraph_node`/`langgraph_step` metadata, and showed
  a node's ordinary return value (e.g. `{"cancelled": True}`) reaching the
  span's `output.value` automatically — **no manual `span.set_attribute()`
  calls needed**, provided `AgentState` is managed correctly.
- **Shape caveat.** Captured as one JSON-serialized string per span
  (`output.value`), not typed columns — queryable in BigQuery via
  `JSON_EXTRACT(...)`, but not `agent_telemetry`'s ergonomics. Fine for
  exploratory analytics on top of it later; not a judge-feeding replacement.
- **Unverified:** the BigQuery Linked Dataset's exact table names/schema
  (`spans`/`annotations` claimed by the original blueprint, not confirmed
  against real output).

## New dependencies, not yet added

`openinference-instrumentation-langchain`, `opentelemetry-sdk`,
`opentelemetry-exporter-otlp` — `uv add` when this is actually built.

## Setup sketch

1. `gcloud services enable telemetry.googleapis.com`
2. Grant `agent-sa`: `roles/telemetry.tracesWriter` (confirmed),
   `roles/serviceusage.serviceUsageConsumer` (confirm first).
3. BigQuery Console → new empty dataset → Cloud Trace Console → **Create
   Linked Dataset** → point at it. Google manages the table schema.
4. App startup, before any LangChain/LangGraph import:
   ```python
   from opentelemetry.sdk.trace import TracerProvider
   from opentelemetry.sdk.trace.export import BatchSpanProcessor
   from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
   from openinference.instrumentation.langchain import LangChainInstrumentor

   provider = TracerProvider()
   provider.add_span_processor(BatchSpanProcessor(
       OTLPSpanExporter(endpoint="telemetry.googleapis.com:443")))
   LangChainInstrumentor().instrument(tracer_provider=provider)
   ```
5. Drop `langsmith-api-key` from Secret Manager; remove
   `LANGSMITH_TRACING`/`LANGCHAIN_CALLBACKS_BACKGROUND` from `app/config.py`.
6. Update component reference §3 to reflect the closed trade-off.

## What doesn't change

`agent_telemetry`'s schema, write path, and the judge's query are untouched.
This becomes a second, independent telemetry source — same LangSmith-vs-
`agent_telemetry` split this project already has today (`.claude/rules/telemetry.md`),
just with a different backend for the trace half.
