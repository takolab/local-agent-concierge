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

from dataclasses import dataclass, field

from .agent_workspace import _ignored_paths, _status_paths, is_residue
from .model import FULL_SHA_PATTERN
from .patch_identity import capture_patch, digest_bytes, patch_digest
from .reviewer_workspace import (
    DEFAULT_GIT_TIMEOUT_SECONDS,
    GitTimeoutError,
    WorkspaceError,
    run_git,
    run_git_capture,
)


#: What the remote itself said about our ref, read from ``--porcelain``.
#: Defined here rather than beside the push, because :class:`PushRefused`
#: carries one as a default argument and a default is evaluated when the
#: class body runs.
#:
#: The distinction that matters is not success versus failure but **proven
#: versus unproven**. Only ``REMOTE_REJECTED`` establishes that nothing was
#: written, so only recognised refusals may reach it; everything else that
#: went wrong is ``REMOTE_UNKNOWN`` and stays unknown.
REMOTE_ACCEPTED = "accepted"
#: The ref already held this exact commit, so this push moved nothing. A
#: separate value from ``accepted`` because "the branch holds the fix" and
#: "this run put it there" are different claims.
REMOTE_UP_TO_DATE = "up_to_date"
REMOTE_REJECTED = "rejected"
#: The remote answered about our ref and the answer does not establish
#: anything -- a transient server-side failure, or a summary this runner does
#: not recognise.
REMOTE_UNKNOWN = "unknown"
#: No per-ref line at all: a local hook refusal, a dropped connection, a
#: timeout. git never got an answer to relay.
REMOTE_SILENT = "silent"

#: Failure summaries that are the remote *refusing*, and therefore evidence
#: that the ref did not move. Anything else after a ``!`` is unknown: git uses
#: ``!`` for "rejected **or failed to push**", and ``[remote failure]`` in
#: particular can be a transient server-side error whose outcome is not
#: established. Matched against C-locale output -- see
#: :func:`review_loop.reviewer_workspace.run_git_capture`.
REFUSAL_SUMMARIES = ("[rejected]", "[remote rejected]", "[no match]")

#: Porcelain flags that mean a ref *was* updated. ``=`` means it was already
#: up to date, and ``!`` means rejected or failed -- neither is a write.
_WRITTEN_FLAGS = (" ", "+", "-", "*")


@dataclass(frozen=True)
class UnexpectedRefs:
    """Refs the remote reported that this runner did not ask it to touch.

    Split by what the report actually establishes, because "another ref
    appeared in the output" and "another ref was written" are different
    facts and only the second is a boundary violation:

    * ``written`` -- the remote says it updated them. Established.
    * ``unresolved`` -- it tried and the answer settles nothing, the same
      ``[remote failure]`` class the branch's own line has.
    * ``untouched`` -- reported and demonstrably not written: already up to
      date, or refused. Worth saying out loud, worth stopping for.
    """

    written: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    untouched: tuple[str, ...] = ()

    @property
    def any_reported(self) -> bool:
        return bool(self.written or self.unresolved or self.untouched)


class CandidatePatchError(Exception):
    """The patch is not the validated candidate patch, or will not apply.

    Raised only before a commit exists. Nothing local or remote has changed.
    """


class CommitRefused(Exception):
    """The commit could not be created, or is not exactly the candidate patch.

    Raised only before a push. A local commit in a throwaway worktree is not
    authoritative state, so nothing a human has to undo exists at this point.
    """


@dataclass(frozen=True)
class PushAttempt:
    """Everything the remote said about a push, not only about *our* ref.

    ``unexpected_refs`` exists because "one bounded ref update" is a claim
    about what the remote *did*, and the only place that shows up is the
    remote's own report. A push that also wrote a tag says so on a line this
    runner would otherwise have skipped past -- and one that merely *mentioned*
    a tag it left alone says so on a line that looks much the same, which is
    why the two are classified apart rather than counted together.
    """

    report: str
    unexpected_refs: UnexpectedRefs = field(default_factory=UnexpectedRefs)


class PushRefused(Exception):
    """``git push`` exited non-zero. What that means is a separate question.

    ``attempt`` carries the remote's own per-ref answer, because the exit
    status does not distinguish "the remote refused this ref" from "the
    answer never arrived" -- and only the first of those proves that nothing
    was written.
    """

    def __init__(self, message: str, attempt: PushAttempt | None = None) -> None:
        super().__init__(message)
        self.attempt = attempt if attempt is not None else PushAttempt(REMOTE_SILENT)

    @property
    def report(self) -> str:
        return self.attempt.report


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


