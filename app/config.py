from google.cloud import secretmanager

# GCP Project Configuration
GCP_PROJECT_ID = "instacart-ml-model"
GCS_CHART_BUCKET = "instacart-ml-model-charts"

# Microsoft Entra ID Configuration
TENANT_ID = "7e6d319c-ffb2-4bbf-8865-d2e7580a8998"
EXPECTED_AUDIENCE = "api://4b86032f-4507-4239-bf90-a9b1c33c571c"

# Power BI Dataset ID
POWER_BI_DATASET_ID = "03fe95af-12af-4e00-bbd6-241e942f76bc"
POWER_BI_WORKSPACE_ID = "8e20abd3-703e-46b8-9832-950249767864"

# LangSmith tracing — literals, not env vars, since both are identical in
# every environment (local-dev-environment-setup.md Step 17).
# LANGSMITH_API_KEY is a secret, fetched via get_secret() once that helper
# exists (Phase 1) — not defined here.
LANGSMITH_TRACING = "true"
LANGCHAIN_CALLBACKS_BACKGROUND = "false"


def get_secret(secret_id: str, project_id: str, version: str = "latest") -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")