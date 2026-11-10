---
name: sqlx
description: Check that a changed .sqlx table is valid BigQuery SQL. Use after every change to a file in definitions/.
---

# Checking .sqlx changes

After changing a file in definitions/, verify that its SQL runs in BigQuery. `dataform compile` does not check this: it never
sends the SQL to BigQuery. `dry_run_sql` does, but only on SQL that reads tables which exist in BigQuery, and BigQuery still has
the old versions of the tables you changed. These steps build that SQL: each reference to a table this repo builds is replaced
by that table's own SQL, so only raw source tables are left.

## Steps

After changing any file in definitions/:

1. Compile: `cd /home/user/repo && dataform compile --json > /tmp/compile.json`. Fix any error it reports.
2. Compose: `python /skills/sqlx/compose_query.py /tmp/compile.json <each .sqlx file you changed>`
   Paths are relative to the repo, like `definitions/gold/final_ml_features_table.sqlx`. It writes a self-contained query for each
   of those tables and for every table that reads one, and prints each query's file path and how many columns the table documents.
3. Dry-run each printed file: call `dry_run_sql` with `sql_file` set to that path. Never dry-run a shortened or hand-written copy.
4. The result lists the columns the query returns. It must include every column in the table's `columns:` block, including the ones you added.
5. If BigQuery reports an error, fix the .sqlx and repeat from step 1.
