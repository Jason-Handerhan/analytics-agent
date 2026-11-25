# Writing a code plan

A code plan changes the ML repo: SQLX tables, Python and notebooks. The plan's `description` is markdown with the sections below. Leave out a section that does not apply.

## Requirements

The deliverables, one item per thing to build or change, each naming the table, file or model it applies to. Split anything joined by 'and' that touches more than one place. For example: 'Add user_aisle_pacing_ratio to prelim_user_aisle_features', 'Forward-fill the new columns in skeleton_features', 'Expose user_aisle_pacing_ratio in final_ml_features_table and add it to rowConditions', 'Pass the feature to the LGBM ranker only', 'Create a notebook comparing the ranker with and without the feature, including gain importance'. How to tell it works is not a requirement (put the test behaviours in Tests), and what not to do goes in Constraints. A table that aggregates `agent_safe` for the dashboard is a notebook in `Notebooks/dashboard_data/` that aggregates with SQL in BigQuery and writes the result to the `agent_dashboard` dataset.

## Tests

Leave this section out when the change needs no new test. Only what the compile, the SQL dry runs and linting cannot check: the behaviours the new pytest tests must pin down, each as one sentence (for example 'select_features returns the feature only for the ranker'). Ask for the fewest tests that would fail if the behaviour broke; no tests of wiring, training, real data or SQL.

## Constraints

Rules the change must follow: paths it may or may not touch, naming conventions, models or tables that must stay as they are, a limit on scope.

## Files to imitate

Existing repo files whose style or structure the new code should follow, each with what to follow in it, specifically: its column documentation block, how it forward-fills, its test style.

## Decisions

Choices the user made that settle something the agent would otherwise guess (which of two approaches, a name, a threshold, what is out of scope), each as an instruction with the user's reason if they gave one. If the user dictated specific logic or SQL, write it here in full. Do not list your own suggestions here; put code you suggest in Suggested code.

## Suggested code

Code you suggest as a starting point, each with its purpose, the file it would live in if it belongs to one, and the code. The coding agent treats it as unverified, and the repo wins on any conflict. Do not paste code that already exists in the repo; name the file in Files to imitate instead.

## What the reviewer checks (for the user, not the plan)

This is not part of the plan's description: do not write it there, or adapt it to the plan. The plan only sets `review` on or off. When review is on, a reviewer checks the finished work against the criteria below and sends the coding agent back to fix what fails. Tell the user what is checked when you show the plan.

Judge only the lines the diff adds or changes, and what those lines refer to. Problems that already exist in code the diff does not touch are not failures. A criterion that does not apply to this task passes. Clearing the saved execution count of a notebook cell that was edited is expected, not a violation.
1. Every deliverable the task asks for is present in the diff, not only described in the agent's report or its todo list.
2. Every column named in an assertion line (such as rowConditions) that the diff adds or changes is produced by that table's own query.
3. Every column or alias that a SQL line added or changed by the diff reads is produced by the table or CTE it comes from, and is in scope where it is used.
4. Every feature name that the diff adds to a Python feature list is a column that definitions/gold/final_ml_features_table.sqlx produces.
5. Every function, import or constant used by the Python lines the diff adds or changes exists.
6. Every caller of a function whose signature the diff changes still works.
7. Every claim the report makes about what the diff changes is true, and it claims no change the diff lacks. Claims about what was run are not judged.
8. The final report lists every file the diff changes and says what changed in each.
9. The diff contains nothing the task did not ask for, and no scratch or debug files.
10. The added tests do not parse source code, copy a production list, run a training script, or read real data or a saved model, and no helper's only job is calling another function.
11. For each .sqlx file the diff changes, the transcript lists a valid dry run of its composed SQL (/tmp/composed/<table>.sql).
