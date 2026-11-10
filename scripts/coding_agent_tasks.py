"""Test tasks for the coding agent, each a CodingAgentPlan to import by name. A task's pieces are module-level variables named
after it; the aisle pacing tasks share the pieces they have in common."""
from app.coding_task_models import CodingAgentPlan, SuggestedCode


# ---- aisle_pacing
aisle_pacing_goal = """Add a new ML feature, user_aisle_pacing_ratio, to the Dataform pipeline: (days since the user's last purchase in the aisle, as of the label order) divided by (the user's average days between purchases in that aisle). It is the aisle-level counterpart of user_product_nearing_reorder_ratio.

Exploratory analysis found that reorder rate rises and then declines as this ratio grows, and that the feature is not redundant with the model's existing top features. That analysis used a 1% sample of users."""

aisle_pacing_requirements = [
    "Add user_aisle_pacing_ratio to the Dataform pipeline in the three layers the repo uses: a silver building block, the skeleton forward-fill, and the final feature.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
]

aisle_pacing_constraints = [
    "Edit only files under /definitions. Follow the three-layer pattern already in the repo (silver building block, skeleton forward-fill, final feature), and mirror how user_product_nearing_reorder_ratio is built.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Do not modify any other files.",
]

aisle_pacing_decisions = [
    "The average gap between aisle purchases uses the anchor order's day, the way user_prod_avg_days_between_purchases does for products: (anchor days_since_first_order - first aisle purchase day) / (cumulative aisle purchase orders - 1), with 999 when it is undefined. Do not precompute the average in the silver layer.",
    "Missing values use 999, as the product version does.",
]

aisle_pacing_suggested_code = [
    SuggestedCode(
        purpose="draft: adds first/last purchase day, purchase-order count and an average gap",
        file="prelim_user_aisle_features.sqlx",
        code="""config {
  type: "table",
  schema: "building_blocks",
  name: "prelim_user_aisle_features",
  bigquery: { clusterBy: ["user_id"] },
  description: "User aisle-level categorical shares and purchase pacing."
}

WITH user_category_temp AS (
  SELECT
    user_id,
    order_id,
    order_number,
    days_since_first_order,
    aisle_id AS category_id,
    COUNT(*) AS user_order_category_products
  FROM ${ref("base_analytical_table")}
  GROUP BY user_id, order_id, order_number, days_since_first_order, category_id
),

user_category_temp2 AS (
  SELECT
    user_id,
    order_id,
    order_number,
    days_since_first_order,
    category_id,
    user_order_category_products,
    SUM(user_order_category_products) OVER(
      PARTITION BY user_id, category_id
      ORDER BY order_number
    ) AS user_cumulative_category_products,
    SUM(user_order_category_products) OVER(
      PARTITION BY user_id
      ORDER BY order_number
      RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS user_cumulative_products,
    MIN(days_since_first_order) OVER(
      PARTITION BY user_id, category_id ORDER BY order_number
    ) AS user_aisle_first_purchase_day,
    MAX(days_since_first_order) OVER(
      PARTITION BY user_id, category_id ORDER BY order_number
    ) AS user_aisle_last_purchase_day,
    COUNT(*) OVER(
      PARTITION BY user_id, category_id ORDER BY order_number
    ) AS user_aisle_cumulative_purchase_orders
  FROM user_category_temp
)

SELECT
  user_id,
  order_id,
  order_number,
  category_id AS aisle_id,
  user_cumulative_category_products AS user_cumulative_aisle_products,
  user_cumulative_products,
  SAFE_DIVIDE(user_cumulative_category_products, user_cumulative_products) AS user_aisle_share,
  user_aisle_last_purchase_day,
  COALESCE(
    SAFE_DIVIDE(
      user_aisle_last_purchase_day - user_aisle_first_purchase_day,
      user_aisle_cumulative_purchase_orders - 1
    ),
    999
  ) AS user_aisle_avg_days_between_purchases
FROM user_category_temp2""",
    ),
    SuggestedCode(
        purpose="draft: forward-fill of the new aisle columns, after the existing aisle forward-fill",
        file="skeleton_features.sqlx",
        code="""LAST_VALUE(a.user_aisle_last_purchase_day IGNORE NULLS) OVER(PARTITION BY cs.user_id, cs.aisle_id ORDER BY cs.order_number) AS user_aisle_last_purchase_day,
  LAST_VALUE(a.user_aisle_avg_days_between_purchases IGNORE NULLS) OVER(PARTITION BY cs.user_id, cs.aisle_id ORDER BY cs.order_number) AS user_aisle_avg_days_between_purchases,""",
    ),
    SuggestedCode(
        purpose="draft: the ratio",
        file="final_ml_features_table.sqlx",
        code="""COALESCE(
    SAFE_DIVIDE(
      temp_ml.label_days_since_first_order - hist.user_aisle_last_purchase_day,
      hist.user_aisle_avg_days_between_purchases
    ),
    999
  ) AS user_aisle_pacing_ratio""",
    ),
]

