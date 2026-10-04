"""Layer 1 tests for app/mcp_server/code_search.py -- the GitHub Contents/
Trees API calls are mocked (docs/testing.md); everything after that is real
logic under test. Fixtures are real, trimmed API response shapes and a real
tiny notebook, under tests/fixtures/code_search/ -- see that directory.
"""
import json
from pathlib import Path

import pytest

import app.mcp_server.code_search as code_search
from app.mcp_server.code_search import GetRepoContentsArgs, _fetch_repo_contents
from fastmcp.exceptions import ToolError

FIXTURES = Path(__file__).parent / "fixtures" / "code_search"
OWNER, REPO, BRANCH, TOKEN = "owner", "repo", "main", None


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    """Just enough of requests.Response to drive _fetch_repo_contents."""
    def __init__(self, *, ok=True, json_data=None, content=b"", text=""):
        self.ok = ok
        self._json_data = json_data
        self.content = content
        self.text = text

    def json(self):
        return self._json_data


def _args(path: str | None) -> GetRepoContentsArgs:
    return GetRepoContentsArgs(path=path)


def test_listing(monkeypatch):
    """Full-repo tree listing, filtered and unfiltered; folder listing;
    a failed Contents API call."""
    tree_url = f"https://api.github.com/repos/{OWNER}/{REPO}/git/trees/{BRANCH}"
    folder_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/src"

    routes = {
        tree_url: FakeResponse(json_data=_load("tree_response.json")),
        folder_url: FakeResponse(json_data=_load("contents_folder.json")),
    }
    monkeypatch.setattr(code_search.requests, "get",
                         lambda url, **kw: routes[url])

    # Full-repo listing, descriptions known -- filtered to the described path only
    result = _fetch_repo_contents(
        _args(None), OWNER, REPO, BRANCH, TOKEN,
        file_descriptions={"README.md": "Project overview and setup instructions."})
    assert result["type"] == "files"
    assert result["entries"] == [
        {"path": "README.md", "description": "Project overview and setup instructions."},
    ]

    # Full-repo listing, no descriptions known at all -- unfiltered, every blob
    result = _fetch_repo_contents(_args(None), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})
    assert result["entries"] == [
        {"path": "README.md", "description": None},
        {"path": "src/utils.py", "description": None},
    ]

    # Folder listing -- the file + the dir, as given, same description wiring, never filtered
    result = _fetch_repo_contents(_args("src"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})
    assert result["entries"] == [
        {"path": "src/utils.py", "description": None},
        {"path": "src/models", "description": None},
    ]

    # A failed Contents API call
    monkeypatch.setattr(code_search.requests, "get",
                         lambda url, **kw: FakeResponse(ok=False, text="not found"))
    with pytest.raises(ToolError, match="GitHub Contents API failed"):
        _fetch_repo_contents(_args("missing.py"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})


def test_small_file_and_oversized_non_notebook(monkeypatch):
    """A small file's inline content decodes; a large non-notebook file fails actionably."""
    small_file_url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/src/hello.py"
    monkeypatch.setattr(code_search.requests, "get",
                         lambda url, **kw: FakeResponse(json_data=_load("contents_small_file.json")))
    result = _fetch_repo_contents(_args("src/hello.py"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})
    assert result == {"type": "file", "path": "src/hello.py", "content": "print('hello world')\n"}

    monkeypatch.setattr(code_search.requests, "get",
                         lambda url, **kw: FakeResponse(json_data=_load("contents_missing_content.json")))
    with pytest.raises(ToolError, match="1MB inline-content limit"):
        _fetch_repo_contents(_args("data/big_dataset.csv"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})


def test_notebook_stripping_and_invalid_json(monkeypatch):
    """A notebook's outputs/execution_count/attachments are stripped on
    fetch; invalid notebook JSON fails actionably."""
    missing_content = _load("contents_missing_content.json")
    download_url = missing_content["download_url"]
    notebook_bytes = (FIXTURES / "sample_notebook.ipynb").read_bytes()

    def fake_get(url, **kw):
        if url == download_url:
            return FakeResponse(content=notebook_bytes)
        return FakeResponse(json_data=missing_content)

    monkeypatch.setattr(code_search.requests, "get", fake_get)
    result = _fetch_repo_contents(
        _args("notebooks/big_notebook.ipynb"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})

    stripped = json.loads(result["content"])
    code_cell, md_cell = stripped["cells"]
    source = code_cell["source"]
    source = "".join(source) if isinstance(source, list) else source
    assert "import matplotlib" in source  # real code survives
    assert code_cell["outputs"] == []
    assert code_cell["execution_count"] is None
    assert md_cell["attachments"] == {}

    # Raw download succeeds, but the bytes aren't valid notebook JSON
    monkeypatch.setattr(code_search.requests, "get",
        lambda url, **kw: FakeResponse(content=b"not a notebook")
                           if url == download_url else FakeResponse(json_data=missing_content))
    with pytest.raises(ToolError, match="isn't valid notebook JSON"):
        _fetch_repo_contents(
            _args("notebooks/broken.ipynb"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})


def test_binary_file_and_length_cap(monkeypatch):
    """A binary file fails actionably on decode; an over-length file fails
    actionably on the character cap."""
    import base64

    png_bytes = (FIXTURES / "dummy.png").read_bytes()
    binary_response = _load("contents_small_file.json") | {
        "content": base64.b64encode(png_bytes).decode("ascii")}
    monkeypatch.setattr(code_search.requests, "get",
                         lambda url, **kw: FakeResponse(json_data=binary_response))
    with pytest.raises(ToolError, match="isn't a text file"):
        _fetch_repo_contents(_args("assets/logo.png"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})

    huge_text = "x" * (code_search.MAX_REPO_FILE_CONTENT_CHARS + 1)
    huge_response = _load("contents_small_file.json") | {
        "content": base64.b64encode(huge_text.encode("utf-8")).decode("ascii")}
    monkeypatch.setattr(code_search.requests, "get",
                         lambda url, **kw: FakeResponse(json_data=huge_response))
    with pytest.raises(ToolError, match="character limit"):
        _fetch_repo_contents(_args("src/huge.py"), OWNER, REPO, BRANCH, TOKEN, file_descriptions={})
