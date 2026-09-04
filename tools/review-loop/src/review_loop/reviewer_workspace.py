"""Where the reviewer runs, bound to the commit it is supposed to be reading.

PR #29 bound the *verdict* to an exact commit: the prompt names the target
SHA, and a verdict echoing anything else is refused. It never bound the
reviewer's **filesystem**. ``--reviewer-cwd`` defaulted to the current
directory, which is normally the operator's checkout -- another branch, or
the right branch with uncommitted edits on top.

That gap is not theoretical, and it is not caught by anything downstream. A
reviewer pointed at a stale tree reads the wrong code, then echoes back the
SHA it was *told* to review, because that SHA comes from the prompt rather
than from what it read. The verdict passes SHA binding, passes validation,
passes revalidation, and is recorded as evidence about a commit nobody read.
The first live experiment
(``docs/delegated-development/review-loop-live-experiment-1.md``) had to
create a detached worktree by hand to avoid exactly this, and named the
missing check as the one thing to fix before findings are routed anywhere.

So the invariant this module enforces is: **the reviewer's working directory
is a clean checkout of the exact target commit, or no reviewer runs.**

Two ways to satisfy it, both ending in the same verification:

* :class:`PreparedWorkspace` -- the default. Fetch the pull request's head
  ref, create a detached worktree at the target SHA, run the reviewer there,
  and remove it afterwards. The operator supplies nothing, and there is
  nothing for them to get wrong.
* :class:`ExistingWorkspace` -- ``--reviewer-cwd``, for a workspace the
  operator controls deliberately (a pre-warmed checkout, a container mount).
  It is now *verified* rather than trusted: wrong commit, or a dirty tree,
  and the run stops before the reviewer starts.

**This is still not a sandbox**, and enforcing this invariant does not make
it one. The reviewer remains an ordinary child process with the invoking
user's permissions, as :mod:`review_loop.reviewer_process` describes. What
changed is narrower and worth stating exactly: the tree it is pointed at is
now known to be the commit under review. Nothing stops a reviewer from
reading somewhere else entirely.

This module is the first place the package writes to the local filesystem.
The writes are bounded to ``git worktree add`` and ``git worktree remove``
on a directory this module created, plus the objects a ``git fetch`` brings
into the repository. It performs no GitHub write: the single comment in
:mod:`review_loop.github_comments` remains the only one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Iterator

#: Git operations here are local except the fetch, which is one small ref.
DEFAULT_GIT_TIMEOUT_SECONDS = 300.0

#: The remote a pull request's head ref is fetched from.
DEFAULT_REMOTE = "origin"

_FULL_SHA_LENGTH = 40


class WorkspaceError(Exception):
    """The reviewer's working directory is not the commit under review.

    Raised instead of returning a failed run, because this is categorically
    not "the reviewer failed": no reviewer was started, and the reason is a
    property of the workspace the runner was asked to use.
    """


def _label(argv: list[str]) -> str:
    """Name a git command by its subcommand, not by its flags."""
    words = [word for word in argv if not word.startswith("-")][:2]
    return "git " + " ".join(words or argv[:1])


def _git(argv: list[str], *, cwd: str | None, timeout: float) -> str:
    """Run one git command with no shell and return its stdout."""
    if shutil.which("git") is None:
        raise WorkspaceError(
            "the 'git' CLI is required to bind the reviewer to the review target "
            "but was not found on PATH"
        )
    try:
        completed = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"{_label(argv)} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise WorkspaceError(f"{_label(argv)} could not be run: {exc}") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "(no stderr)"
        raise WorkspaceError(f"{_label(argv)} failed: {stderr}")
    return (completed.stdout or "").strip()


def verify_checkout(path: str, head_sha: str, *, timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS) -> None:
    """Raise unless ``path`` is a clean checkout of exactly ``head_sha``.

    Every condition is checked positively. "Not obviously wrong" is not the
    same claim as "is the review target", and only the second one permits a
    reviewer to start.
    """
    if len(head_sha) != _FULL_SHA_LENGTH:
        raise WorkspaceError(
            f"the review target {head_sha!r} is not a 40-character SHA, so a "
            "workspace cannot be bound to it"
        )
    if not os.path.isdir(path):
        raise WorkspaceError(f"the reviewer working directory {path!r} is not a directory")

    # Outside a repository git *fails* rather than answering "false", so both
    # shapes are the same finding and deserve the same message.
    try:
        inside = _git(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=timeout)
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"the reviewer working directory {path!r} is not a git work tree: {exc}"
        ) from exc
    if inside != "true":
        raise WorkspaceError(f"the reviewer working directory {path!r} is not a git work tree")

    checked_out = _git(["rev-parse", "HEAD"], cwd=path, timeout=timeout)
    if checked_out != head_sha:
        raise WorkspaceError(
            f"the reviewer working directory {path!r} is at {checked_out}, not the "
            f"review target {head_sha}; a review of it would describe another commit"
        )

    # A clean tree matters as much as the right commit. `rev-parse HEAD` says
    # what was checked out, not what the reviewer would read: uncommitted
    # edits on top of the right SHA are still code that is not in the pull
    # request, and untracked files are still files a reviewer may open.
    dirty = _git(["status", "--porcelain", "--untracked-files=all"], cwd=path, timeout=timeout)
    if dirty:
        changed = len(dirty.splitlines())
        raise WorkspaceError(
            f"the reviewer working directory {path!r} is at the review target "
            f"{head_sha} but has {changed} uncommitted or untracked path(s); the "
            "reviewer would read code that is not in this pull request"
        )


class ExistingWorkspace:
    """A directory the operator chose, verified against the target."""

    def __init__(self, path: str, *, timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS) -> None:
        self.path = path
        self._timeout = timeout

    @contextmanager
    def open(self, head_sha: str) -> Iterator[str]:
        verify_checkout(self.path, head_sha, timeout=self._timeout)
        yield self.path

    def describe(self) -> str:
        return f"{self.path} (verified against the target)"


class PreparedWorkspace:
    """A detached worktree at the target commit, created and then removed."""

    def __init__(
        self,
        repo_root: str,
        number: int,
        *,
        remote: str = DEFAULT_REMOTE,
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        self.repo_root = repo_root
        self.number = number
        self.remote = remote
        self._timeout = timeout

    @contextmanager
    def open(self, head_sha: str) -> Iterator[str]:
        if len(head_sha) != _FULL_SHA_LENGTH:
            raise WorkspaceError(
                f"the review target {head_sha!r} is not a 40-character SHA, so a "
                "workspace cannot be prepared for it"
            )

        # Fetch the pull request's own head ref rather than the branch: the
        # branch may live in a fork this repository has no remote for, and
        # refs/pull/N/head is what GitHub itself resolved the head from.
        ref = f"refs/pull/{self.number}/head"
        _git(["fetch", "--quiet", self.remote, ref], cwd=self.repo_root, timeout=self._timeout)
        fetched = _git(["rev-parse", "FETCH_HEAD"], cwd=self.repo_root, timeout=self._timeout)
        if fetched != head_sha:
            # Two authorities disagree about what this pull request's head is.
            # Guessing which one is current is exactly the guess this tool
            # exists to refuse.
            raise WorkspaceError(
                f"{self.remote}/{ref} resolves to {fetched}, but the review target "
                f"is {head_sha}; the pull request moved, or the remote is not the "
                "repository under review"
            )

        parent = tempfile.mkdtemp(prefix=f"review-loop-pr{self.number}-")
        worktree = os.path.join(parent, head_sha[:12])
        try:
            _git(
                ["worktree", "add", "--detach", "--quiet", worktree, head_sha],
                cwd=self.repo_root,
                timeout=self._timeout,
            )
            # Verified rather than assumed. `worktree add` succeeding is not
            # the same fact as "this directory is that commit, and clean".
            verify_checkout(worktree, head_sha, timeout=self._timeout)
            yield worktree
        finally:
            self._remove(worktree, parent)

    def _remove(self, worktree: str, parent: str) -> None:
        """Best-effort cleanup: never mask the reason the review ended."""
        try:
            _git(
                ["worktree", "remove", "--force", worktree],
                cwd=self.repo_root,
                timeout=self._timeout,
            )
        except WorkspaceError:
            pass
        shutil.rmtree(parent, ignore_errors=True)
        try:
            _git(["worktree", "prune"], cwd=self.repo_root, timeout=self._timeout)
        except WorkspaceError:
            pass

    def describe(self) -> str:
        return f"a detached worktree at the target, from {self.repo_root}"


class WorkspaceBoundReviewer:
    """Run an inner reviewer inside a workspace bound to the target commit.

    The binding lives here rather than in :func:`review_loop.review_runner.run_review`
    because "where does the reviewer run" is a property of the reviewer, not
    of the pull request. The runner only has to hand over the exact SHA and
    let a :class:`WorkspaceError` stop the turn.
    """

    def __init__(self, reviewer, workspace) -> None:
        self._reviewer = reviewer
        self.workspace = workspace

    def invoke(self, prompt: str, *, head_sha: str):
        with self.workspace.open(head_sha) as path:
            return self._reviewer.invoke(prompt, cwd=path)

    def describe_workspace(self) -> str:
        return self.workspace.describe()
