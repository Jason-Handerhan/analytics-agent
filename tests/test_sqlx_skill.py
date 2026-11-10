import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "coding_agent_app" / "skills" / "sqlx" / "compose_query.py"
spec = importlib.util.spec_from_file_location("compose_query", SCRIPT)
compose_query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose_query)


def table(name: str, reads: list[str], sql: str, schema: str = "gold") -> dict:
    target = lambda schema, name: {"database": "p", "schema": schema, "name": name}
    return {"fileName": f"definitions\\{schema}\\{name}.sqlx", "target": target(schema, name), "query": sql,
            "dependencyTargets": [target(*other.split(".")) for other in reads], "actionDescriptor": {"columns": [{"path": ["a"]}]}}


# raw.orders -> silver_a -> gold_b -> gold_c, and gold_other reads only raw.orders
COMPILED = {"tables": [
    table("gold_c", ["gold.gold_b"], "SELECT * FROM `p.gold.gold_b`"),
    table("gold_b", ["silver.silver_a"], "SELECT * FROM `p.silver.silver_a`"),
    table("silver_a", ["raw.orders"], "SELECT * FROM `p.raw.orders`", schema="silver"),
    table("gold_other", ["raw.orders"], "SELECT * FROM `p.raw.orders`"),
]}


@pytest.fixture
def compile_json(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_query, "OUTPUT_DIR", tmp_path / "composed")
    path = tmp_path / "compile.json"
    path.write_text(json.dumps(COMPILED), encoding="utf-8")
    return str(path)


def test_a_changed_table_and_everything_downstream_is_composed_down_to_the_raw_table(compile_json, tmp_path, capsys):
    compose_query.main(compile_json, ["definitions/silver/silver_a.sqlx"])  # forward slashes, though the file names have backslashes
    composed = {path.stem: path.read_text(encoding="utf-8") for path in (tmp_path / "composed").iterdir()}
    assert sorted(composed) == ["gold_b", "gold_c", "silver_a"]  # gold_other does not read the changed table
    assert "`p.silver.silver_a`" not in composed["gold_c"] and "`p.gold.gold_b`" not in composed["gold_c"]
    assert composed["gold_c"].count("`p.raw.orders`") == 1  # the only table left is the raw one, once
    assert "gold_c.sql" in capsys.readouterr().out


def test_an_unknown_file_names_the_files_that_exist(compile_json):
    with pytest.raises(SystemExit) as error:
        compose_query.main(compile_json, ["definitions/gold/nope.sqlx"])
    assert "definitions/gold/gold_b.sqlx" in str(error.value)
