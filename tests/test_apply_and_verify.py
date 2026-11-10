import subprocess

import pytest

from coding_agent_app.apply_and_verify import apply_patch_and_push


def git(repo, *args):
    """Run git in repo and return its stdout as text."""
    return subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          capture_output=True, check=True).stdout.decode().strip()


def patch_for(work, base_commit, content):
    """The patch that turns a.sqlx into content, made in the working clone and then undone there."""
    (work / "a.sqlx").write_bytes(content)
    git(work, "add", "-A")
    patch = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--no-renames", base_commit], capture_output=True, check=True).stdout
    git(work, "reset", "-q", "--hard", base_commit)
    return patch


@pytest.fixture
def remote(tmp_path):
    """A bare repo standing in for GitHub, with dev holding a.sqlx. Returns (origin, working clone, dev's first commit)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-q", "-B", "dev")
    (work / "a.sqlx").write_bytes(b"line 1\nline 2\nline 3\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "dev")
    return origin, work, git(work, "rev-parse", "HEAD")


def test_a_first_task_is_pushed_to_its_own_branch_and_dev_is_left_alone(remote):
    """A first task's patch is committed by the coding agent and pushed to a branch of its own; dev is untouched."""
    origin, work, start = remote
    patch = patch_for(work, start, b"line 1\nline TWO\nline 3\n")
    result = apply_patch_and_push(patch, start, "dev", origin.as_uri(), new_branch="agent/t1", commit_message="agent: change (task t1)\n\nWhy.")
    assert result.applied and result.branch == "agent/t1" and result.commit == git(origin, "rev-parse", "agent/t1")
    assert git(origin, "log", "-1", "--format=%an|%ae|%s", "agent/t1") == "coding-agent|coding-agent@noreply.invalid|agent: change (task t1)"
    assert git(origin, "rev-parse", "dev") == start


def test_a_revision_adds_a_commit_on_top_of_the_agent_branch(remote):
    """A revision's patch is committed on top of the agent's branch, as a new commit on the same branch."""
    origin, work, start = remote
    first = apply_patch_and_push(patch_for(work, start, b"line 1\nline TWO\nline 3\n"), start, "dev", origin.as_uri(),
                                 new_branch="agent/t1", commit_message="agent: first")
    git(work, "fetch", "-q", "origin", "agent/t1")
    git(work, "checkout", "-q", first.commit)
    revision = apply_patch_and_push(patch_for(work, first.commit, b"line 1\nline TWO\nline THREE\n"), first.commit, "agent/t1",
                                    origin.as_uri(), is_revision=True, commit_message="agent: revision")
    assert revision.applied and revision.branch == "agent/t1"
    assert git(origin, "log", "--format=%s", "agent/t1").splitlines() == ["agent: revision", "agent: first", "base"]


def test_a_patch_that_conflicts_with_a_moved_dev_is_not_pushed(remote):
    """A patch that conflicts with changes made to dev since the sandbox started is not applied, and nothing is pushed."""
    origin, work, start = remote
    patch = patch_for(work, start, b"line ONE\nline 2\nline 3\n")
    (work / "a.sqlx").write_bytes(b"line 1 changed on dev\nline 2\nline 3\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "dev moves")
    git(work, "push", "-q", "origin", "dev")
    result = apply_patch_and_push(patch, start, "dev", origin.as_uri(), new_branch="agent/t2", commit_message="agent: change")
    assert result.dev_moved and not result.applied and result.branch is None and result.commit is None
    assert "agent/t2" not in git(origin, "branch", "--list")
