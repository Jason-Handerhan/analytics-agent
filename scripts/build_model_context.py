"""Builds context/schema/model_schema.json from live Power BI (executeQueries
+ Scanner API), replacing the earlier .pbip/TMDL-parsing design
(docs/data-pipeline.md Part 2).

Run manually after a semantic-model change, then commit the result:
    uv run python scripts/build_model_context.py
    git add context/schema/model_schema.json && git commit
"""
import json
import pathlib
import re
import sys
import time

import msal
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, for app.*

from app.config import GCP_PROJECT_ID, POWER_BI_DATASET_ID, POWER_BI_WORKSPACE_ID, get_secret

OUTPUT_PATH = pathlib.Path("context/schema/model_schema.json")

# Power BI's own auto-generated date tables -- excluded by name, not
# IsHidden, since legitimate tables (parameter tables) are also hidden.
AUTO_DATE_TABLE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")

# Power BI's own auto-generated row-index column -- appears on every table
# with this exact name, confirmed against the real model (25/25 tables).
ROW_INDEX_COLUMN = "RowNumber-2662979B-1795-4F74-8F37-6A1BA8059B61"

# Measures that render HTML/markdown for a visual, not an aggregation --
# must never reach the measure registry. Manual list is the mechanism of
# record; HTML_TAG_RE below is a build-time backstop that fails loudly if a
# new one slips in unlisted, rather than silently letting it through.
HTML_DISPLAY_MEASURES = {
    "Financial_Assumptions_HTML",
    "HTML_Model_Evaluation_Info",
    "HTML_Icon_Author_Credit",
    "HTML_Financial_Impact_Info",
}
HTML_TAG_RE = re.compile(r"<[a-z]+[ >]", re.I)

# App-integration plumbing measures, added to feed PowerBIIntegration.Data
# for the embedded chat app (docs/frontend.md) -- not real analytical
# measures. Slicer-selection ones duplicate what filter_context already
# sends every turn, so they add nothing; page-identifying ones are literal
# constants with no analytical meaning. Field-parameter selections
# (Selected_Evaluation_Metric, Selected_Ensemble_Weight) are NOT excluded --
# field parameters can't reach filter_context (a capacity/license error on
# that measure type in the visual's Data well), so these are the only way
# the agent can see which one is currently displayed.
APP_INTEGRATION_MEASURES = {
    "Selected_Dataset_Split",
    "Selected_Model",
    "Selected_Base_Model",
    "Active_Page_ModelEval",
    "Active_Page_FinancialImpact",
}

SELECTEDVALUE_RE = re.compile(r"SELECTEDVALUE\(\s*'([^']+)'\[([^\]]+)\](?:\s*,\s*(.+?))?\s*\)")

# Field parameters -- a second parameter kind, a 'Table'[Column] NAMEOF-style
# reference column instead of a numeric range. Manual list, same reasoning
# as HTML_DISPLAY_MEASURES: only a couple exist and they change rarely.
FIELD_PARAMETER_TABLES = {
    "Evaluation Metric Parameter": ("Parameter Fields", "Parameter Order"),
    "Ensemble Weight Parameter": ("Ensemble Weight Parameter Fields", "Ensemble Weight Parameter Order"),
}
FIELD_REF_RE = re.compile(r"'([^']+)'\[([^\]]+)\]")


