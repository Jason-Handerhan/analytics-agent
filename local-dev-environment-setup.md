# Local Development Environment Setup — Complete Guide

> Written for a first-time local dev setup, coming from an all-cloud background.
> Follow top to bottom once; after that it's `code .` and go. Every command below
> is meant to be run in a terminal — on Windows that's PowerShell, on
> macOS/Linux that's the default Terminal app, unless noted otherwise.
>
> **Order is deliberate: the repo goes live at Step 9, before any cloud
> setup.** Steps 1–6 install tooling and lay out folders; 7–9 get
> `.gitignore`, the first commit, and GitHub done. Everything after that —
> the venv, dependencies, GCP, Entra, secrets — happens with a working repo
> Claude Code can be pointed at, so you're not doing the fiddliest setup by
> hand first. `.gitignore` lands before the venv exists, so `.venv/` is never
> committable even briefly.

> **Placeholders to substitute throughout**, all in caps so they're greppable:
> `YOUR_PROJECT` (GCP project ID), `YOUR_REGION` (pick once in Step 13 and
> keep it consistent — Cloud Run, BigQuery, Firestore, and the Vertex
> connection should all match), `YOUR_CHART_BUCKET`, and
> `your-email@example.com`.
>
> **Where this fits:** this document covers *getting your machine ready* —
> everything up to the point where you can write and run code. What to
> actually build, in what order, and why, lives in the **component reference**
> (architecture, design decisions, build order, success criteria). Do this
> guide once, then work from that one.

---

## Before you start — accounts and access

Nothing here is installed; it's what you need to *have*. The Microsoft items
are the long poles — if they need someone else's approval, request them now
and keep working through Steps 1–13 while you wait.

| Need | For | If you don't have it |
|---|---|---|
| **Access to the Instacart ML GCP project** | Everything from Step 12 on | This project reuses it — no new project, no new billing setup |
| **GitHub account** | Step 9 publishes the repo | Free; the repo is public by design |
| **Entra app-registration rights** | Step 14 — both app registrations | Application Developer role or higher. **Often restricted in corporate tenants** |
| **Power BI / Fabric admin** | Step 14's two tenant settings | **The longest-lead item.** You can't complete registration A alone without it — start the request today |
| **A Power BI workspace + published model** | The DAX tool queries it | This is the Instacart semantic model from the existing project |
| **LangSmith account** | Step 17 (optional) | Free tier is enough; skip the step entirely if you'd rather |

---

## What you're actually installing, and why (read this first)

Since you've only worked in the cloud so far, it helps to know what each piece
*does* before installing it, so none of this feels like cargo-culting commands:

| Tool | What it is | Why you need it |
|---|---|---|
| **uv** | A single fast tool that installs Python, creates virtual environments, and manages dependencies | Replaces `pip` + `venv` + a separate Python install — one tool instead of three |
| **Python** | The language your gateway, LangGraph agent, and MCP tools are written in | Nothing runs without it — installed *through* uv (Step 1) |
| **VS Code** | The editor/IDE | Where you'll read, write, and run all the code |
| **Git** | Version control | Tracks changes, required for the Claude Code workflow (worktrees, diffs) |
| **A virtual environment (`.venv`)** | An isolated Python package sandbox *per project* | Keeps this project's packages from colliding with any other Python project on your machine — created by uv, not `python -m venv` |
| **`pyproject.toml` + `uv.lock`** | Your project's declared dependencies, plus their exact resolved versions | The modern replacement for `requirements.txt` — `uv.lock` is what makes the environment *exactly* reproducible, not just approximately |
| **gcloud CLI** | Google Cloud's command-line tool | Lets you (and Claude Code) authenticate to and manage GCP resources locally — installed in Step 12 |
| **Microsoft Entra app registrations** | Two app identities in your Microsoft tenant (Step 14) | One lets the agent query the Power BI model; the other lets Power Apps call your gateway as the signed-in user. Not installed — configured in a portal |
| **Node.js** | JavaScript runtime | Needed only because the Claude Code CLI itself is distributed via npm |
| **Docker Desktop** | Container tooling | Installed early for convenience; the actual `Dockerfile` waits until deployment (see Step 5) |
| **Jupyter/ipykernel** | Notebook support | For validating isolated pieces of logic against real data (a SQL query, a chunking function, a vector search) — not for building the FastAPI gateway or LangGraph graph themselves, which are async, service-shaped code that doesn't fit a notebook's execution model well. Validate in a notebook, then write the confirmed logic directly into its permanent module. |

---

## Step 1 — Install uv (and Python through it)

**uv is a single, fast tool from Astral that replaces `pip`, `venv`, and
version management** — it can install Python itself, so there's no separate
python.org download, no "which button do I click" ambiguity, and no Windows
PATH checkbox to miss.

1. **Install uv:**
   - **macOS/Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - **Windows (PowerShell):** `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - **macOS (Homebrew alternative):** `brew install uv`

   Restart your terminal after installing, then verify:
   ```bash
   uv --version
   ```

2. **Install Python 3.12 through uv** — no python.org visit needed:
   ```bash
   uv python install 3.12
   ```
   3.12 specifically, not whatever's newest (currently 3.14.x): it's mature
   with universal support across the Google Cloud and data-science libraries
   this project depends on, while a brand-new release still has real gaps in
   third-party package compatibility — not a fight worth having in your
   infrastructure layer.

3. **Verify it installed:**
   ```bash
   uv python list
   ```
   You should see a `3.12.x` entry marked as installed. uv manages this Python
   build in its own isolated location — it doesn't touch your system Python or
   PATH, so it can't collide with anything else on the machine.

## Step 2 — Install VS Code

1. Download from **code.visualstudio.com** and run the installer (defaults are fine).
2. Open it once it's installed. You'll land on the Welcome tab — that's normal.
3. Install these extensions (click the Extensions icon in the left sidebar —
   four squares — and search each by name):
   - **Python** (by Microsoft) — syntax highlighting, running/debugging Python.
   - **Jupyter** (by Microsoft) — lets you run notebooks inside VS Code.
   - **Claude Code** (by Anthropic) — runs the CLI in a VS Code panel.
   - *(Optional but handy)* **GitLens** — richer git history/blame info inline.

## Step 3 — Install Git

1. **Windows:** download from **git-scm.com** and install (defaults are fine —
   this also gives you "Git Bash," a Linux-like terminal, if you ever want it).
2. **macOS:** usually already present; if not, `xcode-select --install` or
   `brew install git`.
3. Verify:
   ```bash
   git --version
   ```
4. One-time identity setup (git needs to know who's making commits):
   ```bash
   git config --global user.name "Your Name"
   git config --global user.email "your@email.com"
   ```

## Step 4 — Install Node.js (optional — only for the standalone CLI)

The VS Code extension bundles its own CLI, so this step is only needed if you
also want to run `claude` from a plain terminal.
1. Go to **nodejs.org** and install the **LTS** version.
2. Verify:
   ```bash
   node --version
   npm --version
   ```
3. Install the CLI globally:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

## Step 5 — Install Docker Desktop (tool only — no Dockerfile yet)

You won't write a `Dockerfile` until you're actually deploying the gateway to
Cloud Run, which happens in **Phase 1** of the build order
(`docs/build-order.md`) — earlier than you might expect, since Phase 1's whole point
is getting a real message flowing app → connector → gateway → back. Installing
the *tool* now has zero downside and is worth doing while you're already in
setup mode.

1. Download from **docker.com/products/docker-desktop** and install.
   - **Windows:** requires WSL 2. If you don't have it yet, open PowerShell as
     Administrator and run `wsl --install`, restart when prompted, then run the
     Docker Desktop installer and make sure **"Use WSL 2 instead of Hyper-V"**
     is checked (should be the default).
   - **macOS:** just run the installer.
2. Launch Docker Desktop once so it finishes first-run setup, then verify from
   a terminal:
   ```bash
   docker --version
   ```
3. **Free tier note:** Docker Desktop is free for personal use regardless of
   employer size — the paid tier only applies to large organizations (250+
   employees or $10M+ revenue) using it *for their business*. A personal
   portfolio project on your own machine is covered either way.

**What NOT to do yet:** don't write a `Dockerfile` during setup. It needs to
reference your actual entrypoint, dependency set, and port — none of which
exist until you've written the Phase 1 echo gateway. Write it as part of
Phase 1's deploy step, not now; installing the tool early saves a step,
writing the file early just means writing it twice.

## Step 6 — Create your project folder and its structure

Pick a home for your projects if you don't have one yet (e.g. `~/projects` on
macOS/Linux, `C:\Users\<you>\projects` on Windows), then:

```bash
mkdir analytics-agent
cd analytics-agent
git init
```

Open it in VS Code:
```bash
code .
```
(If `code` isn't recognized, open VS Code, press `Cmd/Ctrl+Shift+P`, type
"Shell Command: Install 'code' command in PATH", run it, then restart your terminal.)

**Create the module structure now, before writing any code.** This matters more
than it looks: if you don't establish it up front, Claude Code will invent a
layout — and possibly a *different* layout in a later session — and you'll end
up reconciling two structures instead of building. Establishing it once means
every subsequent "add the X tool" conversation has an unambiguous home.

```bash
mkdir -p app/gateway app/orchestrator app/mcp_server app/telemetry \
         context/docs context/page_info context/schema \
         context/orientation \
         definitions/sources definitions/agent_safe \
         definitions/vector_db \
         .claude/rules docs \
         scripts notebooks tests/fixtures
