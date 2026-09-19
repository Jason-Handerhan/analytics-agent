# Core service clients — proves GCP auth works. All four are provisioned in
# Step 13; a missing grant or wrong project surfaces here, not in Phase 3.
from google.cloud import bigquery, secretmanager, storage, firestore
from app.config import GCP_PROJECT_ID, GCS_CHART_BUCKET

bq_client = bigquery.Client(project=GCP_PROJECT_ID)
print("BigQuery client created for project:", bq_client.project)
secretmanager.SecretManagerServiceClient()
storage_client = storage.Client(project=GCP_PROJECT_ID)
firestore.Client(project=GCP_PROJECT_ID)
print("Secret Manager + Storage + Firestore clients created OK")

# Auth working isn't the same as the resources existing — Step 13 created
# three datasets and a bucket by exact name; a typo surfaces here, not as a
# confusing "not found" mid-Phase-1.
for dataset in ("agent_safe", "vector_db", "staging"):
    bq_client.get_dataset(f"{GCP_PROJECT_ID}.{dataset}")
print("BigQuery datasets agent_safe/vector_db/staging all exist")

storage_client.get_bucket(GCS_CHART_BUCKET)
print(f"Chart bucket gs://{GCS_CHART_BUCKET} exists")

# Web framework
import fastapi, uvicorn
print("fastapi + uvicorn OK")

# Agent framework — all three model providers `build_static_context`
# branches on (.claude/rules/gateway.md), not just the one you're starting
# with, plus the MCP adapter layer and langchain-core itself.
import importlib.metadata
import langgraph, fastmcp
import langchain_core, langchain_anthropic, langchain_openai, langchain_google_genai
import langchain_mcp_adapters
print("LangGraph version:", importlib.metadata.version("langgraph"))  # no __version__ attr
print("LangChain (core + anthropic + openai + google-genai) + MCP adapters OK")

# unstructured pulls native deps and is the most likely install to have
# silently half-failed — importing both partitioners proves it landed.
# partition_html has no separate extra of its own — it's covered by
# unstructured[md] already (confirmed 2026-09-13) — but docs/data-pipeline.md
# depends on it just as much for the docs vector index (*.html alongside *.md),
# so it gets its own check rather than being assumed to come along for free.
from unstructured.partition.md import partition_md
from unstructured.partition.html import partition_html
print("unstructured[md] + partition_html OK")

# Everything else in pyproject.toml with no gotcha worth a dedicated check —
# a plain import is enough to catch a broken/missing install.
import numpy, pandas, pydantic, pysbd, requests, mistune
print("numpy + pandas + pydantic + pysbd + requests + mistune OK")

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
