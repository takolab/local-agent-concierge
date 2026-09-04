"""The only place this runner reads or writes pull request comments.

PR #28's client is read-only by construction and stays that way: it names no
comment endpoint and issues no method but ``GET``. The single write this slice
introduces -- creating one Independent AI Review comment -- lives here instead,
in a class whose entire public surface is one method.

The boundary covers this package's own writes. It says nothing about the
reviewer subprocess, which runs with the invoking user's permissions -- see
:mod:`review_loop.reviewer_process`.

Within that scope the boundary is structural, not a convention.
:class:`IssueCommentReader`
hard-codes ``GET``; :class:`IssueCommentWriter` hard-codes ``POST`` and the
``issues/{number}/comments`` path, so there is no code path from any caller to
an edit, a delete, a review object, a label, a merge, a dispatch or a re-run.
The comment body travels as JSON on stdin, never as a command-line argument,
so reviewer text cannot become part of the command.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from .github_client import GitHubApiError

_READ_METHOD = "GET"
_WRITE_METHOD = "POST"


@dataclass(frozen=True)
class ExistingComment:
    """One comment already on the pull request."""

    comment_id: int
    author: str
    body: str


def _run_gh(argv: list[str], *, stdin: str | None, timeout: float) -> Any:
    if shutil.which("gh") is None:
        raise GitHubApiError(
            "the 'gh' CLI is required for GitHub access but was not found on PATH"
        )
    try:
        completed = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubApiError(f"{' '.join(argv[:3])} timed out after {timeout}s") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "(no stderr)"
        raise GitHubApiError(
            f"gh api failed (exit {completed.returncode}): {stderr}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubApiError("gh api returned a non-JSON body") from exc


def resolve_comment_author(timeout: float = 60.0) -> str:
    """Return the login this invocation would create comments as.

    The duplicate check needs it. A marker is a public, deterministic string,
    so on its own it proves only that *someone* wrote one; combined with the
    author it distinguishes a record this automation wrote from a marker
    anyone else copied into a comment.
    """
    payload = _run_gh(
        ["gh", "api", "--method", _READ_METHOD, "user"], stdin=None, timeout=timeout
    )
    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not login:
        raise GitHubApiError(
            "could not determine which account this runner would comment as"
        )
    return login


class IssueCommentReader:
    """Read a pull request's comments. Issues ``GET`` and nothing else."""

    def __init__(self, repo: str, *, timeout: float = 60.0) -> None:
        if "/" not in repo:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self._timeout = timeout

    def list_comments(self, number: int) -> tuple[ExistingComment, ...]:
        """Return every comment on the pull request, oldest first."""
        comments: list[ExistingComment] = []
        page = 1
        while True:
            payload = _run_gh(
                [
                    "gh",
                    "api",
                    "--method",
                    _READ_METHOD,
                    f"repos/{self.repo}/issues/{int(number)}/comments"
                    f"?per_page=100&page={page}",
                ],
                stdin=None,
                timeout=self._timeout,
            )
            if not isinstance(payload, list):
                raise GitHubApiError(
                    f"unexpected comment listing for pull request #{number}"
                )
            for entry in payload:
                comments.append(
                    ExistingComment(
                        comment_id=int(entry.get("id", 0)),
                        author=str((entry.get("user") or {}).get("login", "")),
                        body=str(entry.get("body") or ""),
                    )
                )
            if len(payload) < 100:
                return tuple(comments)
            page += 1
            if page > 30:
                raise GitHubApiError(
                    f"pull request #{number} has more comments than can be listed"
                )


class IssueCommentWriter:
    """Create one pull request comment. The only write in this codebase."""

    def __init__(self, repo: str, *, timeout: float = 60.0) -> None:
        if "/" not in repo:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        self.repo = repo
        self._timeout = timeout

    def create_comment(self, number: int, body: str) -> int:
        """Post ``body`` as a new comment and return the new comment's id."""
        payload = _run_gh(
            [
                "gh",
                "api",
                "--method",
                _WRITE_METHOD,
                f"repos/{self.repo}/issues/{int(number)}/comments",
                "--input",
                "-",
            ],
            stdin=json.dumps({"body": body}),
            timeout=self._timeout,
        )
        comment_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(comment_id, int):
            raise GitHubApiError(
                "the comment was accepted but GitHub returned no comment id, so "
                "whether it was created cannot be confirmed"
            )
        return comment_id
