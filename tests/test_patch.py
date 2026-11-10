import subprocess

import pytest

from coding_agent_app import patch


def git(repo, *args, input=None):
    """Run git in repo and return its stdout as bytes."""
    return subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          input=input, capture_output=True, check=True).stdout


def staged_patch(repo):
    """The staged changes as a patch against HEAD, built the way build_patch builds it (a moved file is a delete plus an add)."""
    return git(repo, "diff", "--cached", "--no-renames", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit holding .github/ci.yml and a.py."""
    git(tmp_path, "init", "-q")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("name: ci\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_a_normal_change_passes(repo):
    """An ordinary edit to an ordinary file has no problems."""
    (repo / "a.py").write_text("x = 2\n")
    git(repo, "add", "-A")
    assert patch.validate_patch(staged_patch(repo)) == ([], [])


def test_moving_a_file_out_of_a_blocked_path_is_caught(repo):
    """A file moved out of a blocked path is caught by its old path, so a move cannot get around the block."""
    (repo / ".github" / "ci.yml").rename(repo / "ci.yml")
    git(repo, "add", "-A")
    fixable, unfixable = patch.validate_patch(staged_patch(repo))
    assert any(".github/ci.yml is a blocked path" in problem for problem in fixable) and unfixable == []


def test_a_symlink_is_caught(repo):
    """A symlink is a fixable problem."""
    # a symlink is staged through the index, since creating one on disk needs privileges on Windows
    blob = git(repo, "hash-object", "-w", "--stdin", input=b"../outside").decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    fixable, unfixable = patch.validate_patch(staged_patch(repo))
    assert any("link would be a symbolic link" in problem for problem in fixable) and unfixable == []


def test_a_binary_file_is_caught(repo):
    """A binary file is a fixable problem."""
    (repo / "model.bin").write_bytes(b"\x00\x01\x02\x03")
    git(repo, "add", "-A")
    fixable, unfixable = patch.validate_patch(staged_patch(repo))
    assert any("model.bin is a binary file" in problem for problem in fixable) and unfixable == []


def test_an_unreadable_or_oversized_patch_cannot_be_fixed(monkeypatch):
    """A patch git cannot read, or one over the size limit, is unfixable: there is nothing for the agent to correct."""
    fixable, unfixable = patch.validate_patch(b"this is not a patch")
    assert fixable == [] and unfixable
    monkeypatch.setattr(patch, "MAX_PATCH_BYTES", 10)
    fixable, unfixable = patch.validate_patch(b"x" * 11)
    assert fixable == [] and "limit is 10" in unfixable[0]
