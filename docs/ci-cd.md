# CI/CD — GitHub Actions

> The workflow, the quality gate, and the two service accounts it needs.
> Built in **Phase 6**, not before — automating a deploy pipeline before
> there's something worth protecting with a quality gate is process ahead of
> substance.
>
> Deploy commands and the `github-deployer` setup are in
> `local-dev-environment-setup.md` Step 16.

## The mental model — three places tests run, not one

There's no single "push to prod" moment that tests gate. They run at three
points, layered rather than competing:

1. **Locally, while writing code** — the primary loop, not a backup.
   `pytest tests/ -v` in seconds, never touches GitHub.
2. **Automatically on every push** — a safety net for forgetting to run them
   locally, running against stale code, or (later) someone else's change.
3. **As a deploy gate** — the *same* Layer 1 suite from #2, wired so a
   failure blocks deployment. Not a separate test run.

## Pipelines are code, not UI

GitHub Actions workflows are YAML in `.github/workflows/*.yml`, version-
controlled like everything else. There's no visual pipeline builder. **The UI
does exactly three things:** shows run history/logs (Actions tab), shows ✅/❌
per commit and PR, and configures **repository secrets** (Settings → Secrets
and variables → Actions) — that last one is the only place you click rather
than write code, because credentials must never sit in a YAML file in a public
repo.

## Execution model, in one paragraph

A trigger spins up a **fresh VM** (`ubuntu-latest`), checks out your code, runs
your steps, tears the VM down. **Nothing persists between runs** unless
explicitly cached — this surprises people, because it feels like it should
remember. Workflows contain **jobs**; jobs contain **steps**. Steps run in order
on one VM; jobs run in parallel on separate VMs unless chained with `needs:`.

## The workflow (illustrative — verify current action versions at build time)

```yaml
# .github/workflows/ci.yml
name: CI/CD

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v10        # v10.0.1 current; v3 is long stale
        with:
          python-version: "3.12"            # confirmed valid input — sets UV_PYTHON
          enable-cache: true
      - run: uv sync --dev
      - run: uv run pytest tests/ -v
        # NO GCP credentials in this job — deliberate. This runs only the
        # Layer 1 suite (docs/testing.md), which needs none. If this
        # job ever needs a credential, the problem is what's being tested
        # here, not the pipeline.
        # `uv sync` installs the exact locked versions from uv.lock — the
        # same versions tested locally, not whatever pip's resolver picks
        # given the version constraints at CI time.

  deploy:
    needs: test                    # <- the quality gate; won't start if test failed
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_DEPLOYER_KEY }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Build and push image
        run: |
          gcloud builds submit \
            --tag us-docker.pkg.dev/YOUR_PROJECT/analytics-gateway/analytics-gateway:${{ github.sha }}
      - name: Deploy to production
        run: |
          gcloud run deploy analytics-gateway \
            --image us-docker.pkg.dev/YOUR_PROJECT/analytics-gateway/analytics-gateway:${{ github.sha }} \
            --service-account=${{ vars.PROD_SERVICE_ACCOUNT }} \
            --allow-unauthenticated \
            --region=${{ vars.PROD_REGION }} \
            --set-env-vars=<every var from CLAUDE.md's deploy command>
            # NOT a subset. Omitting EXPECTED_AUDIENCE breaks auth,
            # GCS_CHART_BUCKET breaks charts, POWER_BI_DATASET_ID breaks DAX —
            # all at runtime, none at deploy. One canonical list, in CLAUDE.md.
```

**Build and deploy are two steps, not one `--source .` call — deliberately,
even with a single environment.** The image is tagged with `${{ github.sha }}`
— the exact git commit — so "what's running in production right now" is
always a specific, addressable artifact, not just "whatever the last build
happened to produce." Rolling back means redeploying a prior tag, no
rebuild, no risk the rebuild produces something subtly different from what
was actually tested.

**`--service-account` and `--region` come from GitHub Actions variables
(`vars.PROD_*`), not hardcoded inline.** This is the part that actually pays
off later, not the split itself: adding a second environment means a new
step referencing `vars.STAGING_*`, deploying the *same* image tag — never a
rearchitecture. A second environment isn't planned; what it would take, and
why it's deferred, is in the component reference.

**`needs: test` *is* the quality gate** — not a separate concept, just a job
dependency. If `test` fails, `deploy` never starts.

## Two service accounts, not one — the part that's easy to get wrong

`agent-sa` runs the **application**; `github-deployer` runs the **pipeline**.
Deliberately non-overlapping: the deployer gets `run.admin` and
`iam.serviceAccountUser` on `agent-sa`, and **no** BigQuery or Secret Manager
access at all.

Reusing `agent-sa` for CI would hand the pipeline a credential with live
access to your data and secrets — permissions it never needs just to redeploy
a container. Same least-privilege reasoning already applied to `agent_safe`
(`.claude/rules/mcp-tools.md`), a different identity.

**The binding people miss:** `iam.serviceAccountUser` on `agent-sa`. Without
it the deploy fails with a confusing permissions error, because attaching a
service account to a Cloud Run service is itself a permissioned action.

Commands and the GitHub secret setup: `local-dev-environment-setup.md`
Step 16.

## Credential strategy — start simple, upgrade deliberately

**Phase 6 starts key-based:** a JSON key for `github-deployer` in the
`GCP_DEPLOYER_KEY` repo secret. The honest downside is a long-lived
credential in GitHub's secret store, valid until manually revoked.

**The upgrade is Workload Identity Federation** — no stored key at all.
GitHub presents a short-lived signed token proving *"this is a run of your
repo's workflow"* and GCP exchanges it for a temporary credential. More setup
(a trust relationship between GCP and GitHub), which is why starting
key-based and migrating is reasonable rather than lazy.

## Daily workflow once this exists

```bash
pytest tests/ -v                       # everything
pytest tests/test_guardrails.py -v   # one file
pytest -k "guardrail" -v               # name pattern

git add . && git commit -m "..." && git push origin main
# -> Actions runs `test`; if green, `deploy` runs; check the Actions tab
```

Nothing changes about local development — CI is an automatic double-check
after the fact, plus the thing that redeploys so you don't run
`gcloud run deploy` by hand.

---
