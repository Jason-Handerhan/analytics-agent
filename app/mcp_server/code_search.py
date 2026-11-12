import asyncio
import base64

import nbformat
import requests
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from app.config import MAX_REPO_FILE_CONTENT_CHARS
from app.mcp_server.server import mcp


def _github_headers(access_token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"} if access_token else {}


def _github_tree(owner: str, repo: str, branch: str, access_token: str | None) -> list[str]:
    """Every file path in the repo on one branch. Fails if GitHub truncates the tree for being too large."""
    # one call returns every level of the tree
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=_github_headers(access_token),
    )
    if not resp.ok:
        raise ToolError(f"GitHub Trees API failed: {resp.text}")
    data = resp.json()
    # GitHub cuts a recursive tree off past its size limit
    if data.get("truncated"):
        raise ToolError("Repo tree was truncated by GitHub's API -- too many files for one recursive call.")
    # files only; folders are dropped
    return [item["path"] for item in data["tree"] if item["type"] == "blob"]


def _describe(path: str, file_descriptions: dict[str, str]) -> dict:
    return {"path": path, "description": file_descriptions.get(path)}


def _strip_notebook_outputs(notebook_json: str) -> str:
    """Strips every cell's outputs -- keeps code and markdown only. Outputs
    are a stale, point-in-time snapshot from whenever the notebook last ran,
    not live data, and can balloon with embedded chart images; the code
    itself is what this tool is for. Parsed via nbformat, not raw json --
    this tool is MCP-hosted and reusable against any repo, so it can't
    assume every notebook is already current-format; nbformat auto-upgrades
    an older format first, which a bare json.loads has no way to do."""
    nb = nbformat.reads(notebook_json, as_version=4)
    for cell in nb.cells:
        if "outputs" in cell:
            cell.outputs = []
        if "execution_count" in cell:
            cell.execution_count = None
        if cell.get("attachments"):
            cell.attachments = {}
    return nbformat.writes(nb)


class GetRepoContentsArgs(BaseModel):
    path: str | None = Field(
        default=None,
        description="A file or folder path in the repo, as previously returned "
                    "by this tool. If it's a file, its full text is returned. "
                    "If it's a folder, that folder's immediate contents are "
                    "returned (one level, not recursive). Omit entirely (or pass "
                    "null) to list every file in the whole repo that has a known "
                    "description -- do this first, before guessing a specific path, "
                    "since a guessed path that doesn't exist fails. A file with no "
                    "description can still be read directly once you know its path, "
                    "e.g. from a folder listing.")
    alternate_branch: str | None = Field(
        default=None,
        description="Read this branch instead of the default one: the branch from a coding task's result "
                    "(get_coding_task), copied exactly. Omit to read the default branch. The whole-repo listing "
                    "(no path) only shows files with a known description, so a file a coding task added isn't in "
                    "it: list its folder instead.")


def _list_branches(owner: str, repo: str, access_token: str | None) -> list[str]:
    names: list[str] = []
    page = 1
    while True:
        # one page of branches
        resp = requests.get(f"https://api.github.com/repos/{owner}/{repo}/branches", params={"per_page": 100, "page": page},
                            headers=_github_headers(access_token), timeout=15)
        if not resp.ok:
            raise ToolError(f"GitHub Branches API failed: {resp.text}")
        batch = [branch["name"] for branch in resp.json()]
        names += batch
        # GitHub pages at 100; a short page is the last one
        if len(batch) < 100:
            return names
        page += 1


def _check_branch_exists(owner: str, repo: str, alternate_branch: str, access_token: str | None) -> None:
    # the single-branch endpoint, which returns 404 when the branch doesn't exist
    resp = requests.get(f"https://api.github.com/repos/{owner}/{repo}/branches/{alternate_branch}",
                        headers=_github_headers(access_token), timeout=15)
    # only a 404 means no such branch; any other failure is left for the read's own error
    if resp.status_code == 404:
        raise ToolError(f"Branch '{alternate_branch}' not found. Existing branches: {_list_branches(owner, repo, access_token)}")


