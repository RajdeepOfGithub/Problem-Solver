"""
actions/github_actions.py
Vega — Phase 4: GitHub Action Layer

⚠️  SAFETY CONTRACT — READ BEFORE CALLING ANY WRITE FUNCTION ⚠️
All functions that mutate GitHub state (create_issue, create_draft_pr,
create_review) MUST only be invoked AFTER the safety confirmation gate
has approved the action via POST /action/confirm. The API server enforces
this; callers in this module assume confirmation has already been received.
This is non-negotiable per the project's human-in-the-loop design principle.

Read-only functions (get_repo_info, get_pr_diff, get_file_content) may be
called freely by agents without a confirmation gate.

Primary SDK: requests (GitHub REST API v3).
Credentials come exclusively from environment variables via python-dotenv.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid
from typing import Optional

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API_BASE: str = "https://api.github.com"

_VALID_VERDICTS = {"APPROVE", "REQUEST_CHANGES", "COMMENT"}


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class GitHubActionError(Exception):
    """Raised when a GitHub REST API call fails in this module."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers(accept: str = "application/vnd.github.v3+json") -> dict:
    """Build the Authorization + Accept headers for every request."""
    token = os.getenv("GITHUB_TOKEN", "")
    return {
        "Authorization": f"token {token}",
        "Accept": accept,
    }