touch app/__init__.py app/config.py \
      app/gateway/__init__.py app/orchestrator/__init__.py \
      app/mcp_server/__init__.py app/telemetry/__init__.py \
      workflow_settings.yaml
```

**The complete picture — where every file (code you'll write, and the
documents you already have) actually lives:**

```
analytics-agent/
├── CLAUDE.md                                            ← save here, repo root
├── conversational-analytics-platform-COMPONENT-REFERENCE.md   ← same
├── local-dev-environment-setup.md                        ← this file, same
├── pyproject.toml          # created by Step 11's `uv add`, not by hand
├── uv.lock                 # generated — never hand-edit
├── Dockerfile              # written in Phase 1, not now
├── .gitignore              # Step 7
│
├── .claude/
│   └── rules/                    ← save all 5 rule files here
│       ├── orchestrator.md
│       ├── gateway.md
│       ├── mcp-tools.md
│       ├── telemetry.md
│       └── data-pipeline.md
│
├── docs/                         ← save all 12 doc files here
│   ├── approval-workflow.md
│   ├── auth.md
│   ├── build-order.md
│   ├── chart-tool.md
│   ├── ci-cd.md
│   ├── code-search.md
│   ├── data-pipeline.md
│   ├── frontend.md
│   ├── golden-dataset.md
│   ├── llm-judge.md
│   ├── success-criteria.md
│   └── testing.md
│
├── app/
│   ├── config.py               # non-secret settings, read from env
│   ├── exceptions.py           # ToolError, ToolTimeoutError, TurnCancelledError
│   │                           # — shared, so a tool and the graph raise the same types
│   ├── gateway/
│   ├── orchestrator/
│   ├── mcp_server/
│   └── telemetry/
│
├── context/                      # source material, not code — populated as you go
│   ├── docs/
│   ├── page_info/
│   ├── schema/
│   └── orientation/
│
├── workflow_settings.yaml      # Dataform — must be at repo root
├── definitions/                # Dataform — must be at repo root
│   ├── sources/
│   ├── agent_safe/
│   └── vector_db/
│
├── scripts/
├── notebooks/
└── tests/
    └── fixtures/                  ← TMDL samples + expected artifact
