import pathlib

from google.cloud import secretmanager

# GCP Project Configuration
GCP_PROJECT_ID = "instacart-ml-model"
GCS_CHART_BUCKET = "instacart-ml-model-charts"
AGENT_SA_EMAIL = "agent-sa@instacart-ml-model.iam.gserviceaccount.com"
CHART_URL_EXPIRATION_HOURS = 168

# Repo-relative paths
CONTEXT_DIR = pathlib.Path(__file__).resolve().parent.parent / "context"
PAGE_INFO_DIR = CONTEXT_DIR / "page_info"

# LLM
AVAILABLE_MODELS = {
    "claude-sonnet-5": "anthropic",
    "gpt-5.6-terra": "openai",
    "gemini-3.1-pro-preview": "gemini",
}
MODEL = "claude-sonnet-5"  # must be a key in AVAILABLE_MODELS
MODEL_PROVIDER = AVAILABLE_MODELS[MODEL]

# Microsoft Entra ID Configuration
TENANT_ID = "7e6d319c-ffb2-4bbf-8865-d2e7580a8998"
EXPECTED_AUDIENCE = "api://4b86032f-4507-4239-bf90-a9b1c33c571c"

# Power BI Dataset ID
POWER_BI_DATASET_ID = "03fe95af-12af-4e00-bbd6-241e942f76bc"
POWER_BI_WORKSPACE_ID = "8e20abd3-703e-46b8-9832-950249767864"

# agent_safe (the only dataset agent-sa can read)
AGENT_SAFE_DATASET = "agent_safe"
PREDICTION_EXPLANATIONS_TABLE = "prediction_explanations"

# Raw feature column -> the label the Power BI dashboard uses.
SHAP_FEATURE_DISPLAY_NAMES = {
    "user_prod_avg_days_between_purchases": "Avg Days Between Product Purchases",
    "user_product_consecutive_order_skips": "Consecutive Product Order Skips",
    "label_user_product_days_since_last_purchased": "Days Since Last Purchase",
    "user_product_reorder_concentration": "Product Reorder Concentration",
    "user_product_purchase_concentration": "Product Purchase Concentration",
    "user_reorder_ratio": "User Reorder Rate",
    "user_deptartment_share": "Department Share of Order History",
    "user_product_nearing_reorder_ratio": "Nearing Reorder Threshold Ratio",
    "user_product_avg_cart_position": "Avg Cart Position",
    "user_product_order_penetration_ratio": "Product Order Penetration Rate",
    "user_product_cumulative_purchases": "Cumulative Product Purchases",
    "user_product_last3order_streak": "Last 3 Orders Purchase Streak",
    "user_distinct_product_count": "Distinct Product Count",
    "user_product_cart_position_last_order": "Last Order Cart Position",
    "user_aisle_share": "Aisle Share of Order History",
}

# agent_telemetry
TELEMETRY_DATASET = "telemetry"
TELEMETRY_TABLE = "agent_telemetry"

# agent_chat_history -- restorable past conversations, not analytics
CHAT_HISTORY_DATASET = "agent_chat_history"
CHAT_HISTORY_TABLE = "turns" # chat history for front end
DELETED_CONVERSATIONS_TABLE = "deleted_conversations"  # soft-delete marker rows,
                                                        # same dataset as CHAT_HISTORY_TABLE

CONVERSATION_FINDINGS_TABLE = "conversation_findings"  # uses chat_history_dataset -- verified-turn history for agent retrieval
TURN_RANGE_SUMMARIES_TABLE = "turn_range_summaries"  # same dataset -- one summary per compaction, over a range of turns
CONVERSATION_LIST_LIMIT = 50  # GET /conversations -- most recent N
CONVERSATION_TITLE_MAX_CHARS = 60  # truncation length for a conversation's title

# Both telemetry and chat-history writes go through this identity, reached
# only via impersonation
TURN_WRITER_SA_EMAIL = "turn-writer-sa@instacart-ml-model.iam.gserviceaccount.com"

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
SESSIONS_TTL_DAYS = 30    # sessions documents auto-delete this many days after last activity
MEMORY_RECALL_TOKEN_BUDGET = 20_000    # max approx tokens one get_conversation_findings call returns; placeholder, tune

