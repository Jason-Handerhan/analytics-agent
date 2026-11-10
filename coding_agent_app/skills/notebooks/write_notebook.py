"""Create a new notebook from a percent-format Python file: python write_notebook.py <cells.py> <new.ipynb>"""
import ast
import json
import re
import sys
import uuid
from pathlib import Path

import nbformat

# used when the new notebook's folder has no other notebook to copy the kernel from
DEFAULT_METADATA = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
}
CELL_MARKER = re.compile(r"^# %%(.*)$")


def to_lines(text: str) -> list[str]:
    """A cell's source as a notebook stores it: a list of lines, each but the last ending in a newline."""
    lines = text.split("\n")
    return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def parse_cells(text: str) -> tuple[list[dict], list[str]]:
    """Split percent-format text into notebook cells. Returns the cells and every problem found."""
    # a '# %%' line starts a code cell, '# %% [markdown]' a markdown cell; the lines below belong to it
    chunks: list[tuple[str, list[tuple[int, str]]]] = []
    errors = []
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        marker = CELL_MARKER.match(line)
        if marker:
            chunks.append(("markdown" if "[markdown]" in marker.group(1) else "code", []))
        elif chunks:
            chunks[-1][1].append((number, line))
        elif line.strip():
            errors.append(f"line {number}: text before the first '# %%' line. Start every cell with '# %%' (code) or '# %% [markdown]'.")

    cells = []
    for kind, lines in chunks:
        body = []
        for number, line in lines:
            if kind == "markdown":
                # markdown text sits in comments: '# text' becomes 'text'
                if line.startswith("# ") or line == "#":
                    line = line[2:]
                elif line.strip():
                    errors.append(f"line {number}: a markdown cell's lines must start with '# '.")
            body.append(line)
        source = "\n".join(body).strip("\n")
        if not source:
            continue
        cell = {"cell_type": kind, "id": str(uuid.uuid4()), "metadata": {}, "source": to_lines(source)}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        cells.append(cell)
    return cells, errors


def check_python(source: str) -> str | None:
    """Compile a code cell's source the way a notebook would, without running it. Returns the problem, if any."""
    if source.startswith("%%"):  # a cell magic's body is not Python
        return None
    # line magics (%x, !x) are blanked out so line numbers still match the cell
    python = "\n".join("" if line.lstrip().startswith(("%", "!")) else line for line in source.split("\n"))
    try:
        compile(python, "cell", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as error:
        return f"line {error.lineno} of the cell: {error.msg}"
    return None


def main(source_path: str, notebook_path: str) -> None:
    source, notebook_file = Path(source_path), Path(notebook_path)
    if notebook_file.exists():
        sys.exit(f"{notebook_file} already exists, and this script never overwrites a notebook. Choose a new name.")
    if not source.is_file():
        sys.exit(f"{source} does not exist. Write the percent-format file first, then run this script on it.")

    # utf-8-sig also accepts the byte-order mark some Windows tools put at the start of a file
    cells, errors = parse_cells(source.read_text(encoding="utf-8-sig"))
    for index, cell in enumerate(cells):
        problem = check_python("".join(cell["source"])) if cell["cell_type"] == "code" else None
        if problem:
            errors.append(f"cell {index}: {problem}")
    if not cells and not errors:
        errors.append("no cells found. Start every cell with '# %%' (code) or '# %% [markdown]'.")
    if errors:
        sys.exit(f"{source} was not converted:\n" + "\n".join(errors))

    # same kernel as a notebook already in the folder, so the new one opens the way its neighbours do
    metadata = DEFAULT_METADATA
    siblings = sorted(notebook_file.parent.glob("*.ipynb"))
    if siblings:
        sibling = json.loads(siblings[0].read_text(encoding="utf-8"))["metadata"]
        metadata = {key: sibling[key] for key in ("kernelspec", "language_info") if key in sibling}

    notebook = {"cells": cells, "metadata": metadata, "nbformat": 4, "nbformat_minor": 5}
    # the repo's notebooks: one-space indent, sorted keys, CRLF line endings including the last line
    text = json.dumps(notebook, indent=1, ensure_ascii=False, sort_keys=True).replace("\n", "\r\n") + "\r\n"
    notebook_file.parent.mkdir(parents=True, exist_ok=True)
    notebook_file.write_bytes(text.encode("utf-8"))

    try:
        nbformat.validate(nbformat.reads(notebook_file.read_text(encoding="utf-8"), as_version=4))
    except nbformat.ValidationError as error:
        notebook_file.unlink()
        sys.exit(f"The notebook was not valid nbformat and was removed: {error.message}")
    print(f"Wrote {notebook_file}: {len(cells)} cells, all outputs empty. Nothing was run.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python write_notebook.py <percent-format .py file> <new .ipynb file>")
    main(sys.argv[1], sys.argv[2])
