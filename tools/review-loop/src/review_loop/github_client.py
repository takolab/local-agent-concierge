"""Read-only GitHub access for the review-loop runner.

Every request goes through :meth:`GitHubClient._get`, which shells out to the
already-authenticated ``gh`` CLI with an explicit ``--method GET``. There is no
code path in this module that can create a comment, a review, a label, a
dispatch, a re-run, or a merge: no caller supplies an HTTP method, and the
single ``gh`` invocation site hard-codes ``GET``.

Using ``gh`` rather than a raw HTTP client is deliberate: it reuses the
developer's existing ``gh auth login`` session, so the runner introduces no new
long-lived credential and stores no token in the repository.
"""

from __future__ import annotations

import base64
import binascii
import json
import shutil
import subprocess
from typing import Any

from .model import require_full_sha

#: The only HTTP method this client is permitted to issue.
ALLOWED_HTTP_METHOD = "GET"

_WORKFLOW_DIR = ".github/workflows"


class GitHubApiError(RuntimeError):
    """Any failure to obtain a trustworthy answer from the GitHub API."""


class GitHubClient:
    """Read-only ``gh api`` wrapper scoped to one repository."""

    def __init__(self, repo: str, *, timeout: float = 60.0) -> None:
        if "/" not in repo:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self._timeout = timeout

    # -- transport ---------------------------------------------------------

    def _get(self, path: str) -> Any:
        """Issue one authenticated read-only GET and parse the JSON body."""
        if shutil.which("gh") is None:
            raise GitHubApiError(
                "the 'gh' CLI is required for GitHub access but was not found on PATH"
            )
        argv = ["gh", "api", "--method", ALLOWED_HTTP_METHOD, path]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitHubApiError(f"gh api {path} timed out after {self._timeout}s") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip() or "(no stderr)"
            raise GitHubApiError(f"gh api {path} failed (exit {completed.returncode}): {stderr}")

        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubApiError(f"gh api {path} returned a non-JSON body") from exc

    # -- endpoints ---------------------------------------------------------

    def get_pull_request(self, number: int) -> dict[str, Any]:
        payload = self._get(f"repos/{self.repo}/pulls/{int(number)}")
        if not isinstance(payload, dict):
            raise GitHubApiError(f"unexpected pull request payload for #{number}")
        return payload

    def list_workflow_runs_for_sha(self, head_sha: str) -> list[dict[str, Any]]:
        """List Actions runs for an exact commit.

        The full SHA is enforced here rather than trusted from the caller: the
        Actions API answers an abbreviated ``head_sha`` with ``total_count: 0``
        and HTTP 200, which would otherwise be read as "this commit has no CI".
        """
        sha = require_full_sha(head_sha, label="head_sha query parameter")
        payload = self._get(
            f"repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100&exclude_pull_requests=true"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise GitHubApiError(f"unexpected workflow runs payload for {sha}")
        return payload["workflow_runs"]

    def list_workflow_files(self, ref: str) -> dict[str, str]:
        """Return ``{path: yaml text}`` for ``.github/workflows`` at ``ref``.

        The configuration is read at the exact commit under review, because
        that is the configuration GitHub itself uses to decide which workflows
        a ``pull_request`` event starts.
        """
        listing = self._get(f"repos/{self.repo}/contents/{_WORKFLOW_DIR}?ref={ref}")
        if not isinstance(listing, list):
            raise GitHubApiError(f"unexpected workflow directory listing at {ref}")

        files: dict[str, str] = {}
        for entry in listing:
            path = entry.get("path")
            if entry.get("type") != "file" or not isinstance(path, str):
                continue
            if not path.endswith((".yml", ".yaml")):
                continue
            files[path] = self._get_file_text(path, ref)
        return files

    def _get_file_text(self, path: str, ref: str) -> str:
        payload = self._get(f"repos/{self.repo}/contents/{path}?ref={ref}")
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise GitHubApiError(f"unexpected file payload for {path} at {ref}")
        try:
            return base64.b64decode(payload.get("content", "")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise GitHubApiError(f"could not decode {path} at {ref}") from exc


def detect_repository(timeout: float = 30.0) -> str:
    """Resolve ``owner/name`` from the current directory's git remote."""
    if shutil.which("gh") is None:
        raise GitHubApiError("the 'gh' CLI is required to detect the repository")
    completed = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "(no stderr)"
        raise GitHubApiError(f"could not detect the repository: {stderr}")
    repo = completed.stdout.strip()
    if "/" not in repo:
        raise GitHubApiError(f"could not detect the repository, got {repo!r}")
    return repo