aisle_pacing = CodingAgentPlan(
    name="Aisle pacing ratio",
    goal=aisle_pacing_goal,
    requirements=aisle_pacing_requirements,
    constraints=aisle_pacing_constraints,
    decisions=aisle_pacing_decisions,
    suggested_code=aisle_pacing_suggested_code,
)

# ---- aisle_pacing_lgbm_ranker
aisle_pacing_lgbm_ranker_requirements = [
    "Add user_aisle_pacing_ratio to the Dataform pipeline in the three layers the repo uses: a silver building block, the skeleton forward-fill, and the final feature.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Use the new feature in the LGBM ranker's training only: the LGBM classifier, the XGBoost ranker and the XGBoost classifier must keep exactly the features they train on now.",
]

aisle_pacing_lgbm_ranker_constraints = [
    "In /definitions, follow the three-layer pattern already in the repo (silver building block, skeleton forward-fill, final feature), and mirror how user_product_nearing_reorder_ratio is built.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Also use the new feature in the LGBM ranker's training, and only there: the LGBM classifier, the XGBoost ranker and the XGBoost classifier must keep exactly the features they train on now. The feature list lives in a function that all four training scripts share, so decide how to keep the other three unchanged.",
    "Add a small pytest under /tests that passes with your change and would fail if the ranker lacked the feature or another model gained it. Run it, and run ruff on the Python files you changed.",
    "Do not modify any other files, and do not change device settings or training hyperparameters.",
]

aisle_pacing_lgbm_ranker = CodingAgentPlan(
    name="Aisle pacing ratio for the LGBM ranker",
    goal=aisle_pacing_goal,
    requirements=aisle_pacing_lgbm_ranker_requirements,
    constraints=aisle_pacing_lgbm_ranker_constraints,
    decisions=aisle_pacing_decisions,
    suggested_code=aisle_pacing_suggested_code,
)

# ---- aisle_pacing_lgbm_ranker_w_test_notebook
aisle_pacing_lgbm_ranker_w_test_notebook_requirements = [
    "Add user_aisle_pacing_ratio to the Dataform pipeline in the three layers the repo uses: a silver building block, the skeleton forward-fill, and the final feature.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Use the new feature in the LGBM ranker's training only: the LGBM classifier, the XGBoost ranker and the XGBoost classifier must keep exactly the features they train on now.",
    "After the feature is built, create one new notebook that compares the LGBM ranker without and with user_aisle_pacing_ratio. Model it on the scratch baseline model in Notebooks/EDA/feature_eda_screening.ipynb (read it first): its train and validation split, ranker, per-order ndcg@5 and feature importance cells.",
    "Train two models on the same sample and split: one on the features the ranker used before your change, one on the features it uses after. Get both feature lists from the repo's feature selection code, not by copying lists.",
    "Use a 5% sample of users. Use the screening baseline's model settings and early stopping for both models; no hyperparameter tuning.",
    "Report each model's ndcg@5 on the validation set, calculated per order the way the screening notebook does, and the difference between them.",
    "Add a feature importance table (gain, with cumulative percentage) and a bar plot for the model that includes the new feature, and show where user_aisle_pacing_ratio ranks and its share of the total importance.",
]