def _raise_for_status(response: requests.Response, context: str) -> None:
    """
    Raise GitHubActionError for any non-2xx response.

    Args:
        response: The requests.Response object.
        context:  Short description of the call (included in the error message).

    Raises:
        GitHubActionError: Always raised when response.status_code >= 300.
    """
    if response.status_code >= 300:
        try:
            body = response.json()
        except Exception:
            body = response.text
        raise GitHubActionError(
            f"{context} failed — HTTP {response.status_code}: {body}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_github_connection() -> bool:
    """
    Verify that the configured GITHUB_TOKEN is valid.

    Calls GET /user and checks for a 2xx response. Never raises; returns
    False on any error (missing token, network failure, invalid credentials).

    Returns:
        True if the token is valid and GitHub is reachable, False otherwise.
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        logger.warning("check_github_connection: GITHUB_TOKEN is not set.")
        return False
    try:
        response = requests.get(f"{GITHUB_API_BASE}/user", headers=_headers(), timeout=10)
        ok = response.status_code == 200
        if ok:
            login = response.json().get("login", "<unknown>")
            logger.debug("GitHub connection OK — authenticated as %r", login)
        else:
            logger.warning(
                "GitHub connection check failed — HTTP %d", response.status_code
            )
        return ok
    except Exception as exc:
        logger.warning("GitHub connection check raised: %s", exc)
        return False


def get_repo_info(owner: str, repo: str) -> dict:
    """
    Fetch basic metadata for a GitHub repository.

    Args:
        owner: Repository owner (user or org).
        repo:  Repository name.

    Returns:
        Dict with keys:
            - name           (str)
            - full_name      (str, e.g. 'owner/repo')
            - default_branch (str)
            - private        (bool)

    Raises:
        GitHubActionError: On 404 (repo not found), 401/403 (auth failure),
                           or any other non-2xx response.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
    logger.info("get_repo_info: GET %s", url)

    try:
        response = requests.get(url, headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        raise GitHubActionError(f"get_repo_info network error: {exc}") from exc

    _raise_for_status(response, f"GET /repos/{owner}/{repo}")
    data = response.json()

    return {
        "name":           data["name"],
        "full_name":      data["full_name"],
        "default_branch": data["default_branch"],
        "private":        data["private"],
    }


def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> dict:
    """
    Create a new GitHub issue.

    ⚠️ DESTRUCTIVE — only call after safety gate confirmation.
    This function mutates repository state on GitHub. The API server
    must have received POST /action/confirm before this is invoked.

    Args:
        owner:  Repository owner.
        repo:   Repository name.
        title:  Issue title.
        body:   Issue body (Markdown).
        labels: Optional list of label strings to apply.

    Returns:
        Dict with keys:
            - url          (str, HTML URL of the created issue)
            - issue_number (int)

    Raises:
        GitHubActionError: On any non-2xx response from GitHub.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues"
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels

    logger.info(
        "create_issue: POST %s title=%r labels=%r", url, title, labels
    )

    try:
        response = requests.post(url, headers=_headers(), json=payload, timeout=15)
    except requests.RequestException as exc:
        raise GitHubActionError(f"create_issue network error: {exc}") from exc

    _raise_for_status(response, f"POST /repos/{owner}/{repo}/issues")
    data = response.json()

    result = {"url": data["html_url"], "issue_number": data["number"]}
    logger.info("Issue created: #%d — %s", result["issue_number"], result["url"])
    return result


def create_draft_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
) -> dict:
    """
    Create a draft pull request.

    ⚠️ DESTRUCTIVE — only call after safety gate confirmation.
    This function mutates repository state on GitHub. The API server
    must have received POST /action/confirm before this is invoked.

    Args:
        owner: Repository owner.
        repo:  Repository name.
        title: PR title.
        body:  PR description (Markdown).
        head:  Branch containing the changes (e.g. 'feature/fix-auth').
        base:  Target branch to merge into. Defaults to 'main'.

    Returns:
        Dict with keys:
            - url       (str, HTML URL of the created PR)
            - pr_number (int)

    Raises:
        GitHubActionError: On any non-2xx response from GitHub.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    payload = {
        "title": title,
        "body":  body,
        "head":  head,
        "base":  base,
        "draft": True,
    }

    logger.info(
        "create_draft_pr: POST %s head=%r → base=%r", url, head, base
    )

    try:
        response = requests.post(url, headers=_headers(), json=payload, timeout=15)
    except requests.RequestException as exc:
        raise GitHubActionError(f"create_draft_pr network error: {exc}") from exc

    _raise_for_status(response, f"POST /repos/{owner}/{repo}/pulls")
    data = response.json()

    result = {"url": data["html_url"], "pr_number": data["number"]}
    logger.info("Draft PR created: #%d — %s", result["pr_number"], result["url"])
    return result


def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """
    Fetch the unified diff for a pull request.

    Uses the 'application/vnd.github.v3.diff' Accept header to retrieve
    the raw diff directly from GitHub.

    Args:
        owner:     Repository owner.
        repo:      Repository name.
        pr_number: Pull request number.

    Returns:
        Unified diff as a plain string.

    Raises:
        GitHubActionError: On any non-2xx response from GitHub.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    logger.info("get_pr_diff: GET %s (diff)", url)

    try:
        response = requests.get(
            url,
            headers=_headers(accept="application/vnd.github.v3.diff"),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise GitHubActionError(f"get_pr_diff network error: {exc}") from exc

    _raise_for_status(response, f"GET /repos/{owner}/{repo}/pulls/{pr_number} (diff)")
    return response.text


def create_review(
    owner: str,
    repo: str,
    pr_number: int,
    verdict: str,
    comments: list[dict] | None = None,
) -> dict:
    """
    Submit a review on a pull request.

    ⚠️ DESTRUCTIVE — only call after safety gate confirmation.
    This function mutates repository state on GitHub. The API server
    must have received POST /action/confirm before this is invoked.

    Args:
        owner:      Repository owner.
        repo:       Repository name.
        pr_number:  Pull request number.
        verdict:    Review event — must be one of 'APPROVE', 'REQUEST_CHANGES',
                    or 'COMMENT'. Raises ValueError if invalid.
        comments:   Optional list of inline comment dicts, each with keys:
                        - path (str)  — file path relative to repo root
                        - line (int)  — line number in the diff
                        - body (str)  — comment text

    Returns:
        Dict with keys:
            - review_id (int)
            - state     (str, mirrors the verdict sent)
            - url       (str, HTML URL of the review)

    Raises:
        ValueError:         If verdict is not one of the three valid values.
        GitHubActionError:  On any non-2xx response from GitHub.
    """
    verdict_upper = verdict.upper()
    if verdict_upper not in _VALID_VERDICTS:
        raise ValueError(
            f"Invalid verdict {verdict!r}. Must be one of: {sorted(_VALID_VERDICTS)}"
        )

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload: dict = {"event": verdict_upper}
    if comments:
        payload["comments"] = [
            {"path": c["path"], "line": c["line"], "body": c["body"]}
            for c in comments
        ]

    logger.info(
        "create_review: POST %s verdict=%r comments=%d",
        url, verdict_upper, len(comments or []),
    )

    try:
        response = requests.post(url, headers=_headers(), json=payload, timeout=15)
    except requests.RequestException as exc:
        raise GitHubActionError(f"create_review network error: {exc}") from exc

    _raise_for_status(
        response, f"POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    )
    data = response.json()

    result = {
        "review_id": data["id"],
        "state":     data["state"],
        "url":       data["html_url"],
    }
    logger.info(
        "Review submitted: id=%d state=%s — %s",
        result["review_id"], result["state"], result["url"],
    )
    return result


def create_or_update_file(
    path: str,
    content: str,
    message: str,
    branch: str = "main",
    owner: str | None = None,
    repo: str | None = None,
) -> dict:
    """
    Create or update a single file in a GitHub repository.

    ⚠️ DESTRUCTIVE — only call after safety gate confirmation.

    Uses the GitHub Contents API (PUT /repos/{owner}/{repo}/contents/{path}).
    If the file already exists, its SHA is fetched first for the update.

    Args:
        path:    File path relative to repo root.
        content: Raw file content (will be base64-encoded).
        message: Commit message.
        branch:  Target branch. Defaults to 'main'.
        owner:   Repository owner. Falls back to GITHUB_OWNER env var.
        repo:    Repository name. Falls back to GITHUB_REPO env var.

    Returns:
        Dict with keys:
            - url     (str, HTML URL of the file)
            - sha     (str, blob SHA of the created/updated file)
            - path    (str)

    Raises:
        GitHubActionError: On any non-2xx response from GitHub.
    """
    owner = owner or os.getenv("GITHUB_OWNER", "")
    repo = repo or os.getenv("GITHUB_REPO", "")

    if not owner or not repo:
        raise GitHubActionError(
            "create_or_update_file: owner and repo are required "
            "(pass as args or set GITHUB_OWNER/GITHUB_REPO env vars)"
        )

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    logger.info("create_or_update_file: PUT %s branch=%r", url, branch)

    # Check if file already exists to get its SHA
    existing_sha = None
    try:
        resp = requests.get(url, headers=_headers(), params={"ref": branch}, timeout=10)
        if resp.status_code == 200:
            existing_sha = resp.json().get("sha")
    except Exception:
        pass  # File doesn't exist yet — that's fine

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: dict = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    try:
        response = requests.put(url, headers=_headers(), json=payload, timeout=15)
    except requests.RequestException as exc:
        raise GitHubActionError(f"create_or_update_file network error: {exc}") from exc

    _raise_for_status(response, f"PUT /repos/{owner}/{repo}/contents/{path}")
    data = response.json()

    result = {
        "url": data.get("content", {}).get("html_url", ""),
        "sha": data.get("content", {}).get("sha", ""),
        "path": path,
    }
    logger.info("File created/updated: %s — %s", path, result["url"])
    return result


def create_draft_pr_with_diff(
    title: str,
    body: str,
    diff: str,
    owner: str | None = None,
    repo: str | None = None,
    base: str = "main",
) -> dict:
    """
    Create a draft PR by applying a unified diff on a new branch.

    ⚠️ DESTRUCTIVE — only call after safety gate confirmation.

    Creates a new branch from base, applies the diff by updating each
    affected file, then opens a draft PR.

    Args:
        title: PR title.
        body:  PR description (Markdown).
        diff:  Unified diff string.
        owner: Repository owner. Falls back to GITHUB_OWNER env var.
        repo:  Repository name. Falls back to GITHUB_REPO env var.
        base:  Base branch. Defaults to 'main'.

    Returns:
        Dict with keys:
            - url       (str, HTML URL of the created PR)
            - pr_number (int)
            - branch    (str, the created branch name)

    Raises:
        GitHubActionError: On any non-2xx response from GitHub.
    """
    import hashlib
    import time

    owner = owner or os.getenv("GITHUB_OWNER", "")
    repo = repo or os.getenv("GITHUB_REPO", "")

    if not owner or not repo:
        raise GitHubActionError(
            "create_draft_pr_with_diff: owner and repo are required "
            "(pass as args or set GITHUB_OWNER/GITHUB_REPO env vars)"
        )

    # Create a unique branch name
    branch_suffix = uuid.uuid4().hex[:8]
    branch_name = f"vega/fix-{branch_suffix}"

    # Get the SHA of the base branch
    ref_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{base}"
    try:
        resp = requests.get(ref_url, headers=_headers(), timeout=10)
    except requests.RequestException as exc:
        raise GitHubActionError(f"create_draft_pr_with_diff: failed to get base ref: {exc}") from exc

    _raise_for_status(resp, f"GET base branch ref {base}")
    base_sha = resp.json()["object"]["sha"]

    # Create the new branch
    create_ref_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs"
    try:
        resp = requests.post(
            create_ref_url,
            headers=_headers(),
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise GitHubActionError(f"create_draft_pr_with_diff: failed to create branch: {exc}") from exc

    _raise_for_status(resp, f"Create branch {branch_name}")
    logger.info("Created branch %s from %s (sha=%s)", branch_name, base, base_sha[:8])

    # Parse diff to find modified files and their new content
    # For each file in the diff, update it on the new branch
    current_file = None
    file_lines: dict[str, list[str]] = {}

    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
            if current_file not in file_lines:
                file_lines[current_file] = []
        elif current_file and line.startswith("+") and not line.startswith("+++"):
            file_lines[current_file].append(line[1:])

    for file_path, lines in file_lines.items():
        content = "\n".join(lines)
        create_or_update_file(
            path=file_path,
            content=content,
            message=f"{title} — update {file_path}",
            branch=branch_name,
            owner=owner,
            repo=repo,
        )

    # Create the draft PR
    result = create_draft_pr(
        owner=owner,
        repo=repo,
        title=title,
        body=body,
        head=branch_name,
        base=base,
    )
    result["branch"] = branch_name
    return result


def get_file_content(
    owner: str,
    repo: str,
    path: str,
    ref: str = "main",
) -> str:
    """
    Fetch the decoded content of a file from a GitHub repository.

    Uses GET /repos/{owner}/{repo}/contents/{path} and base64-decodes
    the response. Used by agents to read specific source files without
    a local clone.

    Args:
        owner: Repository owner.
        repo:  Repository name.
        path:  File path relative to the repository root (e.g. 'src/main.py').
        ref:   Branch, tag, or commit SHA. Defaults to 'main'.

    Returns:
        Decoded file content as a plain UTF-8 string.

    Raises:
        GitHubActionError: On 404 (file or ref not found), auth failure,
                           or any other non-2xx response.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
    logger.info("get_file_content: GET %s ref=%r", url, ref)

    try:
        response = requests.get(
            url, headers=_headers(), params={"ref": ref}, timeout=15
        )
    except requests.RequestException as exc:
        raise GitHubActionError(f"get_file_content network error: {exc}") from exc

    _raise_for_status(response, f"GET /repos/{owner}/{repo}/contents/{path}")
    data = response.json()

    if data.get("encoding") != "base64":
        raise GitHubActionError(
            f"Unexpected encoding {data.get('encoding')!r} for {path!r} — expected 'base64'."
        )

    raw = base64.b64decode(data["content"]).decode("utf-8")
    logger.debug(
        "get_file_content: decoded %d bytes from %r (ref=%r)", len(raw), path, ref
    )
    return raw


# ---------------------------------------------------------------------------
# Smoke test (dev only — not pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("github_actions.py smoke test")
    print("=" * 60)

    # 1. Connection check
    connected = check_github_connection()
    print(f"\n[1] check_github_connection() → {connected}")

    # 2. Repo info (opt-in via env vars)
    test_owner = os.getenv("TEST_OWNER")
    test_repo  = os.getenv("TEST_REPO")

    if connected:
        if test_owner and test_repo:
            try:
                info = get_repo_info(test_owner, test_repo)
                print(f"\n[2] get_repo_info({test_owner!r}, {test_repo!r}) →")
                for k, v in info.items():
                    print(f"    {k}: {v}")
            except GitHubActionError as exc:
                print(f"\n[2] get_repo_info raised GitHubActionError: {exc}", file=sys.stderr)
        else:
            print(
                "\n[2] Skipped get_repo_info — set TEST_OWNER and TEST_REPO env vars to enable."
            )
    else:
        print("\n[2] Skipped get_repo_info — connection check failed.")

    print("\ngithub_actions.py smoke test passed")
