import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langsmith.sandbox import SandboxClient

from app.coding_task_models import FinalCheck
from app.config import REPO_URL
from coding_agent_app.patch import WORK_DIR

CHECK_DETAIL_CHARS = 2_000  # a failed check keeps this many characters from the end of its output


@dataclass
class ApplyPatchResult:
    """The patch applied to a fresh clone of dev, as the job will apply it."""
    dev_commit: str   # dev's tip when cloned
    dev_moved: bool   # dev moved since the sandbox started
    applied: bool     # whether the patch applied (git apply --3way succeeded)
    error: str        # git's message when it did not apply
    diffstat: str     # lines added and removed per file
    branch: str | None = None  # the branch pushed; None if nothing was pushed
    commit: str | None = None  # the commit pushed


# Only matters on Windows (local runs and tests): it stops cleaned-up clones piling up in the temp folder.
# Not needed in production, on Linux.
def make_writable_and_retry(function, path, error):
    """For shutil.rmtree. On Windows git's object files are read-only and cannot be deleted: make the file writable, then retry."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def apply_patch_and_push(patch_bytes: bytes, sandbox_start_commit: str, base_branch: str, repo_url: str = REPO_URL,
                       new_branch: str | None = None, is_revision: bool = False, commit_message: str = "") -> ApplyPatchResult:
    """Check that the patch applies to the base branch (dev, or the agent's own branch for a revision) as it is now, which may
    have moved since the sandbox started: apply it to a fresh shallow clone of its latest commit. A patch that applies is also
    committed and pushed: to new_branch for a first task, or onto the base branch for a revision (is_revision). If new_branch is
    not given and is_revision is False, nothing is pushed and the patch is only checked. repo_url is the repo to clone from:
    the GitHub repo by default, a local repo in tests."""
    workdir = tempfile.mkdtemp(prefix="patch_check_")

    try:
        # clone only the base branch's latest commit (autocrlf off so Windows can't change line endings)
        subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", "--depth", "1", "--branch", base_branch, repo_url, workdir], check=True)

        # the base branch's latest commit; if it is not the commit the sandbox started from, the branch has moved
        tip = subprocess.run(["git", "-C", workdir, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        moved = tip != sandbox_start_commit
        # the branch has moved: also fetch the start commit, so git has the original files the patch was made from and can merge
        if moved:
            subprocess.run(["git", "-C", workdir, "fetch", "-q", "--depth", "1", "origin", sandbox_start_commit], capture_output=True)

        # write the patch into the clone so git can read it
        patch_file = Path(workdir) / "changes.patch"
        patch_file.write_bytes(patch_bytes)
        # apply the patch; --3way merges it with changes dev made to the same files, and fails on a conflict
        applied = subprocess.run(["git", "-C", workdir, "apply", "--3way", "--whitespace=nowarn", str(patch_file)], capture_output=True, text=True)

        # remove the patch file so it is not counted as a change, then stage everything (new files too)
        patch_file.unlink()
        subprocess.run(["git", "-C", workdir, "add", "-A"], capture_output=True)
        # lines added and removed per file, as the patch ends up on dev
        diffstat = subprocess.run(["git", "-C", workdir, "diff", "--cached", "-M", "--stat", "HEAD"], capture_output=True, text=True).stdout.strip()
        error = "" if applied.returncode == 0 else applied.stderr.strip()  # git's message when the patch did not apply

        # push the patch to a branch that depends on the kind of task
        pushed_branch = None
        pushed_commit = None
        # but only if it applied (git exited with 0) and changed something (an empty diffstat means dev already has these changes)
        if applied.returncode == 0 and diffstat:
            if is_revision:
                # a revision: the clone is already on the agent's branch, so the commit goes on top of the previous work
                pushed_branch = base_branch
            elif new_branch:
                # a first task: commit on a new branch of its own, so dev is left alone
                subprocess.run(["git", "-C", workdir, "checkout", "-q", "-b", new_branch], check=True)
                pushed_branch = new_branch

        # commit and push; nothing to do when this was only a check
        if pushed_branch:
            # the author is the coding agent, set for this one commit and not in any git config
            subprocess.run(["git", "-C", workdir, "-c", "user.name=coding-agent", "-c", "user.email=coding-agent@noreply.invalid",
                            "commit", "-q", "-m", commit_message], check=True)
            # git takes its credentials from the environment, not from this code
            subprocess.run(["git", "-C", workdir, "push", "-q", "origin", pushed_branch], check=True)
            pushed_commit = subprocess.run(["git", "-C", workdir, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        return ApplyPatchResult(dev_commit=tip, dev_moved=moved, applied=applied.returncode == 0, error=error, diffstat=diffstat,
                                branch=pushed_branch, commit=pushed_commit)
    finally:
        shutil.rmtree(workdir, onexc=make_writable_and_retry)  # delete the temporary clone (cleanup on Windows needs to make git's read-only object files writable first)


@dataclass
class CleanSandboxVerification:
    """What ran in the clean second sandbox. Each step holds the result of sandbox.run(): .stdout and .stderr (text) and
    .exit_code (0 means it succeeded). A step is None when it did not run."""
    apply: Any                # git apply: exit 0 if applied, else git's complaint is in .stderr. Always runs
    compile: Any = None       # dataform compile --json: .stdout is JSON with "tables" and "graphErrors". Runs if the patch applied
    dependencies: Any = None  # uv pip install: .stderr lists "+ package==version" per package. Runs if requirements.txt changed
    ruff: Any = None          # ruff check: .stdout lists findings, one per line; exit 0 if none. Runs if a changed .py file still exists
    pytest: Any = None        # pytest -q: .stdout ends with a summary like "2 passed"; exit 0 passed, 5 no tests. Runs if a .py file changed


def verify_patch_in_clean_sandbox(client: SandboxClient, snapshot_id: str, patch_bytes: bytes, changed_paths: list[str],
                                  dev_moved: bool, sandbox_start_commit: str, base_branch: str) -> CleanSandboxVerification:
    """Apply the patch to a clean clone of the base branch in a fresh sandbox and check it there: compile, dependencies, ruff, pytest.
    The agent's own sandbox proves nothing, because it had a shell there. The job container is not used, because compiling
    runs the project's JavaScript and the container holds the git token."""
    with client.sandbox(snapshot=snapshot_id) as sandbox:
        # clone only the base branch's latest commit
        sandbox.run(f"git clone --depth 1 --branch {base_branch} {REPO_URL} {WORK_DIR}", timeout=300)
        # the branch has moved: also fetch the start commit, so git has the original files the patch was made from and can merge
        if dev_moved:
            sandbox.run(f"git -C {WORK_DIR} fetch --depth 1 origin {sandbox_start_commit}", timeout=300)

        # apply the patch (--3way merges it with changes dev made to the same files); if it does not apply, nothing else can run
        sandbox.write("/tmp/changes.patch", patch_bytes)
        result = CleanSandboxVerification(apply=sandbox.run(f"git -C {WORK_DIR} apply --3way --whitespace=nowarn /tmp/changes.patch"))
        if result.apply.exit_code != 0:
            return result

        # compile the Dataform project; the JSON output lists its tables and any compilation errors
        result.compile = sandbox.run(f"cd {WORK_DIR} && dataform compile --json", timeout=300)

        # dependencies: if the patch changes requirements.txt, install from it as a person would, with versions held to
        # requirements.lock. The repo runs on a Vertex AI Workbench VM and the lock file is a freeze of that VM's environment, so
        # it also lists the VM's own packages (nb_micromamba_kernels, vertex_ai_workbench, wbi_*). Those are files on the VM and
        # cannot be installed here, so the grep drops them. gcsfs, which the code needs to read gs:// paths, was apparently
        # preinstalled on the VM, so requirements.txt never declared it: it is added to the install here.
        if "requirements.txt" in changed_paths:
            result.dependencies = sandbox.run(
                f"cd {WORK_DIR} && grep -vE '^(nb_micromamba_kernels|vertex_ai_workbench|wbi_)' requirements.lock > /tmp/constraints.txt "
                "&& UV_HTTP_TIMEOUT=300 UV_CONCURRENT_DOWNLOADS=4 uv pip install --python /opt/ml-venv/bin/python -r requirements.txt gcsfs -c /tmp/constraints.txt",
                timeout=900)
    
        # Python changes: list the changed .py files that still exist (added, copied, modified, not deleted)
        if any(path.endswith(".py") for path in changed_paths):
            existing_python_files = sandbox.run(f"git -C {WORK_DIR} add -A && git -C {WORK_DIR} diff --cached --name-only --diff-filter=ACMR HEAD -- '*.py'").stdout.splitlines()

            # ruff on those files, for syntax errors and undefined names only, so older style problems do not fail the task
            if existing_python_files:
                result.ruff = sandbox.run(f"cd {WORK_DIR} && ruff check --select E9,F63,F7,F82 {shlex.join(existing_python_files)}")

            # Run the repo's tests; pytest imports the code the agent wrote, which is why it runs here
            result.pytest = sandbox.run(f"cd {WORK_DIR} && python -m pytest -q", timeout=600)
        return result


@dataclass
class FinalChecks:
    """The checks every task gets, and whether they all passed."""
    checks: list[FinalCheck]        # every check that ran
    failed_checks: list[str]        # names of the checks that failed
    passed: bool                    # True if no check failed
    compiled_tables: list           # tables in the clean sandbox's compile output; empty if it did not run or was not JSON
    compile_errors: list            # compilation errors in that output
    needs_rebase: bool              # dev moved and the patch no longer applies to it


def build_final_checks(validation_problems: list[str], apply_patch_result: ApplyPatchResult | None, clean_sandbox_verification: CleanSandboxVerification | None) -> FinalChecks:
    """Collect what the earlier steps produced into FinalChecks: patch validation, applying to dev, and each step run in the
    clean sandbox. Nothing is run here. Each step becomes a FinalCheck (passed or not, and the end of its output if it
    failed); the compile's tables and errors and whether the task needs a rebase come along. Checks specific to one task
    do not belong here."""
    checks = []
    compiled_tables, compile_errors, needs_rebase = [], [], False

    # ---- from patch validation (validation_problems): did the agent's patch pass? If not, the problems are the detail
    checks.append(FinalCheck(name="patch passes validation", passed=not validation_problems,
                             detail="\n".join(validation_problems)))

    # the checks below only run for a patch that passed validation
    if not validation_problems:
        # ---- from applying the patch to a fresh clone of dev (apply_patch_result)
        checks.append(FinalCheck(name="patch applies to a fresh clone of the current dev", passed=apply_patch_result.applied,
                                 detail=apply_patch_result.error))
        # dev moved and the patch conflicts with its changes: the task ends as needs_rebase
        needs_rebase = apply_patch_result.dev_moved and not apply_patch_result.applied

        # ---- from the clean sandbox (clean_sandbox_verification): one check per step that ran
        sandbox_apply = clean_sandbox_verification.apply
        checks.append(FinalCheck(name="patch applies in the clean sandbox", passed=sandbox_apply.exit_code == 0,
                                 detail=sandbox_apply.stderr))

        # the Dataform compile (dataform compile --json): it prints JSON with the project's tables and any compilation errors,
        # and no JSON if it did not run or crashed
        sandbox_compile = clean_sandbox_verification.compile
        compiled_project = None
        if sandbox_compile is not None:
            try:
                compiled_project = json.loads(sandbox_compile.stdout)
            except json.JSONDecodeError:
                pass
        compiled_tables = (compiled_project or {}).get("tables", [])
        compile_errors = (compiled_project or {}).get("graphErrors", {}).get("compilationErrors", [])
        compile_passed = sandbox_compile is not None and sandbox_compile.exit_code == 0 and not compile_errors
        if sandbox_compile is None:
            compile_output = "The compile did not run."
        else:
            # what went wrong: the compilation errors, or else whatever the compile printed to stderr
            compile_output = "\n".join(f"{error.get('fileName', '')}: {error.get('message', '')}" for error in compile_errors) or sandbox_compile.stderr
        checks.append(FinalCheck(name="compile passes in the clean sandbox", passed=compile_passed, detail=compile_output))

        sandbox_dependencies = clean_sandbox_verification.dependencies
        if sandbox_dependencies is not None:
            checks.append(FinalCheck(name="dependencies declared in requirements.txt install cleanly",
                                     passed=sandbox_dependencies.exit_code == 0, detail=sandbox_dependencies.stderr))

        sandbox_ruff = clean_sandbox_verification.ruff
        if sandbox_ruff is not None:
            checks.append(FinalCheck(name="ruff finds no syntax or undefined-name errors in the changed Python files",
                                     passed=sandbox_ruff.exit_code == 0, detail=sandbox_ruff.stdout))

        sandbox_pytest = clean_sandbox_verification.pytest
        if sandbox_pytest is not None:
            # exit code 5 means no tests were collected, which is fine for a repo without a test suite
            checks.append(FinalCheck(name="pytest passes in the clean sandbox", passed=sandbox_pytest.exit_code in (0, 5),
                                     detail=sandbox_pytest.stdout))

    # so far each check's detail holds the whole output of its step; keep it only for a check that failed, and only the last
    # CHECK_DETAIL_CHARS characters of it (the error is usually at the end)
    for check in checks:
        check.detail = None if check.passed else check.detail[-CHECK_DETAIL_CHARS:]

    # the task passes only if no check failed
    failed_checks = [check.name for check in checks if not check.passed]
    return FinalChecks(checks=checks, failed_checks=failed_checks, passed=not failed_checks, compiled_tables=compiled_tables,
                       compile_errors=compile_errors, needs_rebase=needs_rebase)