aisle_pacing_lgbm_ranker_w_test_notebook_constraints = [
    "In /definitions, follow the three-layer pattern already in the repo (silver building block, skeleton forward-fill, final feature), and mirror how user_product_nearing_reorder_ratio is built.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Also use the new feature in the LGBM ranker's training, and only there: the LGBM classifier, the XGBoost ranker and the XGBoost classifier must keep exactly the features they train on now. The feature list lives in a function that all four training scripts share, so decide how to keep the other three unchanged.",
    "Add a small pytest under /tests that passes with your change and would fail if the ranker lacked the feature or another model gained it. Run it, and run ruff on the Python files you changed.",
    "Do not modify any other files except the new notebook described below, and do not change device settings or training hyperparameters in the repo's scripts.",
]

aisle_pacing_lgbm_ranker_w_test_notebook = CodingAgentPlan(
    name="Aisle pacing ratio for the LGBM ranker, with a comparison notebook",
    goal=aisle_pacing_goal,
    requirements=aisle_pacing_lgbm_ranker_w_test_notebook_requirements,
    constraints=aisle_pacing_lgbm_ranker_w_test_notebook_constraints,
    decisions=aisle_pacing_decisions,
    suggested_code=aisle_pacing_suggested_code,
)

# ---- aisle_pacing_all_models_edit_notebook
aisle_pacing_all_models_edit_notebook_requirements = [
    "Add user_aisle_pacing_ratio to the Dataform pipeline in the three layers the repo uses: a silver building block, the skeleton forward-fill, and the final feature.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Use the new feature in all four base models' training (the LGBM and XGBoost rankers and classifiers).",
    "Carry the feature through everything downstream: find the places that consume the feature list or name features, and change each one the new feature requires, checking Notebooks/evaluation/model_evaluation.ipynb in particular.",
]

aisle_pacing_all_models_edit_notebook_constraints = [
    "In /definitions, follow the three-layer pattern already in the repo (silver building block, skeleton forward-fill, final feature), and mirror how user_product_nearing_reorder_ratio is built.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Use the new feature in all four base models' training (the LGBM and XGBoost rankers and classifiers). They share one feature list function, so decide where the feature belongs.",
    "Carry the feature through everything downstream: find the places that consume the feature list or name features, and change each one the new feature requires. Check Notebooks/evaluation/model_evaluation.ipynb in particular. Change a notebook only if it actually needs it, and if it does not need a change, say so and why.",
    "Edit an existing notebook in place, changing only the cells that need it, and do not run it.",
    "Add a small pytest under /tests that passes with your change and fails if the shared feature list loses the new feature, or if any of the four training scripts stops getting its features from it. Run it, and run ruff on the Python files you changed.",
    "Do not modify any other files, and do not change device settings or training hyperparameters.",
]

aisle_pacing_all_models_edit_notebook = CodingAgentPlan(
    name="Aisle pacing ratio for all four models",
    goal=aisle_pacing_goal,
    requirements=aisle_pacing_all_models_edit_notebook_requirements,
    constraints=aisle_pacing_all_models_edit_notebook_constraints,
    decisions=aisle_pacing_decisions,
    suggested_code=aisle_pacing_suggested_code,
)

# ---- revision_for_aisle_pacing
revision_for_aisle_pacing_goal = (
    "The feature user_aisle_pacing_ratio is already built in the Dataform pipeline on this branch, in definitions/. Finish making it a "
    "feature of all four base models: use it in their training, and carry it through everything downstream."
)

