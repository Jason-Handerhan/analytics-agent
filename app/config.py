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
MAX_LENGTH_RETRIES = 2
MAX_VERIFY_RETRIES = 2
BIGQUERY_TIMEOUT_SECONDS = 40
BIGQUERY_ROW_CAP = 1000  # cap what's fetched, not the SQL text -- a cheap,
                          # well-filtered query with no LIMIT can still
                          # return far more rows than belongs in an answer
                          # or the model's context (.claude/rules/tools.md)
MAX_ITERATIONS = 10  # calibration starting point, not tuned -- exhaustion
                      # degrades to a partial answer, so a high cap costs
                      # nothing; iteration_count is logged per turn to set
                      # the real value later (.claude/rules/orchestrator.md)

# MCP server (app/main.py, app/orchestrator/orchestrator.py). Co-located with
# the gateway for now
# MCP_SERVER_URL/MCP_SERVER_HEADERS are the only things that need to
# change if it moves to its own service.
MCP_HOST = "127.0.0.1"
MCP_PORT = 8001
MCP_SERVER_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"
MCP_SERVER_HEADERS: dict[str, str] = {}
MCP_SERVER_NAME = "analytics"

# BigQuery cost guardrails (.claude/rules/tools.md, docs/approval-workflow.md)
# $6.25 per TiB (2**40 bytes) -- confirmed against current on-demand pricing,
# 2026-09-23. BigQuery bills in binary TiB despite being marketed as "per TB";
# using the wrong divisor here would silently under/over-estimate cost by ~10%.
BIGQUERY_PRICE_PER_TIB = 6.25
MAX_BYTES_BILLED = 1024 ** 3      # 1 GiB -- engine-level fail-safe ceiling
                                   # per query, independent of the tiers
                                   # below. Picked from real dry-run figures
                                   # against agent_safe: well-formed queries
                                   # here top out ~300MB; a SELECT * mistake
                                   # on the largest table hits ~6.8GB -- this
                                   # catches that with margin above normal use.
ABSOLUTE_CAP = 3 * MAX_BYTES_BILLED  # 3 GiB -- turn-cumulative hard decline,
                                      # checked via dry-run in call_tool_node
                                      # before dispatch (.claude/rules/tools.md).
                                      # Deliberately > MAX_BYTES_BILLED so a
                                      # few real queries can combine in one
                                      # turn without tripping this too; still
                                      # well under the ~6.8GB mistake case.
                                      # BIG_QUERY_THRESHOLD (the softer,
                                      # approval-pause tier) isn't set yet --
                                      # needs route_entry/execute_approved
                                      # (item 11) to mean anything.

# LangSmith tracing
LANGSMITH_TRACING = "true"
LANGCHAIN_CALLBACKS_BACKGROUND = "false"


def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")