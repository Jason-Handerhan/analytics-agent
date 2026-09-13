# Golden Dataset Regression Testing

> **Not the same thing as the LLM judge (`docs/llm-judge.md`).** That one is
> automated, scheduled, and checks faithfulness (does the answer match its own
> cited tool result). This one is hand-curated, event-triggered, and checks
> correctness (is the number actually right). Deliberately separate
> mechanisms — don't merge them.

> **Mostly a you-task, not a Claude Code task.** Curating what makes a *good*
> test question is judgment, not delegation. Claude Code's job here is
> narrower: building `scripts/run_golden_tests.py`, the comparison runner.

**The gap this fills:** a subtly wrong SQL query still executes and returns a
real, *faithful* number — one that genuinely matches its own cited tool
result, and answers the wrong question. Faithfulness checks can't catch that.
This is what actually verifies the number is right.

**Design:**
1. **20–30 fixed test questions**, correct SQL/DAX and correct answer
   **hand-verified once** (e.g. Recall@5 for the full ensemble → `0.367`).
2. **Run on events, not a schedule** — after a prompt/schema change or model
   swap, not nightly. The key structural difference from the LLM judge.
3. Send each through the live agent end-to-end; compare its answer to the
   verified expected value within tolerance — **mostly deterministic**, the
   same tolerance-matching as the verification contract's value check
   (`.claude/rules/orchestrator.md`), not an LLM judgment call. An optional light
   LLM check on the surrounding prose is reasonable; the number check isn't.
4. **Any verification queries route through the same governed MCP tools**
   (`agent_safe`, cost gates — `docs/data-pipeline.md`). No separate,
   more-permissive path for testing.

**Maintenance cost worth naming:** golden answers can go stale if underlying
data legitimately changes — a divergence is worth checking against the data
before assuming the agent regressed.