```

**Twenty documents to copy in: the three `.md` files at the root, the five in
`.claude/rules/`, and the twelve in `docs/`.** Copy each into the exact path
shown before running anything else — `pyproject.toml`, `uv.lock`, `Dockerfile`,
and `.gitignore` are *generated* later, not copied. Everything under `app/`, `context/`, `definitions/`, `scripts/`,
and `tests/` gets built out over the course of the project; the folders exist
now so there's an unambiguous home for the first file that lands in each.

What each directory is for, and where its spec lives:

| Path | Holds | Spec |
|---|---|---|
| `app/config.py` | Non-secret config constants (project ID, dataset names, timeouts) read from env vars, including `MODEL` — **same name as the deploy env var**, so a model swap is one value in one place | `.claude/rules/gateway.md` |
| `app/exceptions.py` | `ToolError`, `ToolTimeoutError`, `TurnCancelledError` — shared so a tool and the graph raise and catch the same types | `.claude/rules/orchestrator.md` |
| `app/gateway/` | FastAPI app, `/ask` + status + cancel endpoints, auth validation, Markdown→HTML | `.claude/rules/gateway.md`, `docs/auth.md`, `docs/frontend.md` |
| `app/orchestrator/` | LangGraph graph, agent node, verification checks, guardrails | `.claude/rules/orchestrator.md` |
| `app/mcp_server/` | FastMCP server + one module per tool group (tools sharing a client live together) | `.claude/rules/mcp-tools.md` |
| `app/telemetry/` | `agent_telemetry` writer + schema | `docs/build-order.md` Phase 1, `docs/llm-judge.md` |
| `context/docs/` | Project docs + abbreviated README — methodology corpus, **input to the vector index** | `docs/data-pipeline.md` |
| `context/page_info/` | Power BI page-info HTML — read **whole** by `get_page_info`; not chunked or embedded | `.claude/rules/mcp-tools.md` |
| `context/schema/` | `model_schema.json` — **committed, read at startup** into the table + measure registries (`docs/data-pipeline.md`). Also BigQuery schema snapshots, which are a development aid only | `.claude/rules/gateway.md` |
| `context/orientation/` | Exec summary + navigator + architecture diagram — always-in-context bundle | `.claude/rules/gateway.md` |
| `scripts/` | One-off/manual jobs: `build_model_context.py` (semantic-model registries), `build_vector_db.py` (docs chunking), schema dumps, judge Cloud Run Job, golden dataset runner | `docs/data-pipeline.md`, `docs/llm-judge.md`, `docs/golden-dataset.md` |
| `definitions/` | `sources/` declarations, `agent_safe/` tables (**tables only, never views**), `vector_db/` embedding models — **written and pushed here like any other code; deployed via a GitHub-linked Dataform repository, not by this repo directly** (`docs/data-pipeline.md`) | `docs/data-pipeline.md` |
| `notebooks/` | Throwaway validation of isolated logic against real data | — |
| `tests/` | pytest suite (+ `conftest.py` fixtures) | `docs/testing.md` |

**`context/` is source material, not code** — the docs and TMDL files get
chunked into the vector index, the orientation bundle gets read into every
prompt, and the schema snapshots exist so Claude Code can see your real table
structure while building without a `bq` round-trip per question. You'll
populate these as you go, not during setup; creating them now just means the
structure is settled before anyone has to guess at it.

**A note on `config.py` vs. Secret Manager:** anything secret goes in Secret
Manager (Step 15). `config.py` is for the non-secret values that would
otherwise get hardcoded in five places — project ID, dataset names, the
`vector-search-sa` principal, timeout values, the active model provider. Read
environment variables there where it makes sense (`os.environ.get(...)`), so
the same code works locally and on Cloud Run without edits.

## Step 7 — `.gitignore` (do this before your first commit)

Create a file named `.gitignore` in the project root:
```
.venv/
__pycache__/
*.pyc
.ipynb_checkpoints/
*.egg-info/
.DS_Store
.pytest_cache/
*-key.json
github-deployer-key.json
*.png
!docs/**/*.png
```

This tells git to never track your virtual environment, compiled Python files,
or notebook checkpoint clutter. **Do this before your first `git add`** —
otherwise you risk committing the entire `.venv` folder (thousands of files)
into your history. (No `.env` line needed here since Step 12 has you skip that
file entirely — one less thing to accidentally leak.)

The `*.png` line keeps locally-generated test charts out of the repo while the
`!docs/**/*.png` exception preserves any screenshots you deliberately add for
the README — worth setting now, since `generate_chart` will produce a lot of
throwaway PNGs during development.

The `*-key.json` patterns are the important ones: a service account key is a
live credential, and this repo is **public**. Step 16 has you delete the key
file after use, but this is the backstop for the time you regenerate one and
forget.

**Deliberately *not* on this list: `pyproject.toml` and `uv.lock`.** Both get
committed — they're what makes the environment reproducible for Claude Code,
CI, and future-you, not local clutter to hide.

## Step 8 — First commit

```bash
git add .
git commit -m "Initial project setup"
```

## Step 9 — Connect to GitHub

This repo will ultimately be public-facing (recruiters/managers viewing it), so
worth doing deliberately rather than as an afterthought.

**Easiest path — publish directly from VS Code:**
1. Sign in to GitHub inside VS Code if you haven't: click the Accounts icon
   (bottom-left) → "Sign in with GitHub," and authorize.
2. Open the **Source Control** panel (branching-line icon, left sidebar).
3. Click **"Publish Branch"** (or "Publish to GitHub" — the exact label varies
   by VS Code version). Choose **Public** repository (so it's viewable without
   the person needing to request access) and confirm the name.
4. VS Code creates the GitHub repo *and* pushes your existing commit(s) to it in
   one step — no separate trip to github.com required.

**Manual path, if you'd rather see each step explicitly:**
1. Go to **github.com**, click **New repository**. Name it, set **Public**, and
   — since you already have a local repo with a commit — do **not** initialize
   it with a README, `.gitignore`, or license (that would create conflicting
   history you'd have to merge).
2. Copy the remote URL GitHub shows you, then locally:
   ```bash
   git remote add origin https://github.com/your-username/your-repo-name.git
   git branch -M main
   git push -u origin main
   ```
   `-u` sets the upstream link, so every push after this is just `git push`.

**A few things worth setting up now, since this repo represents you:**
- Add a real top-level `README.md` early — even a placeholder — so the repo
  never sits empty if someone clicks the link before it's finished.
- Consider **branch protection** on `main` later (Settings → Branches) once
  you're using feature branches, so you're not committing directly to the
  branch a recruiter would land on.
- The repo being public from the start is also what enables Claude Code's
  web/cloud session features, which specifically work off a GitHub repo.

## Step 10 — Create the virtual environment with uv

This is the "sandbox per project" step — do this **inside** `analytics-agent`.

```bash
uv venv --python 3.12
```

This creates a standard `.venv` folder, built on the 3.12 you installed
*through* uv in Step 1 — pinned explicitly so it stays on 3.12 even if another
Python version ever ends up on the machine. Faster than `python -m venv`
because uv does the work in a compiled binary rather than pure Python, but the
resulting `.venv` is a completely normal virtual environment — nothing about
how it works is uv-specific.

Activate it (you'll do this every time you open a new terminal for this project):
- **macOS/Linux:** `source .venv/bin/activate`
- **Windows (PowerShell):** `.venv\Scripts\Activate.ps1`
- **Windows (cmd.exe):** `.venv\Scripts\activate.bat`

You'll know it worked because your terminal prompt now starts with `(.venv)`.
**Everything from here on (installing packages, running the app) happens with
the venv activated.** VS Code will also usually detect it and offer to use it
as your notebook/Python interpreter — accept that when prompted (or set it
manually: `Cmd/Ctrl+Shift+P` → "Python: Select Interpreter" → pick the one
inside `.venv`).

**A genuine alternative worth knowing: you don't have to activate at all.**
`uv run <command>` finds and uses the project's `.venv` automatically, every
time, without a separate activation step — e.g. `uv run pytest tests/ -v`
works whether or not you've activated anything. Activation is still completely
normal and this doc uses it throughout for clarity, but if a step ever feels
redundant with `uv run`, that's why — either approach is correct.

## Step 11 — Declare dependencies with uv (`pyproject.toml` + `uv.lock`)

**Two files replace `requirements.txt`, and they do different jobs:**
- **`pyproject.toml`** — the packages your project depends on, by name (with
  version constraints if you ever need them). You read and edit this one.
- **`uv.lock`** — the *exact* resolved version of every package, direct and
  transitive, plus integrity hashes. You never hand-edit this one; uv writes
  it. This is what makes the environment reproducible in the way `pip freeze`
  only approximated — `pip freeze` captures what happened to be installed at
  the moment you ran it; `uv.lock` is generated from a real dependency
  resolution and is what everyone (including future-you, including CI) installs
  from to get *identical* versions, not just versions in the same ballpark.

**You don't create either file by hand.** Run this with the venv activated,
from the project root — it creates `pyproject.toml` if it doesn't exist yet,
resolves everything, writes `uv.lock`, and installs into `.venv`, all in one
step:

```bash
uv add fastapi "uvicorn[standard]" pydantic langgraph langchain-mcp-adapters \
  fastmcp langchain-anthropic langchain-core google-cloud-bigquery \
  google-cloud-secret-manager google-cloud-storage msal "PyJWT[crypto]" \
  requests pandas numpy matplotlib seaborn mistune "unstructured[md]" \
  google-cloud-firestore pysbd
