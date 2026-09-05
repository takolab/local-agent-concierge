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
  It is now *verified* rather than trusted: wrong commit, dirty tree, or a
  git-ignored file layered on top, and the run stops before the reviewer
  starts. Ignored files count because "ignored by git" and "invisible to the
  reviewer" are different claims, and this repository ignores credentials.

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


def run_git(
    argv: list[str], *, cwd: str | None, timeout: float, strip: bool = True
) -> str:
    """Run one git command with no shell and return its stdout.

    ``strip`` is on by default because almost every caller wants one line
    without its newline. It must be turned **off** for ``-z`` output: a
    ``git status --porcelain`` record begins with a two-character status
    field whose first character is a space for an unstaged change, and
    stripping it shifts every path by one character.
    """
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
    stdout = completed.stdout or ""
    return stdout.strip() if strip else stdout


def verify_checkout(
    path: str,
    head_sha: str,
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    role: str = "reviewer",
) -> None:
    """Raise unless ``path`` is a clean checkout of exactly ``head_sha``.

    Every condition is checked positively. "Not obviously wrong" is not the
    same claim as "is the review target", and only the second one permits an
    agent to start.

    ``role`` names the actor in the failure message and changes nothing else.
    The routing slice reuses these rules verbatim for a Coding Agent's
    *starting* tree: a writable workspace that already holds someone else's
    edits is not a workspace whose final state means anything. What that
    agent is allowed to leave behind is a separate question, answered in
    :mod:`review_loop.agent_workspace`.
    """
    if len(head_sha) != _FULL_SHA_LENGTH:
        raise WorkspaceError(
            f"the review target {head_sha!r} is not a 40-character SHA, so a "
            "workspace cannot be bound to it"
        )
    if not os.path.isdir(path):
        raise WorkspaceError(f"the {role} working directory {path!r} is not a directory")

    # Outside a repository git *fails* rather than answering "false", so both
    # shapes are the same finding and deserve the same message.
    try:
        inside = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=timeout)
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"the {role} working directory {path!r} is not a git work tree: {exc}"
        ) from exc
    if inside != "true":
        raise WorkspaceError(f"the {role} working directory {path!r} is not a git work tree")

    checked_out = run_git(["rev-parse", "HEAD"], cwd=path, timeout=timeout)
    if checked_out != head_sha:
        raise WorkspaceError(
            f"the {role} working directory {path!r} is at {checked_out}, not the "
            f"review target {head_sha}; work done in it would describe another commit"
        )

    # A clean tree matters as much as the right commit. `rev-parse HEAD` says
    # what was checked out, not what the reviewer would read: uncommitted
    # edits on top of the right SHA are still code that is not in the pull
    # request, and untracked files are still files a reviewer may open.
    dirty = run_git(["status", "--porcelain", "--untracked-files=all"], cwd=path, timeout=timeout)
    if dirty:
        changed = len(dirty.splitlines())
        raise WorkspaceError(
            f"the {role} working directory {path!r} is at the review target "
            f"{head_sha} but has {changed} uncommitted or untracked path(s); the "
            f"{role} would read code that is not in this pull request"
        )

    # `git status` says nothing about ignored files, and "ignored by git" does
    # not mean "invisible to the reviewer": an ignored file is as readable as
    # any other. It is a separate question from the one above, so it gets its
    # own command and its own message -- and the remedy differs, because the
    # answer to a stray `.env` is never "commit it".
    #
    # This repository's own .gitignore covers `.env`, `credentials.json`,
    # `token.json`, `*.pem` and `*.key`, so on the override path the files
    # this catches are exactly the ones that must not reach a reviewer. A
    # prepared worktree is unaffected: it is a fresh checkout with nothing
    # layered on top.
    ignored = run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard"],
        cwd=path,
        timeout=timeout,
    )
    if ignored:
        paths = ignored.splitlines()
        shown = ", ".join(paths[:3]) + (", ..." if len(paths) > 3 else "")
        raise WorkspaceError(
            f"the {role} working directory {path!r} is at the review target "
            f"{head_sha} but contains {len(paths)} git-ignored path(s) ({shown}); "
            f"they are not in this pull request and the {role} can still read "
            "them, and this repository ignores credential files"
        )


class ExistingWorkspace:
    """A directory the operator chose, verified against the target."""

    def __init__(
        self,
        path: str,
        *,
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
        role: str = "reviewer",
    ) -> None:
        self.path = path
        self.role = role
        self._timeout = timeout

    @contextmanager
    def open(self, head_sha: str) -> Iterator[str]:
        verify_checkout(self.path, head_sha, timeout=self._timeout, role=self.role)
        yield self.path

    def describe(self) -> str:
        # Future tense on purpose. This label is rendered before the check
        # runs, so live experiment #2 saw "(verified against the target)"
        # printed a few lines above REVIEWER_WORKSPACE_INVALID -- a
        # past-tense guarantee that did not hold, in the text an operator
        # reads while diagnosing exactly that failure.
        return f"{self.path} (verified against the target before use)"


class PreparedWorkspace:
    """A detached worktree at the target commit, created and then removed."""

    def __init__(
        self,
        repo_root: str,
        number: int,
        *,
        remote: str = DEFAULT_REMOTE,
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
        role: str = "reviewer",
    ) -> None:
        self.repo_root = repo_root
        self.number = number
        self.remote = remote
        self.role = role
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
        run_git(["fetch", "--quiet", self.remote, ref], cwd=self.repo_root, timeout=self._timeout)
        fetched = run_git(["rev-parse", "FETCH_HEAD"], cwd=self.repo_root, timeout=self._timeout)
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
            run_git(
                ["worktree", "add", "--detach", "--quiet", worktree, head_sha],
                cwd=self.repo_root,
                timeout=self._timeout,
            )
            # Verified rather than assumed. `worktree add` succeeding is not
            # the same fact as "this directory is that commit, and clean".
            verify_checkout(
                worktree, head_sha, timeout=self._timeout, role=self.role
            )
            yield worktree
        finally:
            self._remove(worktree, parent)

    def _remove(self, worktree: str, parent: str) -> None:
        """Best-effort cleanup: never mask the reason the review ended."""
        try:
            run_git(
                ["worktree", "remove", "--force", worktree],
                cwd=self.repo_root,
                timeout=self._timeout,
            )
        except WorkspaceError:
            pass
        shutil.rmtree(parent, ignore_errors=True)
        try:
            run_git(["worktree", "prune"], cwd=self.repo_root, timeout=self._timeout)
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
