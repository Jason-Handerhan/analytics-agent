"""The static context bundle: orientation, schema registries, few-shot
examples, and system instructions, assembled into one cached system-prompt
payload per model (.claude/rules/gateway.md).

BIGQUERY_SCHEMA and the final assembly need a live BigQuery call, so both
are lazy (@lru_cache-decorated functions, called on first real use) rather
than bare module-level constants -- eager construction at import would make
importing this module require live credentials, breaking Layer 1 tests
(the same reasoning already applied to a Firestore client elsewhere in this
project). Everything else here is either a plain file read or authored text,
so it's safe to compute eagerly at import.
"""
from functools import lru_cache

from google.cloud import bigquery

from app.config import AGENT_SAFE_DATASET, CONTEXT_DIR, GCP_PROJECT_ID, MODEL
from app.model_schema import MEASURE_REGISTRY, PARAMETERS, RELATIONSHIPS, TABLE_REGISTRY

ORIENTATION_DIR = CONTEXT_DIR / "orientation"
ORIENTATION_FILES = ["executive_summary.txt", "project_navigator.txt", "system_architecture.txt"]

ORIENTATION_PREAMBLE = """This section is background on the Instacart reorder-recommendation ML project
this dashboard evaluates, extracted from that project's own documentation --
not live data. Numbers in it (Recall@5, profit-lift figures, etc.) are
historical, point-in-time report figures; never cite them as an answer -- a
user-facing number always needs its own live run_bigquery_sql/run_dax_query
call this turn.

The Project Navigator is that documentation's table of contents. Each item
names a topic search_docs can retrieve in more depth -- treat it as an index
of what's available, not a substitute for looking something up. System
Architecture is the one exception: it's included here in full below, not
indexed separately, so search_docs won't return anything more on it.""".strip()


def build_orientation_bundle() -> str:
    sections = [(ORIENTATION_DIR / name).read_text(encoding="utf-8").strip() for name in ORIENTATION_FILES]
    return ORIENTATION_PREAMBLE + "\n\n" + "\n\n---\n\n".join(sections)


ORIENTATION_BUNDLE = build_orientation_bundle()


def _render_bigquery_schema(client: bigquery.Client, dataset: str) -> str:
    header = f"BIGQUERY SCHEMA -- every table in {dataset}, with its columns, types, and descriptions."
    blocks = []
    for tbl_ref in client.list_tables(dataset):
        table = client.get_table(tbl_ref)
        lines = [f"### {table.table_id}"]
        if table.description:
            lines.append(table.description)
        for field in table.schema:
            desc = f": {field.description}" if field.description else ""
            lines.append(f"- {field.name} ({field.field_type}){desc}")
        blocks.append("\n".join(lines))
    return header + "\n\n" + "\n\n".join(blocks)


@lru_cache
def get_bigquery_schema() -> str:
    client = bigquery.Client(project=GCP_PROJECT_ID)
    return _render_bigquery_schema(client, AGENT_SAFE_DATASET)


DAX_FEW_SHOT_EXAMPLES = '''DAX EXAMPLES

Q: "What was the champion model's Recall@5 on the test set?"
EVALUATE
CALCULATETABLE(
    ROW("Champion Recall", [Champion Recall]),
    dataset_split_dimension[dataset_split] = "Test"
)
Note: evaluation_metrics has one row per model per split -- omitting a split filter sums across all of them. Filter dataset_split_dimension[dataset_split] explicitly, from filter_context when present.

Q: "What would the annual profit lift be if churn rate were 45% instead of the current assumption?"
DEFINE VAR ChampionModel = [Champion_Model]
EVALUATE
CALCULATETABLE(
    ROW("Annual Profit Lift", [Annual_Profit_Lift]),
    'Churn Rate'[Churn Rate] = 0.45,
    evaluation_metrics[Model] = ChampionModel,
    dataset_split_dimension[dataset_split] = "Test"
)
Note: a measure can't be a filter's comparison value directly in CALCULATETABLE -- assign it to a VAR first. Scenario questions usually need split, model, and parameter filters together, not just the one being asked about.

Q: "What are the top 5 most important features for the XGBoost Ranker model?"
EVALUATE
TOPN(
    5,
    FILTER(power_bi_shap_barplots, power_bi_shap_barplots[model_name] = "XGBoost Ranker" && power_bi_shap_barplots[split] = "test"),
    power_bi_shap_barplots[mean_absolute_shap], DESC
)
Note: TOPN selects the right rows, but executeQueries doesn't guarantee they arrive sorted -- sort client-side before presenting a ranked list.

Q: "What are the top SHAP features for the champion model?"
Note: power_bi_shap_barplots only covers the four base models -- an ensemble like the champion model has no SHAP decomposition. Use a specific base model, or say none exists, rather than filtering on [Champion_Model] and getting nothing back.'''

