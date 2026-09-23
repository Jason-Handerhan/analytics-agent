# Authentication & Authorization

> Every identity boundary a **request** crosses, in order — each naming which
> credential is used, who validates it, and which build phase wires it.
> Deploy-time and pipeline-time identities (`github-deployer`, the Vertex
> connection's service account) are out of scope here; see
> `local-dev-environment-setup.md`.
>
> Operational rules that implement these live in `.claude/rules/gateway.md`
> (token validation) and `.claude/rules/tools.md` (the Power BI service
> principal). Setup steps are in `local-dev-environment-setup.md`.

## The chain

**Hop 1 — User → Power Apps (Phase 6, whenever demo-ready).** Standard
Entra tenant app-sharing. The one access-gating decision in the chain — no
code, no IAM.

**Hop 2 — Power Apps → Connector → Gateway (Phase 0 registration, Phase 1
wiring).** Delegated OAuth 2.0 — the token represents the individual signed-in
user, obtained automatically by Power Platform. **Every user's query runs
against the backend as the same `agent-sa`**, so this identity isn't used for
differentiated authorization — it's authentication, plus a `user_id` worth
logging for audit.

**Hop 3 — Gateway boundary validation, the real gate (Phase 1).**
**The validators themselves live in `.claude/rules/gateway.md`** — with the
error handling (malformed header, `PyJWTError`) omitted here. What this shows
is how they *attach*: called directly at the top of the handler body, before
any turn work starts — plain function calls, not FastAPI `Depends()`
(`.claude/rules/gateway.md` has the reasoning). `user_id` falls out of the
validated claims.

```python
@app.post("/ask")
async def ask(body: AskRequest, authorization: str = Header(...),
              x_api_key: str = Header(...)):
    validate_api_key(x_api_key)
    claims = validate_entra_token(authorization)
    user_id = claims["oid"]  # log with telemetry
    ...
```

**Why both factors, and why `jwt.decode()` needs no network call** —
`.claude/rules/gateway.md` carries both, next to the code. The one point it
doesn't make: the API key isn't redundant, it catches a *different* failure
mode (a misconfigured or non-connector caller reaching a valid user's token),
which is defense in depth — the same principle as `agent_safe`'s layers.

**Setup (Step 14) → code mapping:** the connector *resource* app's
(`analytics-agent-connector`, B2) Application ID URI becomes
`EXPECTED_AUDIENCE`; the tenant ID builds both the JWKS and issuer URLs.
**Neither app's client secret is read by this code** — Power Platform uses
the *client* app's secret (`analytics-agent-connector-client`, added after
the resource app hit a persistent `AADSTS90008` as a self-referencing OAuth
client — "The custom connector" section below) upstream to obtain the token;
validating one needs only Entra's public key. `entra-client-secret` in
Secret Manager holds the client app's secret, not the resource app's.
Registration A (the Power BI service principal) is unrelated here — that's
Hop 5, outbound.

**Two deploy flags do two different jobs — both required, neither in code:**

```bash
gcloud run deploy analytics-gateway --service-account=agent-sa@YOUR_PROJECT.iam.gserviceaccount.com \
  --allow-unauthenticated
```

- **`--service-account`** sets what the container **runs as** — *outbound*,
  what it can reach (Hops 4 and 6).
- **`--allow-unauthenticated`** turns off Cloud Run's **inbound** IAM check —
  *who may call it*. That check demands a Google-issued token, and Power
  Platform only has Entra tokens, so it would reject every legitimate
  request. This isn't removing authentication; it moves the gate into the
  code above, which validates the Entra token instead.

Get either wrong and correct application code still fails.

**Hop 4 — Gateway → BigQuery (Phase 0 IAM, Phase 3 code):**
```python
from google.cloud import bigquery
client = bigquery.Client()  # ADC auto-resolves to agent-sa on Cloud Run
```
`agent-sa` needs `bigquery.jobUser` at project level (to run any query) plus
`dataViewer` on `agent_safe` only — **not** on `vector_db`, which is the
dataset boundary (`.claude/rules/tools.md`). Grant commands:
`local-dev-environment-setup.md` Step 13.

**Hop 4b — `search_docs` → `vector_db`, as a different identity.** `agent-sa`
genuinely cannot read `vector_db`, so `search_docs` mints a short-lived
token as **`vector-search-sa`** (which holds `dataViewer` there and nothing
else) via `serviceAccountTokenCreator`. That impersonation is what makes the
dataset boundary IAM-enforced rather than a check in application code — a
hand-written query against the vector tables fails at BigQuery. Code:
`docs/data-pipeline.md`.

**Hop 5 — Gateway → Power BI (Phase 0 registration + role, Phase 3 code):**
```python
import msal

def get_power_bi_token() -> str:
    app = msal.ConfidentialClientApplication(
        get_secret("power-bi-sp-client-id", GCP_PROJECT_ID),
        authority=f"https://login.microsoftonline.com/{get_secret('azure-tenant-id', GCP_PROJECT_ID)}",
        client_credential=get_secret("power-bi-sp-client-secret", GCP_PROJECT_ID),
    )
    result = app.acquire_token_for_client(scopes=["https://analysis.windows.net/powerbi/api/.default"])
    return result["access_token"]
```
**Contributor** — the lowest role meeting `executeQueries`' "Read + Build"
requirement. Build means *can query*, not *can edit*; the endpoint has no
data-modification operation at all, so write risk is zero regardless of role.

**RLS must be off** — a hard blocker, not a simplification: Microsoft doesn't
support service principals on RLS-enabled datasets.

**Two tenant settings first** (Phase 0, Power BI admin portal): "Service
principals can call Fabric public APIs" (older tenants: "Allow service
principals to use Power BI APIs" — same setting) and "Dataset Execute Queries
REST API".

**Hop 6 — Secret Manager (Phase 0):**
```python
from google.cloud import secretmanager
def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    return client.access_secret_version(
        request={"name": f"projects/{project_id}/secrets/{secret_id}/versions/{version}"}
    ).payload.data.decode("UTF-8")
```
`agent-sa` gets `secretmanager.secretAccessor` on each secret (Entra client
secret, gateway API key, Power BI SP credentials).

**No separate local auth path.** ADC resolves `bigquery.Client()` to your own
identity locally and to `agent-sa` on Cloud Run — same code, no `if local:`
branch. Setup: `local-dev-environment-setup.md` Step 12.

---

## The custom connector — Hop 2's surface

**What it is:** the contract between Power Apps and the FastAPI gateway — an
OpenAPI definition registered in Power Platform, plus auth config.

**License:** shares the Power Apps Premium requirement — this connector is the
artifact that triggers it.

**One connector, not one per endpoint.** It's registered from a single
spec, and all five gateway endpoints come through as separate operations on
it — `MyConnector.Ask(...)`, `MyConnector.GetStatus(...)`, and so on.

**Two Entra apps, not one — confirmed necessary, not just planned.**
`analytics-agent-connector` is the OAuth *resource* (exposes `access_as_user`;
its Application ID URI is `EXPECTED_AUDIENCE`). A second app,
`analytics-agent-connector-client`, is the OAuth *client* Power Platform
authenticates as. A single self-referencing app (playing both roles) hit a
persistent `AADSTS90008` — "must require access to Microsoft Graph" —
regardless of permissions, consent, or Conditional Access; splitting into two
apps fixed it immediately. The client app needs its own delegated permission
to the resource app's `access_as_user` scope, admin-consented, and its own
redirect URI registered (the one Power Platform's Security tab generates
after the connector is first saved).

**Power Platform's importer can't parse FastAPI's OpenAPI output.** FastAPI
emits OpenAPI 3.1.0; the importer only handles 2.0 (Swagger) reliably, with
partial/no 3.0+ support — confirmed via a hard parse failure, not a
field-level complaint. Fix: hand-write an equivalent Swagger 2.0 spec from
the Pydantic models and import that file instead of `/openapi.json` directly.

**Tokens are v1-format, not v2** — Power Platform's "Azure Active Directory"
OAuth provider issues `sts.windows.net`-issuer tokens regardless of anything
configured on the connector side. `validate_entra_token`
(`.claude/rules/gateway.md`) checks for this format; a token showing a
`login.microsoftonline.com/.../v2.0` issuer here would be unexpected, not
correct.

**Build steps:**
1. Create the connector from a hand-written Swagger 2.0 spec (see above).
   Host = your Cloud Run URL.
2. **Auth:** OAuth 2.0 via Entra ID, using the *client* app's credentials
   (not the resource app's) — Client ID, Client secret, Tenant ID, Resource
   URL = the resource app's Application ID URI, Scope = `access_as_user`.
   This flows user identity through to the gateway, which authenticates the
   *caller* (`.claude/rules/gateway.md`) — separate from what the *service
   account* can reach on the backend.
3. **Send `x-api-key` on every operation** — the gateway enforces *two*
   factors, and OAuth only supplies one (`.claude/rules/gateway.md`). Add it
   as a header parameter with a default value of the `gateway-api-key` secret,
   so callers never see or set it. Miss this and every call 401s with valid
   OAuth, which reads as an auth-config problem and isn't one.

   **Don't also define `Authorization` as a manual header parameter** — Power
   Platform reserves it once OAuth 2.0 is configured and rejects the
   definition outright; it injects the bearer token itself.
4. Define request/response schemas so Power Apps gets typed objects
   (`answer_markdown`, `chart_url`, etc. bindable directly in the gallery —
   snake_case, straight from the Pydantic model).
5. Test in the connector's Test tab before wiring into the app — this catches
   a missing `x-api-key` immediately, before any Power Apps wiring.
