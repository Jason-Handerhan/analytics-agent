import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch

import nbformat

WORK_DIR = "/home/user/repo"  # the repo's path inside the sandbox
# Paths the agent must not change; a patch that touches one is sent back to be fixed
BLOCKED_PATTERNS = (".git/*", ".github/*", "dashboard/*", "images/*", "*.env", "*.pem", "*.key", "service_account.json")
# Limit on what one task may change; generous, so it only catches a runaway agent
MAX_PATCH_BYTES = 5_000_000

# Run inside the sandbox after the agent: puts back each modified file's original line endings.
# The agent's edit tool writes LF, but these .sqlx files are stored with CRLF. Originals come from the sandbox start commit.
RESTORE_ENDINGS = r"""
import subprocess
import sys
from pathlib import Path

root, sandbox_start_commit = Path(sys.argv[1]), sys.argv[2]
changed = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--diff-filter=M", "-z", sandbox_start_commit], capture_output=True, encoding="utf-8", check=True).stdout
for name in filter(None, changed.split("\0")):
    original = subprocess.run(["git", "-C", str(root), "show", f"{sandbox_start_commit}:{name}"], capture_output=True, check=True).stdout
    text = (root / name).read_bytes().replace(b"\r\n", b"\n")
    (root / name).write_bytes(text.replace(b"\n", b"\r\n") if b"\r\n" in original else text)
    print("line endings restored:", name)
"""


def build_patch(sandbox, sandbox_start_commit: str) -> tuple[bytes, str]:
    """The agent's changes as one patch against the commit the sandbox started from. Returns the patch bytes and the
    output of the line-ending restore."""

    # restore the CRLF line endings the agent's edit tool replaced with LF (git would see every line as changed)
    restored = sandbox.run(f"python3 /tmp/restore_endings.py {WORK_DIR} {sandbox_start_commit}").stdout.strip()

    # stage every change
    sandbox.run(f"git -C {WORK_DIR} add -A")

    # the patch is a git diff of the staged changes against the start commit (a moved file is a delete plus an add)
    sandbox.run(f"git -C {WORK_DIR} diff --cached --no-renames {sandbox_start_commit} > /tmp/changes.patch")
    return sandbox.read("/tmp/changes.patch"), restored


@dataclass
class PatchFile:
    added_lines: int | None    # Number of lines the patch adds for a file; None for a binary file
    removed_lines: int | None  # Number of lines the patch removes for a file; None for a binary file
    path: str                  # relative to the repo root; a moved file is two PatchFiles: the old path removed, the new path added


def read_patch(patch_bytes: bytes) -> list[PatchFile]:
    """The files in the patch as git reads them, a list of PatchFile. Raises ValueError with git's message if git cannot
    read the patch."""

    # git's added and removed line counts per file; run from the temp folder, because inside a repo's subfolder
    # (like notebooks/) git skips paths outside it
    numstat = subprocess.run(["git", "apply", "--numstat", "-z"], input=patch_bytes, capture_output=True, cwd=tempfile.gettempdir())
    if numstat.returncode != 0:
        raise ValueError(f"git cannot read the patch: {numstat.stderr.decode(errors='replace').strip()}")

    # NUL separates each file's change; within one, tabs separate "<lines added>", "<lines removed>" and "<path>"
    # (a binary file has no line counts, so git prints "-" for both)
    files = []

    # Loop over the NUL-separated file changes, and build list of PatchFile objects.
    for file_change in numstat.stdout.decode("utf-8", errors="replace").split("\0"):
        if not file_change:  # skip the empty string left after the last NUL
            continue
        added_lines, removed_lines, path = file_change.split("\t", 2)  # the path is everything after the second tab

        # one PatchFile per file; a binary file's "-" counts become None, so validate_patch can reject it
        files.append(PatchFile(None if added_lines == "-" else int(added_lines),
                               None if removed_lines == "-" else int(removed_lines), path))
    if not files:
        raise ValueError("git read no files from the patch")
    return files


def validate_patch(patch_bytes: bytes) -> tuple[list[str], list[str]]:
    """Check the patch and return (fixable, unfixable) lists of messages. Fixable problems send the agent back to correct
    them; an unfixable one fails the task. Every fixable rule runs, so the agent sees all its problems at once."""
    fixable = []
    unfixable = []

    # Reject too large (unfixable): a task that is too big, or an agent that ran away
    if len(patch_bytes) > MAX_PATCH_BYTES:
        unfixable.append(f"The patch is {len(patch_bytes):,} bytes; the limit is {MAX_PATCH_BYTES:,}.")
        return fixable, unfixable

    # Reject empty (fixable): the agent finished without changing anything
    if not patch_bytes.strip():
        fixable.append("The patch is empty: you made no changes. Make the changes the task asks for.")
        return fixable, unfixable

    # Reject unreadable (unfixable): git cannot parse the patch, which the agent cannot correct
    try:
        files = read_patch(patch_bytes)
    except ValueError as error:
        unfixable.append(str(error))
        return fixable, unfixable

    for file in files:
        # Reject blocked paths (fixable): CI workflows, git internals, the Power BI report, images and credential files must not change
        # (see BLOCKED_PATTERNS)
        for pattern in BLOCKED_PATTERNS:
            if fnmatch(file.path, pattern):
                fixable.append(f"{file.path} is a blocked path (matches {pattern}); revert any change to it.")

        # Reject binary files (fixable): a reviewer cannot read it in a PR, and a pickle runs code when loaded
        if file.added_lines is None:
            fixable.append(f"{file.path} is a binary file; binary files cannot be reviewed in a PR, so remove it or use a text format.")

    # Reject symlinks and submodules (fixable): a symlink can point outside the repo and a submodule pulls in another repo's code,
    # and a reviewer sees neither in the diff. Git's summary lists them as files created or changed to mode 120000 or 160000.
    summary = subprocess.run(["git", "apply", "--summary"], input=patch_bytes, capture_output=True, cwd=tempfile.gettempdir())
    for line in summary.stdout.decode("utf-8", errors="replace").splitlines():
        match = re.match(r" (?:create mode|mode change \d+ =>) (120000|160000) (.+)", line)
        if match:
            kind = "symbolic link" if match[1] == "120000" else "submodule"
            fixable.append(f"{match[2]} would be a {kind}; symlinks and submodules are not allowed.")

    return fixable, unfixable


def validate_notebooks(sandbox, notebook_paths: list[str]) -> list[str]:
    """Every notebook the agent added or changed must parse and pass the nbformat schema. Returns fixable messages that
    name the problem and where it is, so the agent can correct it."""
    violations = []
    for path in notebook_paths:

        # a deleted notebook has nothing to check
        if sandbox.run(f"test -f {WORK_DIR}/{path}").exit_code != 0:
            continue

        # read the notebook from the sandbox and check it is a valid notebook, as nbformat defines one
        try:
            nbformat.validate(nbformat.from_dict(json.loads(sandbox.read(f"{WORK_DIR}/{path}").decode("utf-8"))))

        # the file is not UTF-8 text, or not JSON
        except ValueError as error:
            violations.append(f"{path} is not valid notebook JSON ({error}); use the notebooks skill's scripts, or revert it.")

        # valid JSON but not a valid notebook (for example a cell without "source"); the error says where, like $.cells[1]
        except nbformat.ValidationError as error:
            where = f" at {error.original.json_path}" if getattr(error, "original", None) else ""
            violations.append(f"{path} is not a valid notebook{where}: {error.message}. Use the notebooks skill's scripts, or revert it.")
    return violations
