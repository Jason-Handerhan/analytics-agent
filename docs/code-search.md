# Code Search — agentic, not vector

How `list_repo_files` and `read_repo_file` work, and why code search doesn't
use the vector index `search_docs` uses. Read before building either
tool.

## Why not vector search

Embeddings flatten what code questions actually depend on — imports, call
graphs, "what else is in this file." Those are *structural* facts, not
semantic-similarity facts. Benchmarks on real repos show agentic
exploration (list/read/grep in a loop) substantially outperforming pure
embedding retrieval for code.

**Honest caveat:** the strongest results were *hybrid* (semantic + grep
together), not pure agentic. Pure agentic is a deliberate simplification for
a small, stable codebase — see "Known gap" below.

**This applies to code only.** `search_docs` stays vector-based — prose isn't
"grep a repo" shaped, and semantic similarity is a genuinely correct
relevance signal there. The semantic model isn't searched at all: it's small
enough to sit in static context (`.claude/rules/gateway.md`).

## The two tools

```python
FILE_DESCRIPTIONS = {                      # hand-written, this repo
    "utils/data_prep.py": "Train/test split, cleaning, feature engineering",
    "utils/metrics.py":   "Evaluation metrics: precision, recall, F1, AUC",
    # partial coverage is fine — paths without an entry still get returned
}

def list_repo_files() -> list[dict]:
    """One Trees API call. Returns every path; description where we have one."""
    paths = _github_tree()                 # GET /repos/{o}/{r}/git/trees/{sha}?recursive=1
    return [{"path": p, "description": FILE_DESCRIPTIONS.get(p)} for p in paths]

def read_repo_file(path: str) -> str:
    """One Contents API call."""
    return _github_contents(path)          # GET /repos/{o}/{r}/contents/{path}
```

**Two tools, not one.** A single tool with a mode switch and conditionally
required args is harder for the model to call correctly than two unambiguous
ones.

**Domain-agnostic by design.** Splitting by domain matters for vector search
specifically, to avoid wasting half a search on the wrong corpus — the agent
just skips irrelevant paths for free. A third code domain needs no third tool.

## Use the Contents/Trees API, never GitHub's Search API

| | Limit (authenticated) |
|---|---|
| Contents / Trees | 5,000/hour |
| `/search/code` | **30/minute** |

Search runs a real query against GitHub's own index, so it's throttled far
harder. Build grep-like behavior instead: one Trees call for all paths, then
Contents calls for candidates, matching text in our own code. That's also
exactly the primitive-tools pattern the agentic benchmarks used.

**Auth:** `github-read-token` from Secret Manager, same `get_secret()` pattern
as every other credential. Public repos work unauthenticated at 60/hour — the
token is for the rate limit, not access.

## Descriptions live in the tool, not static context

`FILE_DESCRIPTIONS` is returned by `list_repo_files`, **not** added to the
always-injected orientation bundle. Static context is paid on *every* turn,
including DAX and BigQuery questions that never touch code. Returning it from
the tool means only code-exploration turns pay for it — same conditional-cost
principle as the DLP check and the approval pause.

**Manually maintained**, like the HTML-measure exclusion list. A new file in
the ML repo appears with no description until the dict is updated. Fine for a
finished, stable codebase; it would not be for one under active development.

## Known gap — vague conceptual queries

*"Show me the function for the train/test split"* has nothing to match on but
paths. The agent guesses from naming conventions, reads, and re-guesses if
wrong — correct, but more iterations than one ranked semantic hit.
`FILE_DESCRIPTIONS` narrows this; it doesn't close it. **If this proves worse
in practice than expected, adding a semantic fallback (hybrid) is the
evidenced next step** — not a redesign.