```

Then add the dev-only tools **as a separate group** — this is a real
improvement over a flat `requirements.txt`, which had no way to distinguish
"needed to run the app" from "needed only while developing it":

```bash
uv add --dev pytest pytest-asyncio jupyter ipykernel
```

A quick note on what a few of these are, since the names won't all be obvious:
- `uvicorn[standard]` — the actual server that runs your FastAPI app.
- `pydantic` — the schema/validation library behind `AgentResponse`, `Claim`,
  and the chart specs. FastAPI pulls it in as a dependency anyway, but it's
  listed explicitly since your own code imports `BaseModel` directly.
- `langchain-mcp-adapters` — lets LangGraph act as an MCP **client** (talk to
  your tool server) — this is the piece behind `MultiServerMCPClient`.
- `fastmcp` — what you'll use to build the MCP **server** exposing your tools.
- `langchain-anthropic` — the LangChain wrapper for Claude Sonnet 5, the
  primary model. Swapping providers later (the model-swappability goal) means
  `uv add langchain-google-genai` or `langchain-openai` then, plus a branch in
  `infer_provider()` — no need to install them speculatively now.
- `langchain-core` — `HumanMessage`/`AIMessage`, used when rebuilding chat
  history into messages the model reads. It arrives transitively via
  `langgraph`, but declare it explicitly: you import it directly, and a
  transitive dependency can disappear on someone else's release.
- `msal` — Microsoft's auth library, used for the Power BI service principal's
  client-credentials flow (auth Hop 5, `docs/auth.md`).
- `PyJWT[crypto]` — validates the incoming Entra token at the gateway boundary
  (Hop 3). The `[crypto]` extra is **required**, not optional — without it
  PyJWT can't verify RS256 signatures, which is exactly what Entra uses.
- `requests` — used for the Power BI `executeQueries` REST calls.
- `numpy` — near-universal; pandas is built on it, and you'll likely touch it
  directly for anything vector/embedding-related.
- `seaborn` — a statistical-plotting layer on top of matplotlib, used by the
  `generate_chart` tool's rendering methods.
- `mistune` — Markdown → HTML conversion with a subclassable renderer, used to
  add inline styling for the Power Apps response formatting.
- `unstructured[md]` — structure-aware parsing/chunking for the docs
  files feeding the vector index. Page-info is read whole, not chunked.
- `google-cloud-firestore` — conversation state: status, pending approvals,
  the cancel flag, and chat history (`.claude/rules/gateway.md`).
- `pysbd` — sentence-boundary detection for truncating stored chat history.
  Pure Python, no model download (unlike `nltk`). A character cut can slice a
  number in half; a naive regex breaks on *"Approx. 0.367"*.
- `pytest-asyncio` — needed to test any `async` function, which most of this
  codebase's core paths are.

**Adding a package later** is one command, and it keeps both files in sync
automatically — no separate "install it, then remember to update the file"
step, and no `pip freeze` re-dump:
```bash
uv add some-new-package
```

**Recreating the environment** (a fresh clone, a new machine, or after Claude
Code adds a dependency on your behalf) reads both files and installs the exact
locked versions:
```bash
uv sync
```

**Commit both `pyproject.toml` and `uv.lock` to git.** Unlike a
`node_modules`-style folder, a lockfile for an *application* (as opposed to a
library) is meant to be committed — it's the mechanism that makes "works on my
machine" actually true on every machine, including CI. Expect `uv.lock` to be
long — it captures every transitive dependency (things your packages depend on
that you never explicitly installed), not just the ~16 you named. That's
normal and correct; it's what makes it fully reproducible, not a sign anything
went wrong.

## Step 12 — Install and authenticate the gcloud CLI

1. Install from **cloud.google.com/sdk/docs/install** (there's a Windows
   installer and a shell script for macOS/Linux — follow the page for your OS).
2. Verify:
   ```bash
   gcloud --version
   ```
3. Authenticate (opens a browser to sign in with your Google account):
   ```bash
   gcloud auth login
   ```
4. Set up **Application Default Credentials** — this is what your *Python code*
   (the BigQuery/Secret Manager libraries) actually looks for:
   ```bash
   gcloud auth application-default login
   ```
5. Point the CLI at the **existing Instacart ML project** — this project
   reuses it, there's no new one to create:
   ```bash
   gcloud config set project YOUR_PROJECT
   ```
   (The project **ID** is the short slug in the Console's project selector,
   not the display name.)

6. **Set a budget alert if there isn't one already** (Phase 0 item 8 in
   `docs/build-order.md`): Console → **Billing** → **Budgets & alerts** →
   **Create budget**, scoped to this project, email thresholds at 50/90/100%.
   A budget alert *notifies*; it does not cap spend — the per-query byte
   guardrails (`.claude/rules/orchestrator.md`) are what actually stop a
   runaway.

## Step 13 — Provision GCP: APIs, service accounts, datasets, bucket, Firestore

**Why this is here and not "create the Cloud Run service":** Cloud Run doesn't
work like a VM you provision ahead of time. The service is *created by the
first `gcloud run deploy`* — there's no separate "make the service" step in the
console. What you can and should do now is enable the APIs and confirm your
permissions, so that when Phase 1 runs the deploy, it works on the first try
instead of failing on a missing API you then have to go enable and re-run.

1. **Enable the APIs this project touches.** Each is a one-time flip per
   project; deploys fail with a fairly clear "API not enabled" error otherwise:
   ```bash
   gcloud services enable \
     run.googleapis.com \
     cloudbuild.googleapis.com \
     artifactregistry.googleapis.com \
     secretmanager.googleapis.com \
     bigquery.googleapis.com \
     aiplatform.googleapis.com \
     cloudscheduler.googleapis.com \
     firestore.googleapis.com \
     iamcredentials.googleapis.com \
     bigqueryconnection.googleapis.com
   ```
   - `run` — the gateway service itself, and later the judge Cloud Run Job
   - `cloudbuild` + `artifactregistry` — CI builds a tagged image
     (`gcloud builds submit --tag ...:${{ github.sha }}`) and stores it;
     deploy then references that image by tag, not `--source .` — see the
     CI/CD workflow (`docs/ci-cd.md`) for why they're separate steps
   - `secretmanager`, `bigquery` — the data/secrets path
   - `iamcredentials` — **required for impersonation.** Without it,
     `agent-sa` minting a token as `vector-search-sa` fails at runtime, not
     at deploy (`docs/data-pipeline.md`)
   - `bigqueryconnection` — needed for `bq mk --connection`, which the
     embedding model depends on
   - `aiplatform` — Vertex AI, for `ML.GENERATE_EMBEDDING` in the vector index
   - `firestore` — conversation state (status, approvals, cancel flag, chat
     history) that has to survive across Cloud Run instances
   - `cloudscheduler` — the nightly judge trigger (Phase 6; harmless to enable now)

2. **Pick a region and set it as your default** so you don't have to pass
   `--region` on every command. Use the same region as your BigQuery dataset
   where possible — cross-region reads are slower and can incur egress:
   ```bash
   gcloud config set run/region us-central1     # or your BigQuery region
   ```

3. **Create both service accounts.** `vector-search-sa`'s impersonation
   binding names `agent-sa`, so `agent-sa` must exist first. Their dataset
   grants come after the datasets exist (next item).
   ```bash
   gcloud iam service-accounts create agent-sa \
     --display-name="Analytics Agent Service Account"
   gcloud iam service-accounts create vector-search-sa \
     --display-name="Vector Search (vector_db only)"

   # agent-sa may mint short-lived tokens AS vector-search-sa. Without this,
   # impersonation fails at runtime, not at deploy (docs/data-pipeline.md).
   gcloud iam service-accounts add-iam-policy-binding \
     vector-search-sa@YOUR_PROJECT.iam.gserviceaccount.com \
     --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
     --role="roles/iam.serviceAccountTokenCreator"
   ```

4. **Confirm you can deploy *as* the service account.** This is the most common
   Phase 1 blocker and it's silent until you try: to deploy a service that runs
   as `agent-sa`, your own account needs `roles/iam.serviceAccountUser` **on
   that service account**. Being a project Owner does not always imply it.
   ```bash
   gcloud iam service-accounts add-iam-policy-binding \
     agent-sa@YOUR_PROJECT.iam.gserviceaccount.com \
     --member="user:your-email@example.com" \
     --role="roles/iam.serviceAccountUser"
   ```
   *(Run this after item 3 creates `agent-sa`.)*

5. **Create the three BigQuery datasets, then grant each identity only its
   own.** That split *is* the dataset boundary — `run_bigquery_sql` runs as
   `agent-sa` and physically cannot read `vector_db`.
   ```bash
   bq mk --dataset YOUR_PROJECT:agent_safe   # enriched tables the agent queries
   bq mk --dataset YOUR_PROJECT:vector_db    # embedded chunks search_docs reads read
   bq mk --dataset YOUR_PROJECT:staging      # doc_chunks + the embedding model

   # Both need jobUser to run any query at all — that's project-level.
   for SA in agent-sa vector-search-sa; do
     gcloud projects add-iam-policy-binding YOUR_PROJECT \
       --member="serviceAccount:$SA@YOUR_PROJECT.iam.gserviceaccount.com" \
       --role="roles/bigquery.jobUser"
   done

   # Read access is where they diverge — this is the boundary.
   bq add-iam-policy-binding --role="roles/bigquery.dataViewer" \
     --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
     YOUR_PROJECT:agent_safe
   bq add-iam-policy-binding --role="roles/bigquery.dataViewer" \
     --member="serviceAccount:vector-search-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
     YOUR_PROJECT:vector_db

   # search_docs' query calls MODEL `staging.embedding_model` to embed the
   # search string, so vector-search-sa needs to READ that model too. Without
   # this, every search_docs call fails at runtime with a permission error —
   # never at deploy (docs/data-pipeline.md, "What search_docs runs").
   bq add-iam-policy-binding --role="roles/bigquery.dataViewer" \
     --member="serviceAccount:vector-search-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
     YOUR_PROJECT:staging
   ```
   **`agent-sa` gets no grant on `vector_db`, and that's the point** — a
   hand-written query against the vector tables fails at BigQuery, not at a
   check in application code. `staging`'s *tables* are written by
   `scripts/build_vector_db.py` running as **you**, so neither service account
   needs write access — but `vector-search-sa` still reads
   `staging.embedding_model` on every search, hence the grant above.
   **Also one-time, for the embedding model** (`docs/data-pipeline.md`): a
   BigQuery→Vertex connection, plus `roles/aiplatform.user` on *that
   connection's* auto-created service account — a separate identity from
   `agent-sa`, and an easy one to miss.
   ```bash
   bq mk --connection --location=YOUR_REGION \
     --connection_type=CLOUD_RESOURCE vertex_conn

   # The connection's service account is AUTO-GENERATED — look it up, don't
   # guess it. Without this grant, ML.GENERATE_EMBEDDING fails at query time.
   CONN_SA=$(bq show --format=prettyjson --connection \
     YOUR_PROJECT.YOUR_REGION.vertex_conn \
     | python -c "import json,sys; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")
   gcloud projects add-iam-policy-binding YOUR_PROJECT \
     --member="serviceAccount:$CONN_SA" --role="roles/aiplatform.user"
   ```

6. **Create the chart bucket — and configure it for public reads now**, because
   the failure mode later is confusing. `generate_chart` writes PNGs here and
   returns a public `https://` URL that Power Apps fetches directly (a decided
   trade-off — see the `docs/chart-tool.md`). That fetch carries none of
   your auth, so the objects must be anonymously readable:
   ```bash
   # Create WITHOUT uniform bucket-level access, so per-object ACLs work
   gcloud storage buckets create gs://YOUR_CHART_BUCKET \
     --location=YOUR_REGION \
     --no-uniform-bucket-level-access
   ```
   **`agent-sa` needs write access — it's the identity `generate_chart` runs
   as.** Without this the tool fails at first chart, not at deploy:
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://YOUR_CHART_BUCKET \
     --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"
   ```
   `objectAdmin` rather than `objectCreator` because `blob.make_public()`
   modifies an object's ACL after writing it — creating isn't enough.

   **If `blob.make_public()` fails later, this is almost always why.** Two
   bucket settings break it, both common defaults:
   - **Uniform bucket-level access** — disables the per-object ACLs
     `make_public()` relies on.
   - **Public access prevention** — blocks anonymous reads outright, often
     inherited from an org policy rather than set on the bucket.

   The resulting error reads like a code permissions bug, not a bucket
   setting, which is what makes it eat an afternoon. Verify with
   `gcloud storage buckets describe gs://YOUR_CHART_BUCKET` before writing any
   chart code. (If an org policy enforces public access prevention and you
   can't change it, that forces the signed-URL approach instead — a design
   change worth raising rather than working around.)

   Also worth setting a lifecycle rule so charts don't accumulate forever —
   every chart request writes a new object nothing cleans up:
   ```bash
   echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > lifecycle.json
   gcloud storage buckets update gs://YOUR_CHART_BUCKET --lifecycle-file=lifecycle.json
   rm lifecycle.json
   ```

