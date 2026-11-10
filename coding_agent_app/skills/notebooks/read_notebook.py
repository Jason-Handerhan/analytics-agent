"""Print a notebook's cells (index, type, id, source) without their outputs: python read_notebook.py <path>"""
import json
import sys
from pathlib import Path


def main(path: str) -> None:
    # a notebook is a JSON file; each error below names the problem so the caller can fix the path or the file
    try:
        notebook = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"{path} does not exist. Check the path with ls or glob.")
    except json.JSONDecodeError as error:
        sys.exit(f"{path} is not valid JSON ({error}), so it is not a readable notebook.")
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        sys.exit(f"{path} has no 'cells' list, so it is not a notebook.")

    # print as UTF-8 whatever the terminal's encoding is
    sys.stdout.reconfigure(encoding="utf-8")
    cells = notebook["cells"]
    print(f"# {path}: {len(cells)} cells")
    for index, cell in enumerate(cells):
        # a cell's source is a string or a list of lines; only source is printed, never the saved outputs
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        print(f"\n=== cell {index} | {cell.get('cell_type')} | id={cell.get('id', 'none')} ===")
        print(source)

# sys.argv holds the script name followed by the arguments
if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python read_notebook.py <path to a .ipynb file>")
    main(sys.argv[1])