def get_token() -> str:
    app = msal.ConfidentialClientApplication(
        get_secret("power-bi-sp-client-id", GCP_PROJECT_ID),
        authority=f"https://login.microsoftonline.com/{get_secret('azure-tenant-id', GCP_PROJECT_ID)}",
        client_credential=get_secret("power-bi-sp-client-secret", GCP_PROJECT_ID),
    )
    result = app.acquire_token_for_client(
        scopes=["https://analysis.windows.net/powerbi/api/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result}")
    return result["access_token"]


def run_dax(headers: dict, query: str) -> list[dict]:
    resp = requests.post(
        f"https://api.powerbi.com/v1.0/myorg/datasets/{POWER_BI_DATASET_ID}/executeQueries",
        headers=headers,
        json={"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}},
    )
    resp.raise_for_status()
    return resp.json()["results"][0]["tables"][0]["rows"]


def scan_workspace(headers: dict) -> dict:
    submit = requests.post(
        "https://api.powerbi.com/v1.0/myorg/admin/workspaces/getInfo",
        headers=headers,
        params={"datasetExpressions": "True", "datasetSchema": "True", "lineage": "True"},
        json={"workspaces": [POWER_BI_WORKSPACE_ID]},
    )
    submit.raise_for_status()
    scan_id = submit.json()["id"]

    for _ in range(20):
        status = requests.get(
            f"https://api.powerbi.com/v1.0/myorg/admin/workspaces/scanStatus/{scan_id}",
            headers=headers,
        ).json()["status"]
        if status == "Succeeded":
            break
        time.sleep(2)
    else:
        raise TimeoutError("Scan did not finish in time")

    scan_result = requests.get(
        f"https://api.powerbi.com/v1.0/myorg/admin/workspaces/scanResult/{scan_id}",
        headers=headers,
    ).json()
    return next(
        ds for ws in scan_result["workspaces"] for ds in ws.get("datasets", [])
        if ds["id"] == POWER_BI_DATASET_ID
    )


def build_tables(headers: dict) -> tuple[list[dict], set[str]]:
    raw_tables = run_dax(headers,
        'EVALUATE SELECTCOLUMNS(INFO.VIEW.TABLES(), '
        '"TableName", [Name], "Description", [Description], "IsHidden", [IsHidden])'
    )
    raw_columns = run_dax(headers,
        'EVALUATE SELECTCOLUMNS(INFO.VIEW.COLUMNS(), '
        '"TableName", [Table], "ColumnName", [Name], "DataType", [DataType], '
        '"Description", [Description], "IsHidden", [IsHidden])'
    )

    excluded = {
        t["[TableName]"] for t in raw_tables
        if t["[TableName]"].startswith(AUTO_DATE_TABLE_PREFIXES)
    }

    columns_by_table: dict[str, list[dict]] = {}
    for c in raw_columns:
        if c["[TableName]"] in excluded or c["[ColumnName]"] == ROW_INDEX_COLUMN:
            continue
        columns_by_table.setdefault(c["[TableName]"], []).append({
            "name": c["[ColumnName]"],
            "type": c["[DataType]"],
            "description": c["[Description]"],
        })

    tables = [
        {
            "name": t["[TableName]"],
            "description": t["[Description]"],
            "columns": columns_by_table.get(t["[TableName]"], []),
        }
        for t in raw_tables if t["[TableName]"] not in excluded
    ]
    return tables, excluded


def build_relationships(headers: dict, excluded_tables: set[str]) -> list[dict]:
    raw = run_dax(headers,
        'EVALUATE SELECTCOLUMNS(INFO.VIEW.RELATIONSHIPS(), '
        '"FromTable", [FromTable], "FromColumn", [FromColumn], '
        '"ToTable", [ToTable], "ToColumn", [ToColumn], '
        '"FromCardinality", [FromCardinality], "ToCardinality", [ToCardinality], '
        '"CrossFilteringBehavior", [CrossFilteringBehavior], "IsActive", [IsActive])'
    )
    return [
        {
            "from_table": r["[FromTable]"], "from_column": r["[FromColumn]"],
            "to_table": r["[ToTable]"], "to_column": r["[ToColumn]"],
            "from_cardinality": r["[FromCardinality]"], "to_cardinality": r["[ToCardinality]"],
            "cross_filtering_behavior": r["[CrossFilteringBehavior]"], "is_active": r["[IsActive]"],
        }
        for r in raw
        if r["[FromTable]"] not in excluded_tables and r["[ToTable]"] not in excluded_tables
    ]


def find_selectedvalue_refs(expr: str) -> list[tuple[str, str, str | None]]:
    if not expr:
        return []
    return [(t, c, d.strip() if d else None) for t, c, d in SELECTEDVALUE_RE.findall(expr)]


def build_field_parameters(headers: dict) -> list[dict]:
    parameters = []
    for table, (fields_col, order_col) in FIELD_PARAMETER_TABLES.items():
        rows = sorted(run_dax(headers, f"EVALUATE '{table}'"), key=lambda r: r[f"{table}[{order_col}]"])
        options = []
        for r in rows:
            m = FIELD_REF_RE.search(r[f"{table}[{fields_col}]"])
            options.append({"label": r[f"{table}[{table}]"], "field": f"{m.group(1)}[{m.group(2)}]"})
        parameters.append({"type": "field", "filter_column": f"{table}[{table}]", "options": options})
    return parameters


def compute_range(headers: dict, table: str, column: str) -> dict:
    """min/max/step read directly from the parameter table's real values --
    neither API exposes the GENERATESERIES(...) formula that produced them.
    """
    raw = run_dax(headers, f"EVALUATE VALUES('{table}'[{column}])")
    values = sorted(round(list(row.values())[0], 4) for row in raw)
    step = round(values[1] - values[0], 4) if len(values) > 1 else None
    return {"min": values[0], "max": values[-1], "step": step}


def build_measures_and_parameters(headers: dict) -> tuple[list[dict], list[dict]]:
    dataset = scan_workspace(headers)
    measures_full = [
        {
            "table": tbl["name"], "name": m["name"],
            "description": m.get("description"), "expression": m.get("expression"),
        }
        for tbl in dataset["tables"] for m in tbl.get("measures", [])
    ]

    # Parameter detection -- strict form only: the measure's ENTIRE
    # expression is one bare SELECTEDVALUE(...) call.
    parameters = []
    for m in measures_full:
        expr = (m["expression"] or "").strip()
        refs = find_selectedvalue_refs(expr)
        if refs and expr.startswith("SELECTEDVALUE("):
            table, column, default = refs[0]
            parameters.append({
                "type": "numeric",
                "filter_column": f"{table}[{column}]",
                "value_measure": m["name"],
                "default": float(default) if default is not None else None,
                "range": compute_range(headers, table, column),
            })
    parameters.extend(build_field_parameters(headers))

    unlisted_html = [
        m["name"] for m in measures_full
        if m["name"] not in HTML_DISPLAY_MEASURES and HTML_TAG_RE.search(m["expression"] or "")
    ]
    if unlisted_html:
        raise SystemExit(
            f"Found HTML-like measure(s) not in HTML_DISPLAY_MEASURES: {unlisted_html}. "
            "Add them to the exclusion list if confirmed HTML-display measures."
        )

    # Numeric parameters' value measures are covered fully by PARAMETERS
    # (name, default, range) -- listing them again here would just repeat
    # that, under their own single-measure table header.
    value_measure_names = {p["value_measure"] for p in parameters if p["type"] == "numeric"}

    measures = [
        {"table": m["table"], "name": m["name"], "description": m["description"], "dax": m["expression"]}
        for m in measures_full
        if m["name"] not in HTML_DISPLAY_MEASURES
        and m["name"] not in APP_INTEGRATION_MEASURES
        and m["name"] not in value_measure_names
    ]
    return measures, parameters


def main() -> None:
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    tables, excluded_tables = build_tables(headers)
    relationships = build_relationships(headers, excluded_tables)
    measures, parameters = build_measures_and_parameters(headers)

    schema = {
        "tables": tables,
        "measures": measures,
        "relationships": relationships,
        "parameters": parameters,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Wrote {len(tables)} tables, {len(measures)} measures, "
        f"{len(relationships)} relationships, {len(parameters)} parameters "
        f"to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