revision_for_aisle_pacing_requirements = aisle_pacing_all_models_edit_notebook_requirements[2:]

revision_for_aisle_pacing_constraints = [
    *aisle_pacing_all_models_edit_notebook_constraints[2:],
    "Do not change anything under /definitions: the feature is already built there.",
]

revision_for_aisle_pacing = CodingAgentPlan(
    name="Aisle pacing ratio in all four models (revision)",
    goal=revision_for_aisle_pacing_goal,
    requirements=revision_for_aisle_pacing_requirements,
    constraints=revision_for_aisle_pacing_constraints,
)

# ---- column_descriptions
column_descriptions_goal = """Document every column of every table in the Dataform project, so that anyone reading a table's schema in BigQuery (a person, or a program that picks columns for new features) learns what each column means. Add a description for each column to the table's config block, using Dataform's columns property. Dataform writes these descriptions to BigQuery when the pipeline runs.

Every definition has a table-level description today, and none has column descriptions. The 13 tables Dataform builds are in definitions/staging, definitions/silver and definitions/gold. The six files named definitions/sources_*.sqlx declare raw tables that Dataform does not build; document their columns in the repo too."""

column_descriptions_requirements = [
    "Add a description for every column of every table in the Dataform project to the table's config block, using Dataform's columns property: all 19 definitions, the 13 tables and the 6 declarations.",
    "Define the descriptions of columns that mean the same thing in several tables once, in includes/column_descriptions.js, and reference them from the config blocks.",
]

column_descriptions_constraints = [
    "Cover every column each table outputs, in all 19 definitions (13 tables and 6 declarations). Work out a table's columns by reading its SELECT, and the SELECTs it builds on where it uses *.",
    "Change only the config blocks of the .sqlx files, and add the one new include file. Do not change any SQL below a config block. Leave existing table descriptions as they are unless one is wrong; report any you think is wrong instead of rewriting it.",
    "Run dataform compile; it must pass.",
    "Do not modify any other files.",
    "One or two short sentences, under 300 characters.",
    "Say what the value is and its unit (days, count, ratio, share).",
    "Say how missing or undefined cases are encoded when the SQL does it (for example 999 when undefined, 0 for a user's first order).",
    "Describe what the SQL does, in words a person on the team would use. If you cannot tell the business meaning from the SQL and the names, say what you can and list the column in your report.",
]

column_descriptions_decisions = [
    "Many columns appear in several tables with the same meaning (user_id, order_id, product_id, aisle_id, department_id, order_number, days_since_first_order and others). Define each of those descriptions once in a JavaScript include, includes/column_descriptions.js, and reference it from the config blocks, for example user_id: column_descriptions.user_id. Put a description in the include only if the column name means the same thing wherever it appears; a column whose meaning differs between tables gets its own description in that table.",
]

column_descriptions_suggested_code = [
    SuggestedCode(
        purpose="A config block takes a columns property",
        file=None,
        code="""config {
  type: "table",
  schema: "instacart",
  name: "cleansed_orders",
  description: "...",
  columns: {
    order_id: "...",
    user_id: "..."
  }
}""",
    ),
]

column_descriptions = CodingAgentPlan(
    name="Column descriptions for every table",
    goal=column_descriptions_goal,
    requirements=column_descriptions_requirements,
    constraints=column_descriptions_constraints,
    decisions=column_descriptions_decisions,
    suggested_code=column_descriptions_suggested_code,
)