def _fetch_repo_contents(
    args: GetRepoContentsArgs,
    owner: str | None, repo: str | None,
    branch: str | None, access_token: str | None,
    file_descriptions: dict[str, str],
) -> dict:
    """Fetches a file's content, or lists a folder or the whole repo, from GitHub."""
    # check the alternate branch first, so a wrong name gets the list of branches instead of a bare 404
    if args.alternate_branch:
        _check_branch_exists(owner, repo, args.alternate_branch, access_token)
    # the branch this read comes from
    read_branch = args.alternate_branch or branch
    # no path: list the whole repo
    if not args.path:
        paths = _github_tree(owner, repo, read_branch, access_token)
        if file_descriptions:
            # Narrows a big repo to a useful starting point instead of every
            # file -- a deployer with no descriptions yet gets everything,
            # unfiltered, rather than an empty list.
            paths = [p for p in paths if p in file_descriptions]
        return {"type": "files", "entries": [_describe(p, file_descriptions) for p in paths]}

    # a folder's entries, or a file's metadata with its inline content
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{args.path}",
        params={"ref": read_branch},
        headers=_github_headers(access_token),
    )
    if not resp.ok:
        raise ToolError(f"GitHub Contents API failed for '{args.path}': {resp.text}")
    # the Contents API returns a list for a folder and an object for a file
    data = resp.json()
    if isinstance(data, list):
        return {"type": "files", "entries": [_describe(e["path"], file_descriptions) for e in data]}

    try:
        if args.path.endswith(".ipynb"):
            # fetch the notebook from download_url (its inline content is empty over 1MB), then strip its outputs
            # the file's raw bytes, for a notebook too big for the inline content
            raw = requests.get(data["download_url"], headers=_github_headers(access_token))
            if not raw.ok:
                raise ToolError(f"Failed to fetch '{args.path}' raw content: {raw.text}")
            content = raw.content.decode("utf-8")

            try:
                content = _strip_notebook_outputs(content)
            except (ValueError, nbformat.ValidationError):
                raise ToolError(f"'{args.path}' isn't valid notebook JSON -- the fetch may be incomplete.")

        elif data.get("content"):
            # any other file: the inline base64 content, empty over 1MB
            content = base64.b64decode(data["content"]).decode("utf-8")
        else:
            raise ToolError(
                f"'{args.path}' is {data.get('size', 0):,} bytes, over GitHub's 1MB "
                "inline-content limit -- too large for this tool to return.")

    except UnicodeDecodeError:
        raise ToolError(f"'{args.path}' isn't a text file -- this tool only supports text/code files.")

    # the limit applies to the text after notebook outputs are stripped
    if len(content) > MAX_REPO_FILE_CONTENT_CHARS:
        raise ToolError(
            f"'{args.path}' is {len(content):,} characters after processing, over the "
            f"{MAX_REPO_FILE_CONTENT_CHARS:,}-character limit -- too large for this tool to return.")
    return {"type": "file", "path": args.path, "content": content}


@mcp.tool(exclude_args=["owner", "repo", "branch", "access_token", "file_descriptions"])
async def get_repo_contents(
    args: GetRepoContentsArgs,
    owner: str | None = None, repo: str | None = None,
    branch: str | None = None, access_token: str | None = None,
    file_descriptions: dict[str, str] | None = None,
) -> dict:
    """Reads or lists repo contents at path. Always returns a dict -- check
    "type" ("files" or "file") to see which you got back. The whole-repo
    listing (no path) only includes files with a known description, to stay
    a useful starting point instead of every file in the repo; a folder
    listing includes every file in that folder, with a description where
    one is known. The authoritative source for code -- not a search_docs
    snippet, which may be summarized or outdated.
    """
    return await asyncio.to_thread(
        _fetch_repo_contents, args, owner, repo, branch, access_token, file_descriptions or {})

get_repo_contents.handle_validation_error = lambda e: str(e)