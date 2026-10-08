import pathlib

from google.cloud import secretmanager

# GCP Project Configuration
GCP_PROJECT_ID = "instacart-ml-model"
GCS_CHART_BUCKET = "instacart-ml-model-charts"
AGENT_SA_EMAIL = "agent-sa@instacart-ml-model.iam.gserviceaccount.com"
CHART_URL_EXPIRATION_HOURS = 1.0

# Repo-relative paths
CONTEXT_DIR = pathlib.Path(__file__).resolve().parent.parent / "context"
PAGE_INFO_DIR = CONTEXT_DIR / "page_info"

# LLM
MODEL = "claude-sonnet-5"

# Microsoft Entra ID Configuration
TENANT_ID = "7e6d319c-ffb2-4bbf-8865-d2e7580a8998"
EXPECTED_AUDIENCE = "api://4b86032f-4507-4239-bf90-a9b1c33c571c"

# Power BI Dataset ID
POWER_BI_DATASET_ID = "03fe95af-12af-4e00-bbd6-241e942f76bc"
POWER_BI_WORKSPACE_ID = "8e20abd3-703e-46b8-9832-950249767864"

# agent_safe (the only dataset agent-sa can read)
AGENT_SAFE_DATASET = "agent_safe"

# agent_telemetry
TELEMETRY_DATASET = "telemetry"
TELEMETRY_TABLE = "agent_telemetry"

# vector_db (search_docs, reached only via vector-search-sa impersonation --
# agent-sa has no grant on this dataset)
VECTOR_DB_DATASET = "vector_db"
VECTOR_DB_TABLE = "chunks_docs_embedded"
EMBEDDING_MODEL = "staging.embedding_model"  # wraps gemini-embedding-001
VECTOR_SEARCH_SA_EMAIL = "vector-search-sa@instacart-ml-model.iam.gserviceaccount.com"
SEARCH_DOCS_TOP_K_DEFAULT = 5
SEARCH_DOCS_MAX_TOP_K = 20

# GitHub (get_repo_contents, MCP-hosted)
GITHUB_REPO_OWNER = "Jason-Handerhan"
GITHUB_REPO_NAME = "Kaggle-Instacart-Reorder-Engine-Portfolio-Project"
GITHUB_REPO_BRANCH = "main"
MAX_REPO_FILE_CONTENT_CHARS = 300_000  # final cap on returned text, after any stripping

# Conversation history
HISTORY_TURN_COUNT = 3    # sessions.history_messages FIFO length (testing; 5 for prod)
SESSIONS_TTL_DAYS = 30    # sessions documents auto-delete this many days after last_activity_at

# Live status
STATUS_MAX_NAMED_SOURCES = 3    # call_tool status names up to this many sources, then a count

# Orchestrator loop guardrail settings

# Tools whose numbers verify_node checks against -- queries, plus combine_results,
# which only recombines them (including null-to-0 fills). Used by build_numeric_pool
# (orchestrator.py) and rebuild_paused_messages (entry_exit.py) -- shared data, not
# config in the traditional sense, but pure and dependency-free either way.
NUMERIC_SOURCE_TOOLS = {"run_bigquery_sql", "run_dax_query", "combine_results"}

MAX_ANSWER_CHARS = 6000
MAX_ANSWER_TABLE_ROWS = 25  # cap on any single markdown table in an answer -- a display limit, not a fetch limit
MAX_LENGTH_RETRIES = 2
MAX_VERIFY_RETRIES = 3
MAX_CLAIM_PRECISION = 5  # decimals past this are float-reproduction noise,
                          # not a real mismatch -- claim_matches_pool caps to it
BIGQUERY_TIMEOUT_SECONDS = 40
BIGQUERY_ROW_CAP = 1000  # cap on rows fetched -- LIMIT doesn't reduce bytes scanned
DAX_ROW_CAP = BIGQUERY_ROW_CAP  # same constraint as BigQuery's cap, not platform-specific
DAX_TIMEOUT_SECONDS = BIGQUERY_TIMEOUT_SECONDS  # same turn-budget reasoning as BigQuery's timeout
MAX_ITERATIONS = 10  # max tool-call rounds per turn before falling back to a partial answer

# Gateway request/turn guardrails
MAX_QUESTION_CHARS = 2000  # generous for a real question, not a paste -- rejected before a turn starts
GATEWAY_TURN_TIMEOUT_SECONDS = 115  # whole-turn budget before returning a fallback AgentResponse

# MCP server (app/main.py, app/orchestrator/orchestrator.py) -- co-located with the gateway for now
MCP_HOST = "127.0.0.1"
MCP_PORT = 8001
MCP_SERVER_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"
MCP_SERVER_HEADERS: dict[str, str] = {}
MCP_SERVER_NAME = "analytics"

# BigQuery cost guardrails
BIGQUERY_PRICE_PER_TIB = 6.25  # dollars per TiB (2**40 bytes) -- BigQuery bills in binary TiB, not decimal TB
ABSOLUTE_CAP = 4 * 1024 ** 3  # 4 GiB -- turn-cumulative cap, hard-declines further queries once hit
MAX_BYTES_BILLED = ABSOLUTE_CAP  # per-query fail-safe (maximum_bytes_billed)
PENDING_APPROVAL_THRESHOLD = ABSOLUTE_CAP // 2  # batch bytes above this pause for approval

# LangSmith tracing
LANGSMITH_TRACING = "true"
LANGCHAIN_PROJECT = "analytics-agent"


def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")