# ---- cumulative_weekday_counts
cumulative_weekday_counts_goal = """Fix the day-of-week and time-of-day count features so that, for each user and product, the wide tables hold the running count of the user's purchases of that product on EACH weekday (and in EACH time of day), up to and including the order. Then update every description that describes the old behavior.

The features label_dow_user_product_same_dow_count and label_tod_user_product_same_tod_count, and the two affinity features built from them (user_product_day_of_week_affinity and user_product_time_of_day_affinity), are meant to be cumulative: how many times the user bought the candidate product on the same weekday (or time of day) as the label order, up to the anchor order. They are not, because of how the wide tables are built:
- prelim_user_product_features computes user_product_same_dow_count as a running count per user, product and the weekday of THAT row.
- prelim_user_order_dow_wide turns it into columns user_product_same_dow_count_0 to _6 with SUM(CASE WHEN order_dow = k THEN count ELSE 0 END), grouped to one row per purchase. So at each purchase only the column for that purchase's own weekday is filled, and the other six are 0.
- skeleton_features forward-fills each column with LAST_VALUE(... IGNORE NULLS). A 0 is not NULL, so all seven columns come from the product's most recent purchase row.
- final_ml_features_table picks the column for the label order's weekday.
Result: the feature is non-zero only when the product's last purchase fell on the label's weekday. Otherwise it is 0, even if the user bought the product on that weekday many times. The affinity is too low for the same reason. prelim_user_order_tod_wide has the same structure for time of day.

Worked example. One user bought one product in orders 1 to 5 on weekdays 1, 2, 1, 3, 1. The correct running counts per weekday after each order are:
- order 1: weekday 1 = 1
- order 2: weekday 1 = 1, weekday 2 = 1
- order 3: weekday 1 = 2, weekday 2 = 1
- order 4: weekday 1 = 2, weekday 2 = 1, weekday 3 = 1
- order 5: weekday 1 = 3, weekday 2 = 1, weekday 3 = 1
With anchor order 5 and a label order on weekday 2, the feature should be 1. Today it is 0, because the row for order 5 holds 3 under weekday 1 and 0 under every other weekday."""

cumulative_weekday_counts_requirements = [
    "Change prelim_user_order_dow_wide.sqlx and prelim_user_order_tod_wide.sqlx so that each row holds the running count of the user's purchases of the product on each weekday (and in each time of day), up to and including the order.",
    "Update the descriptions that describe the old behavior, in the two wide tables, skeleton_features and final_ml_features_table.",
]

cumulative_weekday_counts_constraints = [
    "Change the SQL of prelim_user_order_dow_wide.sqlx and prelim_user_order_tod_wide.sqlx only. Keep their output column names and types, and keep one row per user, product and order: skeleton_features joins them on user, order and product, so a duplicated row would multiply rows. Confirm from the SQL of prelim_user_product_features that it has one row per user, product and order (it groups by those keys plus order attributes), and keep a safeguard in your version if you are not sure.",
    "Do not change the SQL of any other table. skeleton_features and final_ml_features_table already carry forward and pick the right columns; once the wide tables hold running counts across all weekdays and times of day they give the intended result. Leave the user_product_same_dow_count and user_product_same_tod_count columns in prelim_user_product_features as they are.",
    "Update the descriptions (the table description and the columns blocks) that now describe the old behavior, in the two wide tables, skeleton_features and final_ml_features_table, so they say what the columns hold after the fix. Change only descriptions that became inaccurate. Weekday 0 is Sunday, and that is already defined in includes/column_descriptions.js.",
    "Nothing can run the SQL here, so check your logic on the worked example above by hand (a few lines of pandas in /tmp are fine), then run dataform compile; it must pass.",
    "Do not modify any other files.",
]

cumulative_weekday_counts_suggested_code = [
    SuggestedCode(
        purpose="draft: running counts across weekdays; the same shape for time of day with the four time_of_day values",
        file="prelim_user_order_dow_wide.sqlx",
        code="""SELECT
  user_id, product_id, order_id, order_number,
  COUNTIF(order_dow = 0) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_0,
  COUNTIF(order_dow = 1) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_1,
  COUNTIF(order_dow = 2) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_2,
  COUNTIF(order_dow = 3) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_3,
  COUNTIF(order_dow = 4) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_4,
  COUNTIF(order_dow = 5) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_5,
  COUNTIF(order_dow = 6) OVER(PARTITION BY user_id, product_id ORDER BY order_number) AS user_product_same_dow_count_6
FROM (
  SELECT user_id, product_id, order_id, order_number, order_dow
  FROM ${ref("prelim_user_product_features")}
  GROUP BY user_id, product_id, order_id, order_number, order_dow
)""",
    ),
]