7. **Create the Firestore database and its TTL policy.** This holds
   conversation state that must survive across Cloud Run instances — status
   text, pending approvals, the cancel flag, and short-term chat history.
   **Why this exists at all:** Cloud Run runs *multiple concurrent instances*
   of your one container under ordinary load, each with separate memory, and
   the load balancer routes requests to whichever is free. An in-memory dict
   would silently miss whenever two requests in one conversation land on
   different instances (`.claude/rules/gateway.md`).
   ```bash
   # Native mode, same region as Cloud Run. One database per project.
   gcloud firestore databases create --location=YOUR_REGION

   # agent-sa needs read/write. Same identity that already holds BigQuery,
   # GCS, and Secret Manager access — one more grant, not a new identity.
   gcloud projects add-iam-policy-binding YOUR_PROJECT \
     --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
     --role="roles/datastore.user"

   # Auto-delete stale conversations after 30 days. A declarative policy,
   # not a scheduled job — same idea as the chart bucket's lifecycle rule.
   # Targets the `last_activity_at` field the app writes on every turn.
   gcloud firestore fields ttls update last_activity_at \
     --collection-group=sessions --enable-ttl
   ```
   **No VPC connector needed** — Firestore is reachable the same way BigQuery,
   GCS, and Secret Manager already are, with the same ADC auth. That's the
   specific reason it was chosen over Redis/Memorystore, which *would* have
   required standing up VPC networking this project needs nowhere else.

   **Cost at this project's scale: effectively $0.** The free tier covers
   50,000 reads and 20,000 writes per day; realistic usage here is a tiny
   fraction of that. One honest exception: **TTL deletes are not in the free
   tier** ($0.01 per 100,000) — negligible at this volume, but not literally
   free like the reads and writes.

