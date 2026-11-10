"""Edit an existing notebook by cell id; every other cell and its saved outputs stay exactly as they were.

    python edit_notebook.py replace-text <notebook> <cell id> <old.txt> <new.txt>
    python edit_notebook.py replace-cell <notebook> <cell id> <source.txt>
    python edit_notebook.py insert-after <notebook> <cell id> <code|markdown> <source.txt>
    python edit_notebook.py insert-before <notebook> <cell id> <code|markdown> <source.txt>
    python edit_notebook.py delete-cell  <notebook> <cell id>

Text arguments are files (write them under /tmp); trailing newlines in them are ignored. Get cell ids from read_notebook.py.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

import nbformat
from write_notebook import check_python, to_lines


def dump(notebook: dict, newline: str) -> str:
    """The repo's notebook format: one-space indent, sorted keys, the file's line ending including the last line."""
    return json.dumps(notebook, indent=1, ensure_ascii=False, sort_keys=True).replace("\n", newline) + newline


def load(path: Path) -> tuple[dict, str]:
    """Read a notebook and its line ending. Refuses a file this script would not write back byte for byte."""
    if not path.is_file():
        sys.exit(f"{path} does not exist. Check the path with ls or glob.")
    raw = path.read_bytes().decode("utf-8")
    try:
        notebook = json.loads(raw)
    except json.JSONDecodeError as error:
        sys.exit(f"{path} is not valid JSON ({error}), so it is not an editable notebook.")
    newline = "\r\n" if "\r\n" in raw else "\n"
    if raw != dump(notebook, newline):
        sys.exit(f"{path} is not formatted the way this script writes notebooks, so saving it would change lines you did not edit. "
                 "Do not edit it; say so in your report.")
    return notebook, newline


def read_text(path: str) -> str:
    file = Path(path)
    if not file.is_file():
        sys.exit(f"{path} does not exist. Write the text to a file under /tmp first.")
    return file.read_text(encoding="utf-8-sig").rstrip("\n")


def set_source(cell: dict, source: str) -> None:
    """Replace a cell's source. New code must compile, and an edited code cell loses its outputs, which would be stale."""
    if cell["cell_type"] == "code":
        problem = check_python(source)
        if problem:
            sys.exit(f"Nothing was saved. The new code does not compile: {problem}")
        cell["outputs"] = []
        cell["execution_count"] = None
    cell["source"] = to_lines(source)


def main(args: argparse.Namespace) -> None:
    path = Path(args.notebook)
    notebook, newline = load(path)
    cells = notebook["cells"]
    index = next((i for i, cell in enumerate(cells) if cell.get("id") == args.cell_id), None)
    if index is None:
        sys.exit(f"No cell with id '{args.cell_id}' in {path}. Run read_notebook.py on it to see the cell ids.")
    cell = cells[index]

    if args.command == "replace-text":
        source, old = "".join(cell["source"]), read_text(args.old_file)
        found = source.count(old)
        if found != 1:
            sys.exit(f"The old text was found {found} times in cell {args.cell_id}; it must appear exactly once. "
                     + ("Check it against the cell's source." if found == 0 else "Include more surrounding text."))
        set_source(cell, source.replace(old, read_text(args.new_file)))
    elif args.command == "replace-cell":
        set_source(cell, read_text(args.source_file))
    elif args.command in ("insert-after", "insert-before"):
        new_cell = {"cell_type": args.kind, "id": str(uuid.uuid4()), "metadata": {}, "source": []}
        if args.kind == "code":
            new_cell.update(execution_count=None, outputs=[])
        set_source(new_cell, read_text(args.source_file))
        cells.insert(index + 1 if args.command == "insert-after" else index, new_cell)
    else:
        del cells[index]

    text = dump(notebook, newline)
    try:
        nbformat.validate(nbformat.reads(text, as_version=4))
    except nbformat.ValidationError as error:
        sys.exit(f"Nothing was saved. The edit made the notebook invalid nbformat: {error.message}")
    path.write_bytes(text.encode("utf-8"))
    print(f"Saved {path} ({args.command} on cell {args.cell_id}). Nothing was run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Edit a notebook by cell id.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, extra in [("replace-text", ["old_file", "new_file"]), ("replace-cell", ["source_file"]),
                        ("insert-after", ["kind", "source_file"]), ("insert-before", ["kind", "source_file"]),
                        ("delete-cell", [])]:
        command = commands.add_parser(name)
        command.add_argument("notebook")
        command.add_argument("cell_id")
        for argument in extra:
            command.add_argument(argument, choices=["code", "markdown"] if argument == "kind" else None)
    main(parser.parse_args())