cumulative_weekday_counts = CodingAgentPlan(
    name="Cumulative weekday and time-of-day counts",
    goal=cumulative_weekday_counts_goal,
    requirements=cumulative_weekday_counts_requirements,
    constraints=cumulative_weekday_counts_constraints,
    suggested_code=cumulative_weekday_counts_suggested_code,
)

# ---- rapidfuzz_dependency
rapidfuzz_dependency_goal = "Add a function find_near_duplicate_names(names, threshold=90) to src/utilities/utility_functions.py. It takes a list of product names and returns the pairs whose similarity score is at least threshold (a score from 0 to 100), as a list of (name_a, name_b, score) tuples, each pair once, highest score first. Use the rapidfuzz package to compute the score."

rapidfuzz_dependency_requirements = [
    "Add find_near_duplicate_names(names, threshold=90) to src/utilities/utility_functions.py, using the rapidfuzz package, returning (name_a, name_b, score) tuples for the pairs scoring at least threshold, each pair once, highest score first.",
]

rapidfuzz_dependency_constraints = [
    "Add a small pytest under /tests that passes with your change. Run it.",
    "Do not modify any other files, except requirements.txt if you need to declare a dependency.",
    "Do not change device settings or training hyperparameters.",
]

rapidfuzz_dependency = CodingAgentPlan(
    name="Near-duplicate product names",
    goal=rapidfuzz_dependency_goal,
    requirements=rapidfuzz_dependency_requirements,
    constraints=rapidfuzz_dependency_constraints,
)

# ---- aisle_recency_ambiguous
aisle_recency_ambiguous_goal = """Add a new ML feature, user_aisle_recency_score, to the Dataform pipeline: a score of how much of the user's purchasing in an aisle happened recently, so aisles bought from recently score higher than aisles bought from long ago.

Exploratory analysis suggests aisles a user bought from recently are more likely to be reordered, and the existing aisle features treat all past purchases the same."""

aisle_recency_ambiguous_requirements = [
    "Add user_aisle_recency_score to the Dataform pipeline in the three layers the repo uses: a silver building block, the skeleton forward-fill, and the final feature.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
]

aisle_recency_ambiguous_constraints = [
    "Edit only files under /definitions. Follow the three-layer pattern already in the repo (silver building block, skeleton forward-fill, final feature).",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
    "Do not modify any other files.",
]

aisle_recency_ambiguous = CodingAgentPlan(
    name="Aisle recency score",
    goal=aisle_recency_ambiguous_goal,
    requirements=aisle_recency_ambiguous_requirements,
    constraints=aisle_recency_ambiguous_constraints,
)

# ---- promo_share_ambiguous
promo_share_ambiguous_goal = """Add a new ML feature, user_product_promo_share, to the Dataform pipeline: the share of the user's past purchases of a product that were made while the product was on promotion.

Exploratory analysis suggests users who mostly buy a product on promotion are less likely to reorder it at full price."""

promo_share_ambiguous_requirements = [
    "Add user_product_promo_share to the Dataform pipeline in the three layers the repo uses: a silver building block, the skeleton forward-fill, and the final feature.",
    "Add the matching null check for the new feature to rowConditions in final_ml_features_table.sqlx.",
]

promo_share_ambiguous = CodingAgentPlan(
    name="Promo share of purchases",
    goal=promo_share_ambiguous_goal,
    requirements=promo_share_ambiguous_requirements,
    constraints=aisle_recency_ambiguous_constraints,
)