8. **What you'll write in Phase 1, not now:** the `Dockerfile` and the actual
   `gcloud run deploy`. Both need your real entrypoint and dependencies to
   exist first. The deploy command itself is in `CLAUDE.md`'s fast reference.

**Free tier note:** Cloud Run has a perpetual free tier (a monthly allowance of
requests, CPU-seconds, and memory-seconds). A portfolio project that
scales to zero between demo sessions will likely cost little to nothing — but
the Budget alert from Phase 0 is still the thing that actually protects you,
not an assumption about the free tier's limits.

## Step 14 — Microsoft Entra ID: two app registrations (the Microsoft-side setup)

Everything above wires up the *Google* side. This step does the Microsoft side,
and it's genuinely required before you can test anything that touches Power BI
locally.

**You need two separate app registrations, not one.** They do completely
different jobs and get authenticated in different ways — reusing one for both
doesn't work:

| | **A. Power BI service principal** | **B. Connector app** |
|---|---|---|
| Auth flow | App-only (client credentials) | Delegated (per signed-in user) |
| Who it represents | The system itself | The individual using the app |
| Used by | `run_dax_query` calling `executeQueries` | Power Apps custom connector → gateway |
| Needed by | **Now** — Phase 1 local testing | Partially now, finished in Phase 1 |

**Both access prerequisites are listed at the top of this guide** — Entra
app-registration rights and Power BI/Fabric admin. If you skipped that and
don't have them, stop here: registration A can't be completed without them.

---

### A. Power BI service principal (do this now — Phase 1 needs it)

**A1. Register the app.** portal.azure.com → **Microsoft Entra ID** → **App
registrations** → **New registration**.
- Name: something identifiable, e.g. `analytics-agent-powerbi-sp`
- Supported account types: **Accounts in this organizational directory only**
- Redirect URI: **leave blank** — service principals don't use one

**A2. Record two values** from the app's Overview page immediately:
- **Application (client) ID**
- **Directory (tenant) ID**

**A3. Create a client secret.** → **Certificates & secrets** → **New client
secret**. Set an expiry you'll actually remember (24 months is reasonable).
**Copy the Value — not the Secret ID — immediately.** It's shown exactly once;
if you navigate away you have to delete it and make a new one.

> Note the expiry date somewhere. When it lapses, `run_dax_query` starts
> failing with an auth error that looks like a code bug but isn't.

**A4. Create a security group and add the service principal to it.** This step
is easy to miss and the tenant setting in A5 depends on it — Microsoft's
guidance is to scope service-principal API access to a specific security group
rather than enabling it org-wide.
- Entra ID → **Groups** → **New group**
- Group type: **Security**; name it something like `powerbi-api-service-principals`
- **Members** → add the app registration from A1

**A5. Enable two Power BI tenant settings.** app.powerbi.com → **Settings gear**
→ **Admin portal** → **Tenant settings**:
- Under **Developer settings**: **"Service principals can call Fabric public
  APIs"** (older tenants may still label this *"Allow service principals to use
  Power BI APIs"* — same setting) → **Enabled**, scoped to the security group
  from A4.
- Under **Integration settings**: **"Dataset Execute Queries REST API"** →
  **Enabled**. Without this, `executeQueries` returns an error no amount of
  correct auth will fix.

**A6. Grant workspace access.** In the Power BI workspace holding your Instacart
semantic model → **Manage access** → add the service principal (search by the
app registration name) as **Contributor**.

Contributor is the minimum role that satisfies `executeQueries`' documented
"Read + Build" requirement, while still having no Write/Reshare permission on
the dataset. (Confirm empirically in Phase 1 — see the component reference's
known-unverified list.)

---

### B. Connector app (start now, finish in Phase 1)

**B1. Register a second app**, same path as A1, named distinctly — e.g.
`analytics-agent-connector`.

**B2. Expose an API.** → **Expose an API** → **Add** an Application ID URI
(accept the generated `api://<client-id>` default) → **Add a scope**:
- Scope name: `access_as_user`
- Who can consent: **Admins and users**
- Fill the consent display name/description (shown to users at sign-in)

The **Application ID URI** is what your gateway validates tokens against — it
becomes `EXPECTED_AUDIENCE` in `app/config.py`.

**B3. Create a client secret**, same as A3. This becomes the
`entra-client-secret` in Secret Manager.

**B4. Redirect URI — you can't do this yet, and that's expected.** Power
Platform generates the redirect URI when you create the custom connector
(Phase 1, step 3). Come back here afterward: **Authentication** → **Add a
platform** → **Web** → paste the URI Power Platform shows you. This
chicken-and-egg is normal, not something you did wrong.

**B5. When you build the connector in Phase 1, give it an `x-api-key` header**
with the `gateway-api-key` value as its default (`docs/auth.md`). OAuth is
only one of the two factors the gateway enforces — without this every call
401s despite perfectly valid OAuth, which looks like an auth-config problem
and isn't.

---

### What to record before moving on

| Value | Where it comes from | Where it goes |
|---|---|---|
| Directory (tenant) ID | A2 | `config.py` as `TENANT_ID` |
| PBI SP client ID | A2 | Secret Manager: `power-bi-sp-client-id` |
| PBI SP client secret | A3 | Secret Manager: `power-bi-sp-client-secret` |
| Tenant ID (again, for MSAL) | A2 | Secret Manager: `azure-tenant-id` |
| Connector app ID URI | B2 | `config.py` as `EXPECTED_AUDIENCE` |
| Connector client secret | B3 | Secret Manager: `entra-client-secret` |
| Power BI **dataset** ID | Power BI URL when viewing the model | `config.py` as `POWER_BI_DATASET_ID` |

The next step (Secret Manager) is where the secret values above actually get
stored — don't paste them into a file in the meantime.

---

### Verify A worked (worth doing before you build anything on it)

Once Step 15 has the secrets stored, this is the "prove `executeQueries` from a
plain script" task from Phase 1 — and it isolates Microsoft-side auth from all
your application code, so a failure here is unambiguous:

```python
# throwaway script — delete after it passes
import msal, requests

TENANT_ID = "..."          # from A2
CLIENT_ID = "..."          # from A2
CLIENT_SECRET = "..."      # from A3
DATASET_ID = "..."         # from the Power BI URL

app = msal.ConfidentialClientApplication(
    CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    client_credential=CLIENT_SECRET,
)
token = app.acquire_token_for_client(
    scopes=["https://analysis.windows.net/powerbi/api/.default"]
)["access_token"]

resp = requests.post(
    f"https://api.powerbi.com/v1.0/myorg/datasets/{DATASET_ID}/executeQueries",
    headers={"Authorization": f"Bearer {token}"},
    json={"queries": [{"query": "EVALUATE ROW(\"test\", 1)"}]},
)
print(resp.status_code)
print(resp.json())
```

`EVALUATE ROW("test", 1)` is a trivial DAX expression that doesn't touch your
model — it isolates *auth and permissions* from *whether your DAX is correct*.
If this returns 200, registration A is fully working.

Common failures and what they actually mean:
- **401 on `acquire_token_for_client`** → client ID/secret/tenant wrong, or the
  secret expired.
- **401/403 on the `executeQueries` call** → token was fine, so it's A5 (tenant
  settings) or A6 (workspace role).
- **"powerBINotAuthorizedException"** → almost always A6: the service principal
  isn't a workspace member yet.