BIGQUERY_FEW_SHOT_EXAMPLES = '''BIGQUERY EXAMPLES

Q: "What's the reorder rate by department?"
SELECT department, ROUND(AVG(reordered), 4) AS reorder_rate, COUNT(*) AS n
FROM `instacart-ml-model.agent_safe.product_order_analysis`
GROUP BY department
ORDER BY reorder_rate DESC
Note: reordered is a 0/1 flag at the order-line grain, so AVG(reordered) grouped by department gives a true per-department reorder rate directly.

Q: "On average, how many distinct products has each user ordered?"
SELECT ROUND(AVG(user_distinct_product_count), 2) AS avg_distinct_products
FROM (
  SELECT DISTINCT user_id, user_distinct_product_count
  FROM `instacart-ml-model.agent_safe.product_order_analysis`
)
Note: user_-prefixed columns (user_distinct_product_count, user_reorder_ratio, user_avg_basket_size, etc.) are precomputed per user and repeat on every order-line row for that user. Averaging one directly over product_order_analysis over-weights users with more order lines -- deduplicate to one row per user first.

Q: "Do users who reorder products more quickly have a higher reorder rate for that product?"
SELECT
  CASE WHEN user_prod_avg_days_between_purchases < 7 THEN 'under 7 days' ELSE '7+ days' END AS pace_bucket,
  ROUND(AVG(label_reordered), 4) AS reorder_rate,
  COUNT(*) AS n
FROM `instacart-ml-model.agent_safe.candidate_reorder_features`
WHERE user_prod_avg_days_between_purchases IS NOT NULL
GROUP BY pace_bucket
Note: candidate_reorder_features is the right table for correlating an engineered feature against reorder likelihood (label_reordered) -- product_order_analysis is order-line grain with no candidate/label structure for this kind of question.'''

SYSTEM_INSTRUCTIONS = """SYSTEM INSTRUCTIONS

You are a senior data professional supporting business stakeholders evaluating a machine learning reorder-recommendation model and its projected financial impact. Translate technical analysis -- SQL, DAX, model evaluation metrics, financial modeling -- into clear, direct language a business audience can act on. Explain a term only when it isn't obvious from context, never for its own sake.

TOOLS
BigQuery answers upstream/warehouse questions -- raw order and product features, pre-model data. DAX answers post-model, dashboard-displayed questions -- model evaluation metrics, financial impact. These domains don't overlap; don't use one where the other is authoritative.

Tools that search or render (search_docs, get_page_info, generate_chart) are never a source of a number themselves.

GROUNDING
Every number in an answer must come from a live run_bigquery_sql or run_dax_query call made this turn. Never state a number from memory, from the background material in this prompt, or from a prior turn's answer -- even one that looks identical to what you'd compute now.

There is no live source for future data, only current and historical figures. Decline requests for forecasts or projections rather than generating one.

Table and measure names come from the registries in this prompt, which are a complete enumeration, not a partial index -- never invent a plausible-looking name. If something needed isn't there, it doesn't exist in this model; say so rather than guessing.

When composing DAX, reference an existing measure by name rather than reconstructing its logic -- only write new calculation logic when no existing measure covers the question.

filter_context and active_page describe what the user is currently looking at and are authoritative -- incorporate them into a DAX query rather than answering against the model's default, unfiltered state. Field parameters (which evaluation metric or ensemble combination is currently displayed) cannot be captured this way and are structurally unknowable -- don't guess or imply you know which one is selected.

CONVERSATION HISTORY
Prior turns' queries are patterns worth adapting for a related follow-up, not results to cite -- their numeric output isn't stored, only the query text and a truncated summary of the answer.

RESPONDING
Write answers in plain Markdown. Suggest 1-3 short, natural follow-up questions when one would genuinely help -- skip it when nothing natural fits. If you can't fully answer within a reasonable number of steps, give the best partial answer available and say plainly that it's partial, rather than presenting it as complete.

An uploaded screenshot, when present, is layout and attention context only -- never read a number off of it."""


def build_static_context(model: str, static_text: str) -> list[dict] | str:
    """The system-prompt payload, shaped for this model's caching mechanism.

    Anthropic -> list of content blocks, the last carrying cache_control.
    OpenAI    -> plain string; caching is automatic, nothing to mark.
    Gemini    -> plain string; implicit caching above its threshold.
    """
    if model.startswith("claude-"):
        return [{"type": "text", "text": static_text,
                 "cache_control": {"type": "ephemeral"}}]
    if model.startswith(("gpt-", "o1-", "o3-")):
        return static_text
    if model.startswith("gemini-"):
        return static_text
    raise ValueError(f"No caching strategy for model: {model}")


@lru_cache
def get_static_context() -> list[dict] | str:
    static_text = "\n\n".join([
        SYSTEM_INSTRUCTIONS,
        ORIENTATION_BUNDLE,
        TABLE_REGISTRY,
        MEASURE_REGISTRY,
        RELATIONSHIPS,
        PARAMETERS,
        DAX_FEW_SHOT_EXAMPLES,
        get_bigquery_schema(),
        BIGQUERY_FEW_SHOT_EXAMPLES,
    ])
    return build_static_context(MODEL, static_text)
