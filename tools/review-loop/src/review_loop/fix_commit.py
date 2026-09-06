"""The git half of the push turn: apply, commit, push, and read back.

Everything in this module is arranged around one question -- **at each point,
what is the evidence, and who produced it?** The answer is never "the runner
remembers doing it". A commit is believed because ``git`` reports its parent
and re-renders its diff to the same bytes as the candidate patch; a push is
believed because the *remote* is asked what the branch now points at.

Four checks carry the slice, in the order they can first fail:

1. **The patch file is the candidate patch.** Its bytes hash to the digest the
   fix turn recorded. A file that merely looks like a fix, or a stale patch
   from an earlier turn, stops here -- before anything is applied.
2. **Applying it reproduces exactly the validated change.** The re-rendered
   diff of the working tree hashes to the same digest, and the changed-path
   set equals the one the fix turn observed. This is what makes "no unrelated
   workspace change leaks in" a checked fact rather than a hope: any extra
   edit, from a hook or a stale worktree or anything else, changes the diff
   and therefore the digest.
3. **The commit contains that and only that.** Its parent is the reviewed
   head, it is exactly one commit, its own diff hashes to the digest again,
   and the working tree is clean afterwards -- so nothing was left outside it.
4. **The remote agrees.** ``git ls-remote`` is asked what the branch points
   at, and it must be the commit this runner created. ``git push`` exiting
   zero is not the same claim: it says the local process succeeded, not that
   the ref moved where it was meant to.

The same machinery reads the *already pushed* state, and that is deliberate
rather than a convenience. A retry does not consult a log of what happened
last time; it asks the remote what the branch is, and identifies a commit as
this fix by the two facts that define it -- its parent is the reviewed head,
and its diff is the candidate patch. Local memory of a previous run is not
evidence and is never used.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_workspace import _ignored_paths, _status_paths, is_residue
from .model import FULL_SHA_PATTERN
from .patch_identity import capture_patch, digest_bytes, patch_digest
from .reviewer_workspace import DEFAULT_GIT_TIMEOUT_SECONDS, WorkspaceError, run_git


class CandidatePatchError(Exception):
    """The patch is not the validated candidate patch, or will not apply.

    Raised only before a commit exists. Nothing local or remote has changed.
    """


class CommitRefused(Exception):
    """The commit could not be created, or is not exactly the candidate patch.

    Raised only before a push. A local commit in a throwaway worktree is not
    authoritative state, so nothing a human has to undo exists at this point.
    """


class PushRefused(Exception):
    """The push did not happen, and the remote ref is where it was."""


class PushNotVerified(Exception):
    """The push was attempted and the remote does not show the expected commit.

    Distinct from :class:`PushRefused` because the two call for opposite
    responses: this one means the remote state is *not known* to be unchanged,
    and a human has to look.
    """


@dataclass(frozen=True)
class FixCommit:
    """One created commit, with the evidence that it is the candidate patch."""

    sha: str
    parent_sha: str
    tree_sha: str
    patch_sha256: str
    changed_paths: tuple[str, ...]
    message: str


def read_patch(path: str, *, expected_digest: str, expected_bytes: int) -> bytes:
    """Read the candidate patch, or refuse a file that is not it.

    The digest is checked against the file's raw bytes rather than against
    decoded text: what is applied is bytes, so what is identified must be too.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise CandidatePatchError(
            f"the candidate patch could not be read from {path!r}: {exc}"
        ) from exc

    if not data:
        raise CandidatePatchError(f"the candidate patch at {path!r} is empty")

    actual = digest_bytes(data)
    if actual != expected_digest:
        raise CandidatePatchError(
            f"the patch at {path!r} hashes to {actual}, but the fix turn recorded "
            f"{expected_digest}. This is not the validated candidate patch: it may "
            "be from another fix turn, edited by hand, or written by something "
            "else. Nothing was applied"
        )
    if len(data) != expected_bytes:
        raise CandidatePatchError(
            f"the patch at {path!r} is {len(data)} bytes but the fix turn recorded "
            f"{expected_bytes}"
        )
    return data


