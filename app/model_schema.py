"""Static-context registries built from the committed model schema artifact
(context/schema/model_schema.json, built by scripts/build_model_context.py).

Read once at import -- a plain file on disk, no credentials needed.
"""
import json
from typing import Literal

from app.config import CONTEXT_DIR

_schema = json.loads((CONTEXT_DIR / "schema" / "model_schema.json").read_text(encoding="utf-8"))


def _render_tables(tables: list[dict]) -> str:
    header = "TABLE REGISTRY -- every table in the semantic model, with its columns, types, and descriptions."
    blocks = []
    for t in tables:
        lines = [f"### {t['name']}"]
        if t.get("description"):
            lines.append(t["description"])
        for c in t["columns"]:
            desc = f": {c['description']}" if c.get("description") else ""
            lines.append(f"- {c['name']} ({c['type']}){desc}")
        blocks.append("\n".join(lines))
    return header + "\n\n" + "\n\n".join(blocks)


def _render_measures(measures: list[dict]) -> str:
    header = ("MEASURE REGISTRY -- every measure's name and description. "
               "Names and descriptions only -- fetch a measure's DAX body with "
               "get_measure_dax when you actually need to see its formula.")
    by_table: dict[str, list[dict]] = {}
    for m in measures:
        by_table.setdefault(m["table"], []).append(m)

    blocks = []
    for table, ms in by_table.items():
        lines = [f"### {table}"]
        for m in ms:
            desc = f": {m['description']}" if m.get("description") else ""
            lines.append(f"- {m['name']}{desc}")
        blocks.append("\n".join(lines))
    return header + "\n\n" + "\n\n".join(blocks)


def _render_relationships(relationships: list[dict]) -> str:
    header = "RELATIONSHIPS -- join paths between tables in the semantic model."
    lines = []
    for r in relationships:
        active = "active" if r["is_active"] else "inactive"
        lines.append(
            f"- {r['from_table']}[{r['from_column']}] -> {r['to_table']}[{r['to_column']}] "
            f"({r['from_cardinality']}:{r['to_cardinality']}, {r['cross_filtering_behavior']}, {active})"
        )
    return header + "\n" + "\n".join(lines)


def _render_parameters(parameters: list[dict]) -> str:
    header = ("PARAMETERS -- what-if and field parameters, with their filter column, "
               "current value or options, and whether they arrive in filter_context.")
    lines = []
    for p in parameters:
        if p["type"] == "numeric":
            r = p["range"]
            lines.append(
                f"- [numeric, in filter_context] {p['filter_column']} -> value measure: {p['value_measure']}, "
                f"default {p['default']}, range {r['min']}-{r['max']} step {r['step']}"
            )
        elif p["type"] == "field":
            options = ", ".join(f"{o['label']} = {o['field']}" for o in p["options"])
            lines.append(f"- [field, never in filter_context] {p['filter_column']} -> options: {options}")
    return header + "\n\n" + "\n".join(lines)


# -> static context
TABLE_REGISTRY = _render_tables(_schema["tables"])
MEASURE_REGISTRY = _render_measures(_schema["measures"])
RELATIONSHIPS = _render_relationships(_schema["relationships"])
PARAMETERS = _render_parameters(_schema["parameters"])

# -> get_measure_dax only, never static context
MEASURE_DAX = {m["name"]: m["dax"] for m in _schema["measures"]}
MEASURE_NAMES = Literal[tuple(MEASURE_DAX)]