# Compaction
COMPACTION_HISTORY_TOKEN_THRESHOLD = 32_000    # estimated history tokens (chars // 4) that trigger it; low for testing, raise later
COMPACTION_HISTORY_MAX_BYTES = 600_000    # stored-history bytes that trigger it; low for testing
COMPACTION_KEEP_TURNS = 2    # newest stored turns left verbatim; everything older is folded (3 for prod--> set to to two for testing)
COMPACTION_MODEL = "claude-haiku-5-5"    # not in AVAILABLE_MODELS -- the user never picks it

# Live status
STATUS_MAX_NAMED_SOURCES = 3    # call_tool status names up to this many sources, then a count

# Orchestrator loop guardrail settings

# Tools whose numbers verify_node checks against -- queries, plus combine_results
NUMERIC_SOURCE_TOOLS = {"run_bigquery_sql", "run_dax_query", "combine_results"}

# Tools whose results carry verified_numbers from earlier turns -- only their verified numbers
#  field feeds the pool
MEMORY_SOURCE_TOOLS = {"get_conversation_findings", "search_conversation_findings"}

# Tools whose results carry a chart_url
CHART_TOOLS = {"generate_chart", "generate_beeswarm"}

MAX_ANSWER_CHARS = 14000
MAX_ANSWER_TABLE_ROWS = 25  # cap on any single markdown table in an answer -- a display limit, not a fetch limit
MAX_LENGTH_RETRIES = 2
MAX_VERIFY_RETRIES = 3
MAX_CLAIM_PRECISION = 5  # decimals past this are float-reproduction noise,
                          # not a real mismatch -- claim_matches_pool caps to it
BIGQUERY_TIMEOUT_SECONDS = 40
BIGQUERY_ROW_CAP = 1000  # cap on rows fetched -- LIMIT doesn't reduce bytes scanned
DAX_ROW_CAP = BIGQUERY_ROW_CAP  # same constraint as BigQuery's cap, not platform-specific
DAX_TIMEOUT_SECONDS = BIGQUERY_TIMEOUT_SECONDS  # same turn-budget reasoning as BigQuery's timeout
MAX_ITERATIONS = 50  # backstop on tool-call rounds per turn; the turn timeout ends a turn long before this

# Gateway request/turn guardrails
MAX_QUESTION_CHARS = 2000  # generous for a real question, not a paste -- rejected before a turn starts
GATEWAY_TURN_TIMEOUT_SECONDS = 115  # whole-turn budget before returning a fallback AgentResponse
TURN_WARNING_SECONDS_REMAINING = 35  # the agent gets a time check once this much of the budget is left

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

# answer_html styling -- Power Apps' HTML text control doesn't reliably
# honor a <style> block, so every styled element in the custom mistune
# renderer builds its style inline from these.
ANSWER_FONT_FAMILY = "Segoe UI, sans-serif"

#Font Sizes
ANSWER_BODY_FONT_SIZE = "24px"
ANSWER_HEADING_FONT_SIZE = "32px"
ANSWER_SUBHEADING_FONT_SIZE = "27px"
ANSWER_CODE_FONT_SIZE = "22px"
ANSWER_TABLE_FONT_SIZE = "24px"
ANSWER_CODE_FONT_FAMILY = "Consolas, Monaco, 'Courier New', monospace"
ANSWER_CODE_BLOCK_BG = "#282c34"
ANSWER_CODE_BLOCK_TEXT = "#e8e8e8"
ANSWER_INLINE_CODE_BG = "#e3e5e8"
ANSWER_INLINE_CODE_BORDER = "#c8cbd0"
ANSWER_INLINE_CODE_TEXT = "#1a1a1a"
ANSWER_TABLE_HEADER_BG = "#5a6572"
ANSWER_TABLE_HEADER_BORDER = "#4a5260"
ANSWER_TABLE_HEADER_TEXT = "#ffffff"
ANSWER_TABLE_CELL_BORDER = "#ccc"
ANSWER_TABLE_CELL_TEXT = "#1a1a1a"


def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")