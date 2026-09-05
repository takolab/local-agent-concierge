"""Inspect the tree a Coding Agent was given, after it has finished with it.

PR #32 bound the *reviewer's* workspace: exact target commit, clean, no
git-ignored residue, verified before the reviewer starts. A coding agent gets
the same guarantee at the start and needs a different one at the end, because
its whole purpose is to leave the tree different from how it found it.

So the boundary is asymmetric, and stated in both directions:

**Before the agent runs** the rules are PR #32's, unchanged and re-used --
:func:`review_loop.reviewer_workspace.verify_checkout`. A writable workspace
that already contains someone else's edits, or a stray ``.env``, is not a
workspace whose final state means anything.

**After the agent runs** three kinds of change are distinguished, because
they are three different facts:

* **Tracked and untracked changes** are the fix. They are what a later slice
  would commit and push, so they are compared -- as a set -- against what the
  agent said it changed, and against the routed scope. This is the check that
  makes ``Files changed`` a claim rather than a courtesy.
* **Build and test residue** -- ``__pycache__``, ``.pytest_cache``,
  ``.hypothesis``, a virtualenv -- is expected. Telling the agent to run the
  tests and then failing the run because the tests wrote a cache directory
  would make a writable agent turn impossible. It is git-ignored, so it is
  not part of the fix and cannot reach a pull request; it is reported and
  tolerated.
* **Any other git-ignored path** is neither. The tree started with zero
  ignored paths, so every one of these was produced by the agent, and this
  repository's ``.gitignore`` covers ``.env``, ``credentials.json``,
  ``token.json``, ``*.pem`` and ``*.key``. A run that ends with one of those
  in the workspace is not a bounded fix.

One more asymmetry is worth naming. **The agent must not commit.** The head
is re-read afterwards, and a moved ``HEAD`` fails the run: a commit would put
the fix somewhere ``git status`` no longer reports, which is exactly the
place a hidden change would hide. Committing, and everything after it, is the
next slice's decision to make -- with a human in it.

Nothing here is a sandbox, and the inspection does not pretend to be one. It
establishes what changed *inside the worktree*. An agent that wrote somewhere
else on the filesystem did so as the invoking user, and no amount of
inspecting this directory would show it.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

from .fix_response import MAX_PATCH_BYTES
from .reviewer_workspace import (
    DEFAULT_GIT_TIMEOUT_SECONDS,
    DEFAULT_REMOTE,
    WorkspaceError,
    run_git,
)

#: Directory names that are build or test residue wherever they appear.
RESIDUE_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".hypothesis",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        "target",
        "dist",
        "build",
    }
)

#: File names, matched against the basename.
RESIDUE_FILES = ("*.pyc", "*.pyo", "*.pyd", ".coverage", ".coverage.*", ".DS_Store")


def is_residue(path: str) -> bool:
    """Whether a git-ignored path is ordinary build or test output.

    Any segment matching means the whole path does: ``__pycache__`` is
    residue, and so is everything git lists beneath it.
    """
    segments = path.rstrip("/").split("/")
    if any(
        segment in RESIDUE_DIRECTORIES or segment.endswith(".egg-info")
        for segment in segments
    ):
        return True
    return any(fnmatch.fnmatch(segments[-1], pattern) for pattern in RESIDUE_FILES)


@dataclass(frozen=True)
class WorkspaceInspection:
    """What the working tree actually shows after the agent has run."""

    head_sha: str
    #: Tracked modifications, additions, deletions and untracked files, sorted.
    #: This is the fix, and the only set the response is held to.
    changed_paths: tuple[str, ...]
    #: Every git-ignored path present afterwards.
    ignored_paths: tuple[str, ...]
    #: Those of them that are ordinary build or test output.
    residue_paths: tuple[str, ...]
    #: Those that are not, and therefore should not be there.
    unexpected_ignored: tuple[str, ...]
    #: A unified diff of ``changed_paths``, capturing new files too.
    patch: str = ""
    #: Set when the diff was larger than a bounded fix should produce.
    patch_refused: str | None = None

    @property
    def clean(self) -> bool:
        return not self.changed_paths


def _split_nul(value: str) -> list[str]:
    return [entry for entry in value.split("\0") if entry]


def _status_paths(worktree: str, timeout: float) -> tuple[str, ...]:
    """Read ``git status`` as paths, handling renames and odd file names.

    ``-z`` is not a nicety: without it git quotes and escapes paths that
    contain spaces or non-ASCII, and a set comparison against the agent's
    reported paths would then differ for reasons that have nothing to do with
    what changed.
    """
    raw = run_git(
        ["status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=worktree,
        timeout=timeout,
        strip=False,
    )
    entries = _split_nul(raw)
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            raise WorkspaceError(
                f"git status produced an entry this runner cannot read: {entry!r}"
            )
        status, path = entry[:2], entry[3:]
        paths.append(path)
        # A rename or a copy is reported as "XY new\0old\0": both paths are
        # part of what changed, and consuming only one would leave the other
        # looking like a separate entry's status code.
        if "R" in status or "C" in status:
            if index < len(entries):
                paths.append(entries[index])
                index += 1
    return tuple(sorted(set(paths)))


def _ignored_paths(worktree: str, timeout: float) -> tuple[str, ...]:
    raw = run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=worktree,
        timeout=timeout,
        strip=False,
    )
    return tuple(sorted(set(_split_nul(raw))))


def _capture_patch(
    worktree: str, changed: tuple[str, ...], timeout: float
) -> tuple[str, str | None]:
    """Produce a unified diff of everything the agent changed.

    Untracked files are staged with ``--intent-to-add`` first, so that a fix
    which adds a file shows up as a new file in the patch rather than as
    nothing at all. That writes to the throwaway worktree's index only, which
    is removed with it.
    """
    if not changed:
        return "", None

    untracked = _split_nul(
        run_git(
            ["ls-files", "--others", "--exclude-standard", "-z"],
            cwd=worktree,
            timeout=timeout,
            strip=False,
        )
    )
    if untracked:
        run_git(
            ["add", "--intent-to-add", "--", *untracked],
            cwd=worktree,
            timeout=timeout,
        )

    patch = run_git(["diff", "--binary", "--no-color", "HEAD"], cwd=worktree, timeout=timeout)
    if len(patch.encode("utf-8", "replace")) > MAX_PATCH_BYTES:
        return "", (
            f"the working tree holds a diff larger than {MAX_PATCH_BYTES} bytes, "
            "which is not a bounded fix; it was not captured"
        )
    return patch, None


def inspect_workspace(
    worktree: str,
    *,
    target_head_sha: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> WorkspaceInspection:
    """Read what the coding agent left behind, independently of what it said."""
    inside = run_git(["rev-parse", "--is-inside-work-tree"], cwd=worktree, timeout=timeout)
    if inside != "true":
        raise WorkspaceError(
            f"the coding agent's working directory {worktree!r} is no longer a git "
            "work tree"
        )

    head = run_git(["rev-parse", "HEAD"], cwd=worktree, timeout=timeout)
    changed = _status_paths(worktree, timeout)
    ignored = _ignored_paths(worktree, timeout)
    residue = tuple(path for path in ignored if is_residue(path))
    unexpected = tuple(path for path in ignored if not is_residue(path))
    patch, refused = _capture_patch(worktree, changed, timeout)

    return WorkspaceInspection(
        head_sha=head,
        changed_paths=changed,
        ignored_paths=ignored,
        residue_paths=residue,
        unexpected_ignored=unexpected,
        patch=patch,
        patch_refused=refused,
    )


# --------------------------------------------------------------------------
# The pull request's own change set
# --------------------------------------------------------------------------
#
# This is the one authority in a fix turn that neither the reviewer nor the
# coding agent controls: what *this pull request* actually changed, according
# to git. :mod:`review_loop.fix_request` uses it as the outer boundary a
# finding's cited paths may select within, so that reviewer prose can narrow
# the fix scope but never widen it.
#
# It is deliberately computed the way the reviewer prompt (PR #29) tells a
# reviewer to compute it: against the point the branch diverged from its base,
# not against the commit CI merged onto. Those differ whenever the base branch
# has advanced since the branch was cut, and using the second would present
# base-only changes as though this pull request had made them -- widening the
# boundary with commits nobody in this pull request wrote.


def _try_rev_parse(worktree: str, revision: str, timeout: float) -> str | None:
    """Resolve a revision, or return None if this repository does not have it."""
    try:
        return run_git(
            ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            cwd=worktree,
            timeout=timeout,
        )
    except WorkspaceError:
        return None


def _base_tip(
    worktree: str, *, base_ref: str, remote: str, timeout: float
) -> str:
    """Find the base branch's tip, preferring what is already local.

    A remote-tracking ref first, then a local branch, and only then the
    network. Most invocations run in an ordinary clone where the first
    candidate answers, so the common path adds no fetch.
    """
    for candidate in (f"refs/remotes/{remote}/{base_ref}", f"refs/heads/{base_ref}"):
        resolved = _try_rev_parse(worktree, candidate, timeout)
        if resolved:
            return resolved

    try:
        run_git(
            ["fetch", "--quiet", remote, f"refs/heads/{base_ref}"],
            cwd=worktree,
            timeout=timeout,
        )
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"the base branch {base_ref!r} is not available in this repository "
            f"and could not be fetched from {remote!r} ({exc}). Without it the "
            "pull request's own change set cannot be established, and the fix "
            "scope would have no authority behind it but the reviewer's text"
        ) from exc

    fetched = _try_rev_parse(worktree, "FETCH_HEAD", timeout)
    if not fetched:
        raise WorkspaceError(
            f"{remote}/{base_ref} was fetched but did not resolve to a commit, so "
            "the pull request's own change set cannot be established"
        )
    return fetched


def resolve_change_set(
    worktree: str,
    *,
    head_sha: str,
    base_ref: str,
    remote: str = DEFAULT_REMOTE,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Return the paths this pull request changed, according to git.

    Raises :class:`WorkspaceError` rather than returning an empty tuple when
    the answer cannot be established. "I could not work out what this pull
    request changed" and "this pull request changed nothing" are different
    facts, and only one of them is a reason to route a fix.
    """
    base = _base_tip(worktree, base_ref=base_ref, remote=remote, timeout=timeout)
    try:
        merge_base = run_git(
            ["merge-base", base, head_sha], cwd=worktree, timeout=timeout
        )
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"no common ancestor of {base_ref} ({base[:12]}) and the target "
            f"{head_sha[:12]} could be found, so this pull request's change set "
            f"cannot be established: {exc}"
        ) from exc

    raw = run_git(
        ["diff", "--name-only", "-z", merge_base, head_sha],
        cwd=worktree,
        timeout=timeout,
        strip=False,
    )
    changed = tuple(sorted(set(_split_nul(raw))))
    if not changed:
        raise WorkspaceError(
            f"the target {head_sha[:12]} changes no path relative to where this "
            f"branch diverged from {base_ref} ({merge_base[:12]}), so there is no "
            "change set for a fix to be bounded to"
        )
    return changed