def read_remote_urls(
    repo_root: str,
    *,
    remote: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Every URL git would use for ``remote``: fetch and push, deduplicated.

    Three things about this are load-bearing, and each was got wrong before it
    was got right.

    **Both directions.** A remote can carry a separate ``pushurl``, and the URL
    that decides where a push lands is not the one that decides where a fetch
    comes from.

    **``--all``, not the first one.** A remote may have *several* push URLs,
    and ``git push`` writes to **every** one of them while
    ``git remote get-url --push`` without ``--all`` reports only the first.
    Checking that first URL and pushing to all of them is not a check; it is
    the appearance of one. An operator with
    ``origin`` pointing at this repository *and* at another would have passed
    the earlier version and written to both.

    **The effective URL.** ``git remote get-url`` applies any
    ``url.<base>.insteadOf`` rewriting before answering, so what comes back is
    where git will actually go rather than what someone typed into the config.
    """
    urls: list[str] = []
    for argv in (
        ["remote", "get-url", "--all", "--", remote],
        ["remote", "get-url", "--push", "--all", "--", remote],
    ):
        try:
            value = run_git(argv, cwd=repo_root, timeout=timeout)
        except WorkspaceError as exc:
            raise WorkspaceError(
                f"the remote {remote!r} could not be resolved to a URL ({exc}), so "
                "which repository this push would reach cannot be established"
            ) from exc
        for line in value.splitlines():
            entry = line.strip()
            if entry and entry not in urls:
                urls.append(entry)
    return tuple(urls)


def contains_commit(
    repo_root: str,
    *,
    remote: str,
    branch: str,
    commit: str,
    tip: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> bool | None:
    """Is ``commit`` in the history of the branch's current ``tip``?

    This is the question that separates "the push never landed" from "the push
    landed and the branch moved on again", and no other observation answers
    it. A read-back that is not our commit is compatible with both, and the
    two call for opposite reactions from a human.

    ``None`` means the question could not be answered -- the branch could not
    be fetched, or git could not compare the two -- which is itself an answer
    a caller must not round to ``False``.
    """
    try:
        run_git(
            ["fetch", "--quiet", remote, f"refs/heads/{branch}"],
            cwd=repo_root,
            timeout=timeout,
        )
        completed = run_git(
            ["rev-list", "--max-count=1", commit, f"^{tip}"],
            cwd=repo_root,
            timeout=timeout,
        )
    except WorkspaceError:
        return None
    # `rev-list <commit> ^<tip>` lists what is in `commit` but not reachable
    # from `tip`. Empty means `commit` is an ancestor of `tip`.
    return completed == ""


def _porcelain_lines(stdout: str):
    """Yield ``(flag, refspec, summary)`` for each per-ref line git reported."""
    for line in stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 3 and ":" in fields[1]:
            yield fields[0], fields[1], fields[2]


def read_unexpected_refs(stdout: str, *, refspec: str) -> UnexpectedRefs:
    """Classify every ref the remote reports other than the authorised one.

    The argv already refuses the two expansions git configuration can apply --
    ``push.followTags`` and ``push.recurseSubmodules`` -- but that is the
    *stated* half of the boundary. This is the backstop: whatever the reason,
    a report naming another ref means the push did more than name one refspec,
    and the runner would rather stop and say so than continue on the strength
    of having passed the right flags.

    What it will not do is call every such line a write. ``git push`` reports
    a ref it left alone the same way it reports one it changed -- by printing
    a line for it -- and the flag is what separates them. Exit 74 means a
    write beyond authority was *established*, so only the flags that mean "it
    was updated" may produce one.
    """
    written: list[str] = []
    unresolved: list[str] = []
    untouched: list[str] = []
    for flag, spec, summary in _porcelain_lines(stdout):
        if spec == refspec:
            continue
        if flag.startswith("!"):
            if any(reason in summary for reason in REFUSAL_SUMMARIES):
                untouched.append(spec)
            else:
                unresolved.append(spec)
        elif flag.startswith("="):
            untouched.append(spec)
        elif flag[:1] in _WRITTEN_FLAGS or flag == "":
            written.append(spec)
        else:
            unresolved.append(spec)
    return UnexpectedRefs(
        written=tuple(written),
        unresolved=tuple(unresolved),
        untouched=tuple(untouched),
    )


def read_push_report(stdout: str, *, refspec: str) -> str:
    """What did the remote say about *our* ref?

    ``git push --porcelain`` writes one machine-readable line per ref, on
    stdout, whether the push succeeded or failed::

        To <url>
        !\t<sha>:refs/heads/b\t[rejected] (non-fast-forward)
        Done

    The flag character is the first field: ``!`` for a ref that was rejected
    **or failed to push**, ``=`` for one that was already up to date, and a
    space, ``*`` or ``+`` for one that was updated.

    That ``or failed to push`` is the whole reason this function is not two
    lines. Reading every ``!`` as a refusal would turn ``[remote failure]`` --
    a transient server-side error whose outcome is *not* established -- into
    evidence that nothing was written, which is the strongest claim this
    runner makes and the one it must never make on a guess. So refusals are an
    explicit allowlist and everything else fails closed to
    :data:`REMOTE_UNKNOWN`.
    """
    for _flag, spec, _summary in _porcelain_lines(stdout):
        if spec != refspec:
            continue
        flag, summary = _flag, _summary
        if flag.startswith("!"):
            if any(reason in summary for reason in REFUSAL_SUMMARIES):
                return REMOTE_REJECTED
            return REMOTE_UNKNOWN
        if flag.startswith("="):
            return REMOTE_UP_TO_DATE
        return REMOTE_ACCEPTED
    return REMOTE_SILENT


def push_fix_commit(
    worktree: str,
    *,
    remote: str,
    refspec: str,
    lease: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Push one commit to one branch, with ordinary fast-forward semantics.

    No ``--force`` and no ``+`` in the refspec, so a non-fast-forward is
    refused by the remote rather than by this runner's good intentions; no tag
    ref, and no configuration-driven expansion into one. The ``lease`` is the
    one force-shaped argument here and it is the opposite of a force: it makes
    the update conditional. ``--`` separates the remote from the refspec so
    neither can be read as an option.

    A ``pre-push`` hook is deliberately **not** bypassed. It is the operator's
    own configuration on the operator's own machine, and a runner that gained
    push authority this week is not the thing that should start ignoring it.
    A hook that refuses the push is a refusal, reported as one.

    ``lease`` is the compare-and-swap condition built by
    :meth:`review_loop.push_branch.PushTarget.lease`, and it is what makes the
    write atomic with respect to the read that authorised it. Despite its
    name it authorises no rewrite: the commit's parent is already proven to be
    the leased value, so the update is an ordinary fast-forward that the
    remote will only apply while the ref still holds exactly that value.

    Returns a :class:`PushAttempt`: what the remote said about our ref -- see
    :func:`read_push_report` -- together with any *other* ref it reported
    updating. :class:`PushRefused` carries the same, because git's exit status
    alone cannot tell a rejection the remote sent from an answer that never
    arrived.
    """
    try:
        result = run_git_capture(
            [
                "push",
                "--porcelain",
                # Two expansions the operator's own configuration can apply to
                # a push that names one refspec, both refused explicitly.
                # `push.followTags` pushes annotated tags reachable from the
                # commit -- a second ref update, in a namespace this runner
                # promises never to write. `push.recurseSubmodules=on-demand`
                # pushes submodule commits to *their* remotes, turning one
                # repository write into writes to several.
                "--no-follow-tags",
                "--recurse-submodules=no",
                lease,
                "--",
                remote,
                refspec,
            ],
            cwd=worktree,
            timeout=timeout,
        )
    except GitTimeoutError as exc:
        # The process had already started, so the remote may have applied the
        # update and the answer may be the only thing that went missing. This
        # is emphatically not a workspace failure, which is what it used to be
        # reported as -- with `repository_mutated: false` attached to a branch
        # that could already hold the commit.
        raise PushRefused(str(exc), PushAttempt(REMOTE_SILENT)) from exc

    attempt = PushAttempt(
        report=read_push_report(result.stdout, refspec=refspec),
        unexpected_refs=read_unexpected_refs(result.stdout, refspec=refspec),
    )
    if not result.ok:
        raise PushRefused(f"git push failed: {result.failure}", attempt)
    return attempt