## Step 15 — Secrets: Secret Manager from day one (not `.env`)

Some setups reach for a local `.env` file for dev and switch to Secret Manager
only when deploying. Skip that — you've already decided Secret Manager is a real
built feature of this project, and your local `gcloud` Application Default
Credentials (Step 12) authenticate to Secret Manager exactly the same way they
authenticate to BigQuery. Calling Secret Manager from the start means your local
and deployed code use the **identical path** — no switch-over step later, no
risk of forgetting to change something before deploying.

1. Create the secrets you recorded in Step 14 (repeat per secret):
   ```bash
   echo -n "your-secret-value" | gcloud secrets create power-bi-sp-client-id --data-file=-
   echo -n "your-secret-value" | gcloud secrets create power-bi-sp-client-secret --data-file=-
   echo -n "your-secret-value" | gcloud secrets create azure-tenant-id --data-file=-
   echo -n "your-secret-value" | gcloud secrets create entra-client-secret --data-file=-
   # GitHub read token — for the ML repo's code tools. Public repos work
   # unauthenticated at 60 req/hr; the token raises that to 5,000.
   echo -n "ghp_..." | gcloud secrets create github-read-token --data-file=-
   # The LLM the whole agent runs on — same get_secret() pattern as the rest.
   echo -n "sk-ant-..." | gcloud secrets create anthropic-api-key --data-file=-
   # LangSmith tracing — runs in the deployed service too (Step 17)
   echo -n "lsv2_..."  | gcloud secrets create langsmith-api-key --data-file=-
   ```
   You'll also need a `gateway-api-key` — this one you invent rather than copy
   from Microsoft; it's the shared secret between the custom connector and your
   gateway (auth Hop 3):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))" \
     | tr -d '\n' | gcloud secrets create gateway-api-key --data-file=-
   ```
2. **Grant `agent-sa` access to every secret — the deployed app reads them as
   that identity, so without this Phase 1 fails on the first request**, not at
   deploy. Grant your own account too, for local runs.
   ```bash
   for S in gateway-api-key anthropic-api-key github-read-token \
            azure-tenant-id entra-client-secret langsmith-api-key \
            power-bi-sp-client-id power-bi-sp-client-secret; do
     gcloud secrets add-iam-policy-binding "$S" \
       --member="serviceAccount:agent-sa@YOUR_PROJECT.iam.gserviceaccount.com" \
       --role="roles/secretmanager.secretAccessor"
     gcloud secrets add-iam-policy-binding "$S" \
       --member="user:your-email@example.com" \
       --role="roles/secretmanager.secretAccessor"
   done
   ```
   *(Run after item 1 has created all eight.)*
3. **Confirm all eight exist and are readable** before moving on — a typo in a
   secret name creates nothing and fails silently until Phase 1's first
   request:
   ```bash
   for S in gateway-api-key anthropic-api-key github-read-token \
            azure-tenant-id entra-client-secret langsmith-api-key \
            power-bi-sp-client-id power-bi-sp-client-secret; do
     gcloud secrets versions access latest --secret="$S" >/dev/null \
       && echo "OK   $S" || echo "FAIL $S"
   done
   ```
   Eight `OK` lines and you're done. This proves *your* access; `agent-sa`'s
   comes from the same loop in item 2, so a failure there means re-running it.
4. Read it from Python — a small helper you'll reuse everywhere you need a secret:
   ```python
   from google.cloud import secretmanager

   def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
       client = secretmanager.SecretManagerServiceClient()
       name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
       response = client.access_secret_version(request={"name": name})
       return response.payload.data.decode("UTF-8")
   ```
   Call it once at startup and cache the result in memory — don't call Secret
   Manager on every request; that's unnecessary latency and API calls for a
   value that doesn't change mid-session.

The things that stay purely local, never in Secret Manager, are the non-secret
config values — `GCP_PROJECT_ID`, the Power BI `dataset_id`, `TENANT_ID`, and
the connector's `EXPECTED_AUDIENCE` (the `api://...` URI from Step 14 B2).
Those aren't secrets, just configuration; environment variables or constants in
`app/config.py` are fine. The tenant ID appears in both places on purpose —
it's read from Secret Manager by the MSAL call for consistency with the other
Power BI credentials, and available as plain config where it's just used to
build a URL.

## Step 16 — CI/CD prerequisites (the parts you can do now)

**What belongs here vs. Phase 6:** the *workflow file* can't be written yet —
it references `pytest tests/` and a deploy command for a service that doesn't
exist. But the **identity and credential plumbing** can all be done now, and
doing it here means Phase 6 is just "write the YAML" rather than "write the
YAML and also untangle IAM."

Full explanation of how this all works is in `docs/ci-cd.md` —
this step is just the setup actions.

**1. Create a separate deployer service account.** This is *not* `agent-sa`.
Two different identities with deliberately non-overlapping permissions:

| | `agent-sa` | `github-deployer` |
|---|---|---|
| Used by | The running application | The CI pipeline |
| Gets | BigQuery + Secret Manager access | Deploy rights only |
| Must **not** have | Deploy rights | **Any** data or secret access |

Why they must stay separate, and the `iam.serviceAccountUser` binding below
that's easy to miss: `docs/ci-cd.md`.

```bash
gcloud iam service-accounts create github-deployer \
  --display-name="GitHub Actions Deployer"

gcloud projects add-iam-policy-binding YOUR_PROJECT \
  --member="serviceAccount:github-deployer@YOUR_PROJECT.iam.gserviceaccount.com" \
  --role="roles/run.admin"

# Lets the deployer attach agent-sa to the service it deploys.
# Without this, deploys fail with a confusing permissions error.
gcloud iam service-accounts add-iam-policy-binding \
  agent-sa@YOUR_PROJECT.iam.gserviceaccount.com \
  --member="serviceAccount:github-deployer@YOUR_PROJECT.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

*(`agent-sa` was created in Step 13, so the last command works as written.)*

**2. Generate a key and store it as a GitHub secret.**

```bash
gcloud iam service-accounts keys create github-deployer-key.json \
  --iam-account=github-deployer@YOUR_PROJECT.iam.gserviceaccount.com
