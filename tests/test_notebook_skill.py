import json
import subprocess
import sys
from pathlib import Path

import nbformat

SKILL_DIR = Path(__file__).resolve().parents[1] / "coding_agent_app" / "skills" / "notebooks"


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SKILL_DIR / name), *args], capture_output=True, text=True, encoding="utf-8")


def write_notebook(path: Path, cells: list[dict]) -> Path:
    notebook = {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    text = json.dumps(notebook, indent=1, ensure_ascii=False, sort_keys=True).replace("\n", "\r\n") + "\r\n"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_read_notebook_prints_sources_and_ids_but_no_outputs(tmp_path):
    image = "iVBORw0KGgo" * 5000
    path = write_notebook(tmp_path / "nb.ipynb", [
        {"cell_type": "markdown", "id": "intro", "metadata": {}, "source": "# 📊 Title\n"},
        {"cell_type": "code", "id": "load", "metadata": {}, "execution_count": 1,
         "source": ["df = load()\n", "df.head()"],
         "outputs": [{"output_type": "display_data", "data": {"image/png": image}, "metadata": {}}]},
    ])
    result = run_script("read_notebook.py", str(path))
    assert result.returncode == 0
    assert "id=intro" in result.stdout and "id=load" in result.stdout
    assert "df = load()\ndf.head()" in result.stdout  # a list source is joined; CRLF in the file does not leak in
    assert "iVBORw0KGgo" not in result.stdout
    assert len(result.stdout) < 500


def test_read_notebook_names_the_problem_for_a_missing_or_broken_file(tmp_path):
    missing = run_script("read_notebook.py", str(tmp_path / "gone.ipynb"))
    assert missing.returncode == 1 and "does not exist" in missing.stderr
    broken = tmp_path / "broken.ipynb"
    broken.write_text("{not json")
    assert "not valid JSON" in run_script("read_notebook.py", str(broken)).stderr


PERCENT_FILE = """# %% [markdown]
# # Aisle pacing
# Compares the ranker with and without the feature.

# %%
%matplotlib inline
import pandas as pd

df = pd.DataFrame({"a": [1]})
result = await load(df)
"""


def test_write_notebook_builds_a_valid_notebook_in_the_repo_format(tmp_path):
    sibling = write_notebook(tmp_path / "existing.ipynb", [])
    sibling.write_bytes(sibling.read_bytes().replace(b'"metadata": {}', b'"metadata": {"kernelspec": {"display_name": "Repo", "language": "python", "name": "repo-kernel"}}', 1))
    source = tmp_path / "cells.py"
    source.write_bytes(b"\xef\xbb\xbf" + PERCENT_FILE.encode("utf-8"))  # starts with a byte-order mark, as PowerShell writes it
    new = tmp_path / "new.ipynb"
    result = run_script("write_notebook.py", str(source), str(new))
    assert result.returncode == 0, result.stderr

    raw = new.read_bytes()
    assert raw.endswith(b"\r\n") and raw.count(b"\n") == raw.count(b"\r\n")
    notebook = nbformat.reads(raw.decode("utf-8"), as_version=4)
    nbformat.validate(notebook)
    assert [cell.cell_type for cell in notebook.cells] == ["markdown", "code"]
    assert notebook.cells[0].source == "# Aisle pacing\nCompares the ranker with and without the feature."
    assert notebook.cells[1].source.startswith("%matplotlib inline\nimport pandas as pd")
    assert len({cell.id for cell in notebook.cells}) == 2
    assert all(cell.outputs == [] for cell in notebook.cells if cell.cell_type == "code")
    assert notebook.metadata.kernelspec.name == "repo-kernel"


def test_write_notebook_never_overwrites_an_existing_notebook(tmp_path):
    existing = write_notebook(tmp_path / "keep.ipynb", [{"cell_type": "markdown", "id": "a", "metadata": {}, "source": "saved\n"}])
    before = existing.read_bytes()
    source = tmp_path / "cells.py"
    source.write_text(PERCENT_FILE)
    result = run_script("write_notebook.py", str(source), str(existing))
    assert result.returncode == 1 and "already exists" in result.stderr
    assert existing.read_bytes() == before


def test_write_notebook_reports_every_problem_and_writes_nothing(tmp_path):
    source = tmp_path / "cells.py"
    source.write_text("stray = 1\n# %%\nx = 1\n\n# %%\ndef broken(:\n    pass\n\n# %% [markdown]\nnot a comment\n")
    new = tmp_path / "new.ipynb"
    result = run_script("write_notebook.py", str(source), str(new))
    assert result.returncode == 1
    assert "line 1: text before the first" in result.stderr
    assert "line 10: a markdown cell's lines must start with '# '" in result.stderr
    assert "cell 1: line 1 of the cell" in result.stderr
    assert not new.exists()


def code_cell(cell_id: str, source: str, output: str = "") -> dict:
    outputs = [{"output_type": "stream", "name": "stdout", "text": [output]}] if output else []
    return {"cell_type": "code", "id": cell_id, "metadata": {}, "execution_count": 3 if output else None,
            "outputs": outputs, "source": source.splitlines(keepends=True)}


def edit(tmp_path: Path, *args: str, **files: str) -> subprocess.CompletedProcess:
    """Run edit_notebook.py; each keyword is a text file written under tmp_path and its path replaces {name} in args."""
    paths = {}
    for name, text in files.items():
        paths[name] = tmp_path / f"{name}.txt"
        paths[name].write_text(text)
    return run_script("edit_notebook.py", *[arg.format(**paths) for arg in args])


def test_edit_notebook_changes_one_cell_and_leaves_the_rest_byte_identical(tmp_path):
    notebook = write_notebook(tmp_path / "nb.ipynb", [
        code_cell("a", "x = 1\ny = 2\n", output="saved output a"),
        code_cell("b", "z = 3", output="saved output b"),
    ])
    result = edit(tmp_path, "replace-text", str(notebook), "a", "{old}", "{new}", old="y = 2", new="y = 20\nprint(y)")
    assert result.returncode == 0, result.stderr

    raw = notebook.read_bytes()
    assert raw.endswith(b"\r\n") and raw.count(b"\n") == raw.count(b"\r\n")
    cells = {cell["id"]: cell for cell in json.loads(raw)["cells"]}
    assert cells["a"]["source"] == ["x = 1\n", "y = 20\n", "print(y)\n"]  # no empty last element
    assert cells["a"]["outputs"] == [] and cells["a"]["execution_count"] is None
    assert cells["b"] == code_cell("b", "z = 3", output="saved output b")


def test_edit_notebook_refuses_a_bad_edit_and_leaves_the_file_alone(tmp_path):
    notebook = write_notebook(tmp_path / "nb.ipynb", [code_cell("a", "x = 1\nx = 1")])
    before = notebook.read_bytes()
    attempts = [
        (("replace-text", str(notebook), "a", "{old}", "{new}"), {"old": "nope", "new": "y"}, "found 0 times"),
        (("replace-text", str(notebook), "a", "{old}", "{new}"), {"old": "x = 1", "new": "y"}, "found 2 times"),
        (("replace-text", str(notebook), "missing", "{old}", "{new}"), {"old": "x", "new": "y"}, "No cell with id 'missing'"),
        (("replace-cell", str(notebook), "a", "{source}"), {"source": "def broken(:"}, "does not compile"),
    ]
    for args, files, message in attempts:
        result = edit(tmp_path, *args, **files)
        assert result.returncode == 1 and message in result.stderr, (message, result.stderr)
        assert notebook.read_bytes() == before


def test_edit_notebook_inserts_and_deletes_cells(tmp_path):
    notebook = write_notebook(tmp_path / "nb.ipynb", [code_cell("a", "x = 1"), code_cell("b", "y = 2")])
    assert edit(tmp_path, "insert-after", str(notebook), "a", "markdown", "{source}", source="# Notes").returncode == 0
    cells = json.loads(notebook.read_bytes())["cells"]
    assert [cell["id"] for cell in cells][::2] == ["a", "b"] and cells[1]["cell_type"] == "markdown"
    assert "".join(cells[1]["source"]) == "# Notes"
    assert edit(tmp_path, "insert-before", str(notebook), "a", "code", "{source}", source="import pandas as pd").returncode == 0
    cells = json.loads(notebook.read_bytes())["cells"]
    assert [cell["cell_type"] for cell in cells][:3] == ["code", "code", "markdown"] and cells[1]["id"] == "a"
    assert "".join(cells[0]["source"]) == "import pandas as pd"
    assert edit(tmp_path, "delete-cell", str(notebook), "b").returncode == 0
    cells = json.loads(notebook.read_bytes())["cells"]
    assert [cell["id"] for cell in cells][1] == "a" and len(cells) == 3


def test_edit_notebook_refuses_a_notebook_it_would_reformat(tmp_path):
    notebook = tmp_path / "other.ipynb"
    notebook.write_text(json.dumps({"cells": [code_cell("a", "x = 1")], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, indent=4))
    result = edit(tmp_path, "delete-cell", str(notebook), "a")
    assert result.returncode == 1 and "would change lines you did not edit" in result.stderr
