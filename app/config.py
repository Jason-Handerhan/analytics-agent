import pathlib

from google.cloud import secretmanager

# GCP Project Configuration
GCP_PROJECT_ID = "instacart-ml-model"
GCS_CHART_BUCKET = "instacart-ml-model-charts"

# Repo-relative paths
CONTEXT_DIR = pathlib.Path(__file__).resolve().parent.parent / "context"

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

# Conversation history
HISTORY_TURN_COUNT = 5    # sessions.recent_messages FIFO length
HISTORY_ROW_CAP = 100     # rows kept per stored tool result

# Orchestrator loop guardrail settings
MAX_ANSWER_CHARS = 6000
MAX_ANSWER_TABLE_ROWS = 25  # cap on any single markdown table in an answer -- a display limit, not a fetch limit
MAX_LENGTH_RETRIES = 2
MAX_VERIFY_RETRIES = 2
BIGQUERY_TIMEOUT_SECONDS = 40
BIGQUERY_ROW_CAP = 1000  # cap on rows fetched -- LIMIT doesn't reduce bytes scanned
DAX_ROW_CAP = BIGQUERY_ROW_CAP  # same constraint as BigQuery's cap, not platform-specific
MAX_ITERATIONS = 10  # max tool-call rounds per turn before falling back to a partial answer

# MCP server (app/main.py, app/orchestrator/orchestrator.py) -- co-located with the gateway for now
MCP_HOST = "127.0.0.1"
MCP_PORT = 8001
MCP_SERVER_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"
MCP_SERVER_HEADERS: dict[str, str] = {}
MCP_SERVER_NAME = "analytics"

# BigQuery cost guardrails
BIGQUERY_PRICE_PER_TIB = 6.25  # dollars per TiB (2**40 bytes) -- BigQuery bills in binary TiB, not decimal TB
MAX_BYTES_BILLED = 2 * 1024 ** 3  # 2 GiB -- per-query engine-level fail-safe (maximum_bytes_billed)
ABSOLUTE_CAP = 3 * 1024 ** 3  # 3 GiB -- turn-cumulative cap, hard-declines further queries once hit

# LangSmith tracing
LANGSMITH_TRACING = "true"
LANGCHAIN_CALLBACKS_BACKGROUND = "false"


def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")