```

Then: your repo on github.com → **Settings** → **Secrets and variables** →
**Actions** → **New repository secret**. Name it `GCP_DEPLOYER_KEY`, paste the
**entire contents** of the JSON file as the value.

**Same page, but the "Variables" tab, not "Secrets"** — these two aren't
sensitive, just environment-specific, so they don't belong in Secret Manager
or as GitHub secrets. Add `PROD_SERVICE_ACCOUNT`
(`agent-sa@YOUR_PROJECT.iam.gserviceaccount.com`) and `PROD_REGION` (your
region). Referenced as `vars.PROD_SERVICE_ACCOUNT`/`vars.PROD_REGION` in the
deploy step (`docs/ci-cd.md`) — named this way specifically so a
future `deploy-staging` job needs only its own `STAGING_*` variables, never a
change to the workflow file itself.

```bash
# Delete the local key file immediately after pasting — it's a live credential
# and your .gitignore won't save you if you accidentally commit it from
# somewhere else.
rm github-deployer-key.json
```

> **A JSON key is the deliberate starting point, not the end state** —
> Workload Identity Federation is the upgrade, and why it's deferred is in
> `docs/ci-cd.md`. Don't let it become the finished design by default.

**3. Nothing to add to `.gitignore`** — Step 7 already covers
`*-key.json` and `github-deployer-key.json`, deliberately, so a regenerated
key is ignored even if you forget to delete it. Confirm they're still there
rather than adding them twice.

**4. Nothing to write yet.** `.github/workflows/ci.yml` gets written in Phase
6, when there are tests to run and a service to deploy to. The point of doing
steps 1–3 now is that Phase 6 becomes a single focused task instead of a
tangle of IAM debugging.

## Step 17 — LangSmith tracing

**What this is for:** a rich, per-turn execution trace of the LangGraph agent
loop — every LLM call, tool call, input/output, latency, rendered in a UI.
Genuinely useful once Phase 3's tool-calling loop exists, for watching
whether it gets stuck, loops, or calls the wrong tool — not something you
need working yet, but worth setting up now while you're already doing
account/credential setup for everything else.

**This is not a replacement for `agent_telemetry`** (the BigQuery table from
Phase 1) — that one is structured, queryable, and feeds the judge pipeline;
this one is a rich debugging view, mostly useful while building. A generic
tracing tool has no way to know what "citation compliance" means for this
schema. Add one alongside the other, not instead of it.

**The specific payoff, beyond generic observability:** each iteration of the
tool-calling loop appears as its own step in the trace tree, in order — so
you can *watch* whether the loop got stuck, looped, or called the wrong tool.
That's directly useful for two things already flagged as needing real data:
calibrating `max_iterations`, and checking whether DAX questions
disproportionately exhaust the budget compared to BigQuery ones. Both are
observable during development, well before there's enough production volume
for the judge's aggregate stats to mean anything.

1. **Sign up** at smith.langchain.com if you don't have an account, and
   create an API key from the settings page.
2. **Set two environment variables on your local machine** — the same
   shell-export pattern already used for non-secret config values
   (`config.py` reads via `os.environ`), not something to add to
   `pyproject.toml` or Secret Manager:
   ```bash
   export LANGSMITH_TRACING=true
   export LANGSMITH_API_KEY="lsv2_..."   # the key from item 1
   ```
3. **Also set them in the Cloud Run deploy command, plus one more flag:**
   ```bash
   --set-env-vars=LANGSMITH_TRACING=true,LANGCHAIN_CALLBACKS_BACKGROUND=false
   ```
   **`LANGCHAIN_CALLBACKS_BACKGROUND=false` is not optional.** Trace uploads
   go through a background callback by default, and Cloud Run freezes CPU the
   instant the response is sent — the upload is lost, the same way an
   unawaited telemetry write would be.

   **Know what this sends:** tool inputs and outputs — real `agent_safe`
   query results — go to LangChain's third-party cloud, through a path the
   IAM design doesn't cover. That's an accepted trade-off for a portfolio
   build, where showing the trace is the point (component reference §3), not
   what a production deployment would do.
4. **`LANGSMITH_API_KEY` goes in Secret Manager** like every other deployed
   credential, now that it runs in the deployed service — not a plain env
   var in the deploy command.

**Skip this step for now if you'd rather come back to it in Phase 3** — it
adds no value until the agent loop actually exists to trace.

## Step 18 — Verify everything works end to end

Create a throwaway file `verify_setup.py`:

> Named `verify_setup.py`, not `test_setup.py`, deliberately — a root-level
> file matching `test_*.py` gets picked up by a bare `pytest` run later and
> mixed in with your real suite. Small thing, free to avoid.

```python
# Core service clients — proves GCP auth works. All four are provisioned in
# Step 13; a missing grant or wrong project surfaces here, not in Phase 3.
from google.cloud import bigquery, secretmanager, storage, firestore

client = bigquery.Client()
print("BigQuery client created for project:", client.project)
secretmanager.SecretManagerServiceClient()
storage.Client()
firestore.Client()
print("Secret Manager + Storage + Firestore clients created OK")

# Agent framework
import langgraph, fastmcp, langchain_anthropic
print("LangGraph version:", langgraph.__version__)

# unstructured pulls native deps and is the most likely install to have
# silently half-failed — importing the md partitioner proves the extra landed.
from unstructured.partition.md import partition_md
print("unstructured[md] OK")

# Microsoft-side auth libraries
import msal
import jwt
from jwt.algorithms import RSAAlgorithm   # fails if PyJWT[crypto] wasn't installed
print("msal + PyJWT (with crypto) OK")

# Charting — Agg backend is what Cloud Run needs; confirm it works headless
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
fig, ax = plt.subplots()
sns.barplot(x=["a", "b"], y=[1, 2], ax=ax)
plt.close(fig)
print("matplotlib (Agg) + seaborn OK")

print("Setup looks good.")
```
Run it:
```bash
uv run python verify_setup.py
```
*(Or, with the venv activated the traditional way: `python verify_setup.py` —
identical result, per the note on activation vs. `uv run` in Step 10.)*

**Why these specific extra checks:** `import jwt` succeeds even when the
`[crypto]` extra is missing — the failure only surfaces later, at Phase 1,
as a confusing RS256 verification error. Importing `RSAAlgorithm` directly
forces that failure to happen *now*, during setup, where the fix is obvious.
Same logic for the seaborn/Agg check: rendering one throwaway chart confirms
the headless backend works before you depend on it in a deployed container.

If it prints your project ID and versions with no errors, Python, the venv,
your packages, and your GCP authentication (BigQuery, Secret Manager, Storage,
and Firestore) are all correctly wired together. Delete `verify_setup.py` once confirmed — it was
just a smoke test.

---

## Daily workflow from here on

1. Open the project: `code .` from the folder, or just reopen it in VS Code.
2. Open a terminal inside VS Code (`` Ctrl+` ``) and activate the venv (Step 10) —
   VS Code sometimes does this automatically for integrated terminals once it
   knows the interpreter, but check for the `(.venv)` prefix.
3. Open the Claude Code panel (Spark icon / Activity Bar) and start working.
4. For quick query experiments, create a notebook (`New File` → name it `.ipynb`)
   and pick the `.venv` kernel when prompted.

**Where to go next:** setup is done — building starts at
`docs/build-order.md`, which opens with a status block naming the current
phase. Phase 0 is provisioning you've largely finished here; the first real
code is Phase 1. `CLAUDE.md` is loaded automatically by Claude Code and
indexes everything else.

## Quick troubleshooting

- **`uv` not found after install:** restart your terminal — the installer
  updates your shell's PATH, and that only takes effect in new terminal
  sessions, not the one you ran the installer from.
- **`python`/`.venv` behaving oddly:** confirm you're in the project root
  (`uv venv` and `uv run` both look for `.venv` relative to where you run
  them) and that `uv python list` still shows 3.12.x installed.
- **`gcloud` not found after install:** restart your terminal (PATH changes
  often need a fresh shell) or restart your machine if that doesn't work.
- **VS Code notebook won't find your packages:** wrong kernel selected — click
  the kernel name top-right of the notebook and choose the one inside `.venv`.
- **`ModuleNotFoundError` when running code:** venv isn't activated in that
  terminal — check for `(.venv)` in the prompt; if missing, redo Step 10's
  activate command.
