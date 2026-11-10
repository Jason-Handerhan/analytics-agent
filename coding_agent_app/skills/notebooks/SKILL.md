---
name: notebooks
description: Read, create or edit Jupyter notebooks (.ipynb) in the repo with three scripts. Use whenever a task involves a notebook. Never open a notebook with read_file or change one with edit_file or write_file.
---

# Notebooks

The scripts in `src/` are authoritative; the notebooks are for testing and analysis on samples. Put feature changes in the scripts, and touch a notebook only when the task asks for it.

A notebook is a large JSON file. `read_file` returns raw JSON that costs tens of thousands of tokens, and `edit_file` and `write_file` corrupt it. Use the scripts below instead; they run with `python`. Write any text files they need under `/tmp`, never in the repo. Never run a notebook; test its code as a `.py` file (see Testing).

## Read

    python /skills/notebooks/read_notebook.py <notebook>

Prints each cell's index, type, id and source, without outputs. Run it first, to see the cell ids the edit commands need.

## Create

1. Write the cells as a plain Python file at `/tmp/cells.py`. Each `# %%` line starts a code cell and each `# %% [markdown]` line a markdown cell, whose text goes in `# ` comments:

        # %% [markdown]
        # # Title
        # Some text.

        # %%
        import pandas as pd

2. Run `python /skills/notebooks/write_notebook.py /tmp/cells.py <new notebook path>`.
3. It prints every problem it finds (syntax errors name the cell and line). Nothing is written until the file is clean, so fix `/tmp/cells.py` and run it again.

It never overwrites an existing notebook and always writes empty outputs.

## Edit

    python /skills/notebooks/edit_notebook.py <command> <notebook> <cell id> ...

Write the text arguments to files under `/tmp` first.

| To | Write | Run |
|---|---|---|
| Change a few lines of a cell | `/tmp/old.txt` (the exact lines to replace; they must occur once in the cell) and `/tmp/new.txt` | `replace-text <notebook> <id> /tmp/old.txt /tmp/new.txt` |
| Rewrite most of a cell | `/tmp/source.txt` (the cell's full new source) | `replace-cell <notebook> <id> /tmp/source.txt` |
| Add a cell after or before another | `/tmp/source.txt` | `insert-after <notebook> <id> code /tmp/source.txt` or `insert-before ...` (use `markdown` for a markdown cell) |
| Remove a cell | nothing | `delete-cell <notebook> <id>` |

Prefer `replace-text`: its exactly-once rule catches an edit in the wrong place. An edited code cell loses its saved outputs; every other cell stays byte-identical. If a command prints an error, nothing was saved: fix the input and run it again. If it says the notebook is not formatted the way it writes notebooks, do not edit that notebook and say so in your report.

## Writing a good notebook

Apply these whether or not the task mentions them.

- **Follow the neighbours.** Read one or two notebooks in the same folder with `read_notebook.py` first and match their structure and style.
- **Sample by user, never by row.** These features depend on a user's whole history, so a row-level sample leaks the same user across the train and validation split.
- **Import from `src/`.** Call the repo's training and feature functions instead of copying their logic, so the notebook reflects production code.
- **Assert your assumptions at the top** (expected columns, sample size, no user in both splits), so a wrong assumption fails loudly.
- **Keep it concise.** No more cells than the task needs.
- **Say where it lives.** Put it where the task says; if the task does not say, put it beside the related notebooks and name the path in your report.

## Testing

Test the code in a `.py` file, not the notebook. For a new notebook, run `python /tmp/cells.py` before converting it, when it needs only files in the repo. You have no BigQuery access, so code that queries BigQuery cannot run. State in your report what you ran and what you could not run.