def require_clean_target(
    worktree: str, *, reviewed_head_sha: str, timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS
) -> None:
    """Refuse to build on anything but a clean checkout of the reviewed head."""
    head = run_git(["rev-parse", "HEAD"], cwd=worktree, timeout=timeout)
    if head != reviewed_head_sha:
        raise CommitRefused(
            f"the commit workspace {worktree!r} is at {head}, not the reviewed head "
            f"{reviewed_head_sha}; a commit made here would be against another commit"
        )
    changed = _status_paths(worktree, timeout)
    if changed:
        raise CommitRefused(
            f"the commit workspace {worktree!r} already holds {len(changed)} "
            f"uncommitted or untracked path(s) ({', '.join(changed[:5])}"
            + (", ..." if len(changed) > 5 else "")
            + "). The candidate patch is the only change that may be committed, so "
            "an unrelated change in the workspace stops the run rather than being "
            "committed alongside it"
        )


def apply_candidate_patch(
    worktree: str,
    *,
    patch_path: str,
    expected_digest: str,
    expected_paths: tuple[str, ...],
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Apply the candidate patch and prove the result is exactly it."""
    try:
        run_git(
            ["apply", "--index", "--whitespace=nowarn", "--", patch_path],
            cwd=worktree,
            timeout=timeout,
        )
    except WorkspaceError as exc:
        raise CandidatePatchError(
            f"the candidate patch does not apply to the reviewed head: {exc}. It "
            "was generated against this commit, so a failure here means the "
            "workspace is not the commit it was generated against. Nothing was "
            "committed"
        ) from exc

    applied = capture_patch(worktree, "HEAD", timeout=timeout)
    actual = patch_digest(applied)
    if actual != expected_digest:
        raise CandidatePatchError(
            f"applying the patch produced a diff hashing to {actual}, not the "
            f"candidate patch's {expected_digest}. The working tree holds something "
            "other than the validated fix -- an unrelated edit, a hook, or a "
            "different patch. Nothing was committed"
        )

    changed = _status_paths(worktree, timeout)
    if set(changed) != set(expected_paths):
        unexpected = sorted(set(changed) - set(expected_paths))
        missing = sorted(set(expected_paths) - set(changed))
        raise CandidatePatchError(
            "the applied working tree does not match the change the fix turn "
            "validated"
            + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
        )

    unexpected_ignored = [
        path for path in _ignored_paths(worktree, timeout) if not is_residue(path)
    ]
    if unexpected_ignored:
        raise CandidatePatchError(
            f"applying the patch left {len(unexpected_ignored)} git-ignored path(s) "
            f"that are not build or test residue: {', '.join(unexpected_ignored[:3])}"
        )


def create_fix_commit(
    worktree: str,
    *,
    message: str,
    reviewed_head_sha: str,
    expected_digest: str,
    expected_paths: tuple[str, ...],
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> FixCommit:
    """Commit the applied patch, then check the commit against the patch.

    The commit is created and *then* interrogated, because what matters is
    what git recorded rather than what this runner asked for. A commit hook
    that rewrites a file, a partially staged index, a second commit from
    somewhere -- each shows up in one of the checks below rather than in a
    comment saying it should not happen.
    """
    try:
        run_git(["commit", "--quiet", "-m", message], cwd=worktree, timeout=timeout)
    except WorkspaceError as exc:
        raise CommitRefused(f"the fix commit could not be created: {exc}") from exc

    sha = run_git(["rev-parse", "HEAD"], cwd=worktree, timeout=timeout)
    if not FULL_SHA_PATTERN.match(sha):
        raise CommitRefused(f"git reported the new commit as {sha!r}")

    parents = run_git(
        ["rev-list", "--parents", "-n", "1", sha], cwd=worktree, timeout=timeout
    ).split()
    if len(parents) != 2:
        raise CommitRefused(
            f"the fix commit {sha} has {len(parents) - 1} parents; a fix commit is "
            "one ordinary commit on top of the reviewed head"
        )
    parent = parents[1]
    if parent != reviewed_head_sha:
        raise CommitRefused(
            f"the fix commit {sha} is a child of {parent}, not of the reviewed head "
            f"{reviewed_head_sha}"
        )

    committed = capture_patch(worktree, parent, sha, timeout=timeout)
    actual = patch_digest(committed)
    if actual != expected_digest:
        raise CommitRefused(
            f"the fix commit {sha} contains a diff hashing to {actual}, not the "
            f"candidate patch's {expected_digest}; it is not the change that was "
            "validated. It exists only in a workspace this run removes"
        )

    names = run_git(
        ["diff", "--name-only", "-z", parent, sha],
        cwd=worktree,
        timeout=timeout,
        strip=False,
    )
    committed_paths = tuple(sorted({entry for entry in names.split("\0") if entry}))
    if set(committed_paths) != set(expected_paths):
        raise CommitRefused(
            f"the fix commit {sha} changes {', '.join(committed_paths)}, not the "
            f"validated {', '.join(expected_paths)}"
        )

    leftover = _status_paths(worktree, timeout)
    if leftover:
        raise CommitRefused(
            f"the fix commit {sha} left {len(leftover)} change(s) outside it "
            f"({', '.join(leftover[:5])}); the commit is not the whole fix"
        )

    return FixCommit(
        sha=sha,
        parent_sha=parent,
        tree_sha=run_git(["rev-parse", f"{sha}^{{tree}}"], cwd=worktree, timeout=timeout),
        patch_sha256=actual,
        changed_paths=committed_paths,
        message=message,
    )


def read_remote_tip(
    repo_root: str,
    *,
    remote: str,
    branch: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str | None:
    """Ask the remote what one branch points at. ``None`` if it has no such branch."""
    ref = f"refs/heads/{branch}"
    raw = run_git(
        ["ls-remote", "--", remote, ref], cwd=repo_root, timeout=timeout, strip=False
    )
    matches = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == ref:
            matches.append(parts[0].strip())
    if not matches:
        return None
    if len(set(matches)) != 1:
        raise WorkspaceError(
            f"{remote} reports {len(set(matches))} different values for {ref}"
        )
    sha = matches[0]
    if not FULL_SHA_PATTERN.match(sha):
        raise WorkspaceError(f"{remote} reports {ref} as {sha!r}, which is not a commit")
    return sha


def describe_remote_commit(
    repo_root: str,
    *,
    remote: str,
    branch: str,
    tip: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> tuple[str | None, str | None]:
    """Fetch a remote branch tip and report ``(parent sha, patch digest)``.

    Both are ``None`` when the commit has no single parent, because then it is
    not the shape a fix commit has and there is no diff worth digesting. The
    fetch writes objects into ``repo_root`` and moves no ref there.
    """
    try:
        run_git(
            ["fetch", "--quiet", remote, f"refs/heads/{branch}"],
            cwd=repo_root,
            timeout=timeout,
        )
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"the pull request branch {branch!r} could not be fetched from "
            f"{remote!r} ({exc}), so what it currently points at cannot be "
            "identified"
        ) from exc

    parents = run_git(
        ["rev-list", "--parents", "-n", "1", tip], cwd=repo_root, timeout=timeout
    ).split()
    if len(parents) != 2:
        return None, None
    parent = parents[1]
    return parent, patch_digest(capture_patch(repo_root, parent, tip, timeout=timeout))


def push_fix_commit(
    worktree: str,
    *,
    remote: str,
    refspec: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Push one commit to one branch, with ordinary fast-forward semantics.

    No ``--force``, no ``+`` in the refspec, no ``--force-with-lease``, and no
    tag: a non-fast-forward is refused by the remote, which is the correct
    place for that decision. ``--`` separates the remote from the refspec so
    neither can be read as an option.

    A ``pre-push`` hook is deliberately **not** bypassed. It is the operator's
    own configuration on the operator's own machine, and a runner that gained
    push authority this week is not the thing that should start ignoring it.
    A hook that refuses the push is a refusal, reported as one.
    """
    try:
        run_git(
            ["push", "--quiet", "--", remote, refspec],
            cwd=worktree,
            timeout=timeout,
        )
    except WorkspaceError as exc:
        raise PushRefused(str(exc)) from exc
