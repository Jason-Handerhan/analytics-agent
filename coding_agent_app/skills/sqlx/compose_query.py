"""Write a self-contained query for each table built from the changed .sqlx files, and for every table that reads one, to
/tmp/composed/<table>.sql: python compose_query.py <compile.json> <changed .sqlx file> [<changed .sqlx file> ...]"""
import json
import sys
from pathlib import Path

OUTPUT_DIR = Path("/tmp/composed")


def full_table_name(target: dict) -> str:
    """A table's name as compiled SQL writes it: `project.dataset.table`, with the backticks.
    target is a dict with 'database', 'schema' and 'name', as in compiled_table['target']."""
    return f"`{target['database']}.{target['schema']}.{target['name']}`"


def source_file_path(compiled_table: dict) -> str:
    """The table's .sqlx file path, always with forward slashes (the compile output uses backslashes on Windows)."""
    return compiled_table["fileName"].replace("\\", "/")


def replace_table_references_with_sql(sql: str, sql_by_table_name: dict[str, str]) -> str:
    """Goes down, toward the raw tables: replace each reference in the SQL to a table this repo builds with that table's
    own SQL, as a subquery. The inserted SQL may reference built tables too, so each one is expanded the same way first."""
    for table_name, table_sql in sql_by_table_name.items():
        if table_name in sql:
            sql_with_references_replaced = replace_table_references_with_sql(table_sql, sql_by_table_name)
            sql = sql.replace(table_name, f"(\n{sql_with_references_replaced}\n)")
    return sql


def find_affected_tables(compiled_tables: list[dict], changed_table_names: set[str]) -> set[str]:
    """Goes up, toward the final table: the changed tables plus every table that reads one of them, directly or through
    other tables. A table's dependencyTargets are the tables its SQL reads; nothing stores who reads a table, so this
    works it out by scanning every table."""
    affected_table_names = set(changed_table_names)
    found_new_affected_table = True
    # a table added in one pass can make a table earlier in the list qualify, so repeat until a pass adds nothing
    while found_new_affected_table:
        found_new_affected_table = False
        for compiled_table in compiled_tables:
            table_name = full_table_name(compiled_table["target"])
            referenced_table_names = [full_table_name(referenced) for referenced in compiled_table.get("dependencyTargets", [])]
            reads_from_affected_table = any(name in affected_table_names for name in referenced_table_names)
            if reads_from_affected_table and table_name not in affected_table_names:
                affected_table_names.add(table_name)
                found_new_affected_table = True
    return affected_table_names


def main(compile_json_path: str, changed_files: list[str]) -> None:
    # compiled_tables: one dict per table from `dataform compile --json` (file name, target, SQL, dependencies, documented columns)
    try:
        compiled_tables = json.loads(Path(compile_json_path).read_text(encoding="utf-8"))["tables"]
    except FileNotFoundError:
        sys.exit(f"{compile_json_path} does not exist. Create it with: cd <repo> && dataform compile --json > {compile_json_path}")

    # find the table that each changed file defines
    table_by_source_file = {source_file_path(compiled_table): compiled_table for compiled_table in compiled_tables}
    unknown_files = [file for file in changed_files if file not in table_by_source_file]
    if unknown_files:
        sys.exit(f"Not a table's source file in the compile output: {unknown_files}. Files that are: {sorted(table_by_source_file)}")
    changed_table_names = {full_table_name(table_by_source_file[file]["target"]) for file in changed_files}

    # write one self-contained query for each affected table
    sql_by_table_name = {full_table_name(compiled_table["target"]): compiled_table["query"] for compiled_table in compiled_tables}
    affected_table_names = find_affected_tables(compiled_tables, changed_table_names)
    OUTPUT_DIR.mkdir(exist_ok=True)
    for compiled_table in compiled_tables:
        if full_table_name(compiled_table["target"]) not in affected_table_names:
            continue
        output_path = OUTPUT_DIR / f"{compiled_table['target']['name']}.sql"
        output_path.write_text(replace_table_references_with_sql(compiled_table["query"], sql_by_table_name), encoding="utf-8")
        documented_column_count = len(compiled_table.get("actionDescriptor", {}).get("columns", []))
        print(f"{source_file_path(compiled_table)} -> {output_path} ({output_path.stat().st_size:,} characters, {documented_column_count} documented columns)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("Usage: python compose_query.py <compile.json> <changed .sqlx file> [<changed .sqlx file> ...]")
    main(sys.argv[1], sys.argv[2:])
