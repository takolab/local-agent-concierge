"""One push turn: candidate patch -> fix commit -> push -> authoritative CI.

This is the first stage of the review loop that writes to the repository, and
the order of its steps is the design, for the same reason it was in the review
and fix turns -- except that here a step performed too early cannot be undone
by ending the run.

**Establish authority before touching anything.** The branch is derived from
GitHub's own pull request object (:mod:`review_loop.push_branch`) before a
workspace exists. A run whose target is a fork, a closed pull request or the
default branch stops with no fetch, no worktree and no commit.

**Reconstruct state from evidence, not from memory.** The remote is asked what
the pull request branch points at *before* anything is applied, and the answer
decides which of two paths this run is on: the branch is still at the reviewed
head, so a fix commit has to be made -- or it already holds a commit whose
parent is the reviewed head and whose diff *is* the candidate patch, in which
case this exact fix is already pushed and the run resumes at CI. There is no
state file, no lock, and no record of previous runs; a retry re-derives which
case it is in from git and GitHub every time. That is the whole idempotency
mechanism, and it is deliberately the only one: a runner that remembered
having pushed would be wrong exactly when it mattered.

**Verify the write from the far side.** ``git push`` exiting zero says the
local process succeeded. What is believed instead is ``git ls-remote``: the
branch must point at the exact commit this runner created. If it does not,
the run ends in ``PUSH_NOT_VERIFIED`` -- which is not a failure and not a
success, because remote state is genuinely unknown at that point, and saying
otherwise would be the one thing an operator cannot recover from.

**Bind CI to the pushed commit, not to the branch.** The wait re-uses PR
#28's verification wholesale, and requires that what it verified is the exact
commit that was pushed. CI from the reviewed head, from someone else's later
commit, or from a ``push`` event rather than the authoritative
``pull_request`` one, all fail to satisfy that -- the first two here, the
third inside :mod:`review_loop.evaluate`, which never treated them as
evidence.

**Merge-context currency is back in contract.** A fix turn deliberately did
not re-establish it, because a fix turn wrote nothing. This one does: READY
from :func:`review_loop.runner.verify_pull_request` already requires that the
base commit CI merged onto is still the base branch tip, and that requirement
is asserted again here before the result is called ready for re-review. A
push whose CI is green against a merge that no longer exists is not a pushed
fix that anyone may act on.

What this turn still does **not** do: no re-review, no finding-resolution
judgement, no second fix round, no merge. It ends by saying what is on the
branch and what CI thought of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Callable

from .fix_commit import (
    REMOTE_ACCEPTED,
    REMOTE_REJECTED,
    REMOTE_SILENT,
    REMOTE_UP_TO_DATE,
    CandidatePatchError,
    CommitRefused,
    FixCommit,
    PushRefused,
    apply_candidate_patch,
    contains_commit,
    create_fix_commit,
    describe_remote_commit,
    push_fix_commit,
    read_patch,
    read_remote_tip,
    read_remote_urls,
    require_clean_target,
)
from .fix_handoff import FixHandoff
from .github_client import GitHubApiError
from .model import CiEvaluation, Verdict, short_sha
from .push_branch import (
    BranchAuthorityError,
    PushTarget,
    check_remote_repository,
    resolve,
)
from .push_response import (
    DEFAULT_CI_POLL_SECONDS,
    DEFAULT_CI_TIMEOUT_SECONDS,
    PUSH_EXIT_CODES,
    PUSHED_OUTCOMES,
    PushOutcome,
)
from .review_target import ReviewTarget, TargetNotVerified, from_evaluation
from .reviewer_workspace import DEFAULT_REMOTE, GitTimeoutError, WorkspaceError
from .runner import verify_pull_request

#: How many consecutive GitHub failures the CI wait tolerates before giving
#: up. A transient 502 while waiting half an hour should not discard a
#: verified push; a persistent one should not be waited out in silence.
MAX_CI_API_FAILURES = 3


@dataclass(frozen=True)
class PushResult:
    """Everything one push turn established, and what it changed."""

    outcome: PushOutcome
    reasons: tuple[str, ...] = ()
    target: ReviewTarget | None = None
    push_target: PushTarget | None = None
    #: The commit this run created, when it created one.
    commit: FixCommit | None = None
    #: The commit verified to be on the pull request branch. Set whenever the
    #: branch is known to hold the fix, including when a previous run pushed it.
    pushed_sha: str | None = None
    #: The candidate patch file this run actually read, resolved to an
    #: absolute path. Reported because "which file was this?" is part of the
    #: provenance, and because the path the operator typed may be relative.
    patch_path: str | None = None
    #: True when this run created and pushed the commit; False when it found
    #: the exact fix already pushed and did nothing.
    push_performed: bool = False
    already_pushed: bool = False
    commit_created: bool = False
    dry_run: bool = False
    ci_evaluation: CiEvaluation | None = None
    ci_polls: int = 0
    verified_target: ReviewTarget | None = field(default=None)

    @property
    def exit_code(self) -> int:
        return PUSH_EXIT_CODES[self.outcome]

    @property
    def repository_mutated(self) -> bool | None:
        """Whether the pull request branch now holds the fix commit.

        ``None`` is a real answer, not a missing one: after
        ``PUSH_NOT_VERIFIED`` the remote was written to and did not read back
        as expected, and pretending to know which way that went would be
        worse than saying so.
        """
        if self.outcome is PushOutcome.PUSH_NOT_VERIFIED:
            return None
        return self.outcome in PUSHED_OUTCOMES


def _result(outcome: PushOutcome, reasons, **kwargs) -> PushResult:
    return PushResult(outcome=outcome, reasons=tuple(reasons), **kwargs)


def remote_said_label(remote: str) -> str:
    return f"the remote {remote!r}"


def _commit_message(handoff: FixHandoff) -> str:
    """A commit message built only from validated, runner-held facts.

    No agent prose reaches it. The finding ids have been through the finding
    id pattern, the SHAs through the full-SHA check, and the digest through
    the digest pattern -- so the message is as checkable as the commit it
    describes, and there is no free-text channel from the Coding Agent into
    the repository's history.
    """
    findings = ", ".join(handoff.finding_ids)
    return (
        f"fix: address independent review finding(s) {findings}\n"
        "\n"
        f"Applied by review-loop push from the candidate patch validated for\n"
        f"pull request #{handoff.target.number} at reviewed head "
        f"{handoff.target.head_sha}.\n"
        "\n"
        f"Findings: {findings}\n"
        f"Candidate patch sha256: {handoff.patch_sha256}\n"
    )


# --------------------------------------------------------------------------
# Pre-flight: what does the branch currently hold?
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchState:
    """What the remote branch holds, identified against the candidate patch."""

    tip: str
    #: True when the tip is a commit whose parent is the reviewed head and
    #: whose diff is exactly the candidate patch -- i.e. this fix, pushed.
    is_this_fix: bool
    reason: str


def _classify_branch(
    repo_root: str,
    *,
    remote: str,
    branch: str,
    reviewed_head_sha: str,
    expected_digest: str,
    timeout: float,
) -> BranchState:
    """Decide, from git alone, whether this fix is already on the branch."""
    tip = read_remote_tip(repo_root, remote=remote, branch=branch, timeout=timeout)
    if tip is None:
        raise WorkspaceError(
            f"{remote} has no branch {branch!r}, but GitHub reports it as the head "
            "branch of this pull request; the remote and the pull request disagree"
        )
    if tip == reviewed_head_sha:
        return BranchState(
            tip=tip,
            is_this_fix=False,
            reason=f"the branch is at the reviewed head {short_sha(tip)}",
        )

    parent, digest = describe_remote_commit(
        repo_root, remote=remote, branch=branch, tip=tip, timeout=timeout
    )
    if parent == reviewed_head_sha and digest == expected_digest:
        return BranchState(
            tip=tip,
            is_this_fix=True,
            reason=(
                f"the branch is already at {tip}, a commit whose parent is the "
                f"reviewed head and whose diff is exactly this candidate patch; "
                "this fix is already pushed"
            ),
        )
    return BranchState(
        tip=tip,
        is_this_fix=False,
        reason=(
            f"the branch is at {tip}, which is neither the reviewed head "
            f"{reviewed_head_sha} nor a commit containing this candidate patch"
            + (f" (its parent is {parent})" if parent else " (it has no single parent)")
        ),
    )


# --------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------


def run_push(
    *,
    client,
    workspace,
    repo_root: str,
    handoff: FixHandoff,
    patch_path: str,
    git_remote: str = DEFAULT_REMOTE,
    git_timeout: float = 300.0,
    ci_timeout: float = DEFAULT_CI_TIMEOUT_SECONDS,
    ci_poll_seconds: float = DEFAULT_CI_POLL_SECONDS,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    dry_run: bool = False,
) -> PushResult:
    """Commit one validated candidate patch, push it, and wait for its CI.

    The patch path is resolved to an absolute path **here**, once, and the
    resolved value is both used everywhere below and attached to every
    outcome -- so no failure path can forget to say which file it read.
    """
    resolved = os.path.abspath(patch_path)
    result = _run_push(
        client=client,
        workspace=workspace,
        repo_root=repo_root,
        handoff=handoff,
        patch_path=resolved,
        git_remote=git_remote,
        git_timeout=git_timeout,
        ci_timeout=ci_timeout,
        ci_poll_seconds=ci_poll_seconds,
        clock=clock,
        sleep=sleep,
        dry_run=dry_run,
    )
    return replace(result, patch_path=resolved)


def _run_push(
    *,
    client,
    workspace,
    repo_root: str,
    handoff: FixHandoff,
    patch_path: str,
    git_remote: str,
    git_timeout: float,
    ci_timeout: float,
    ci_poll_seconds: float,
    clock,
    sleep,
    dry_run: bool,
) -> PushResult:
    """The turn itself, with ``patch_path`` already absolute.

    That absoluteness is load-bearing, not tidiness. The two places the patch
    is read run in two different directories: :func:`read_patch` opens it from
    wherever the operator invoked the command, and ``git apply`` runs with
    ``cwd`` set to a temporary worktree that contains none of the operator's
    files. A relative path -- ``--patch fix.patch``, which is exactly what the
    documented flow produces -- therefore passed the identity check and then
    failed to open, and was reported as though the patch did not apply to the
    reviewed commit. Resolving once, above, removes the possibility rather
    than the symptom.
    """
    import time

    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    target = handoff.target

    # Resolve the patch path **here**, against the process's own working
    # directory, and use the resolved path everywhere after.
    #
    # This is not tidiness. The two places the patch is read run in two
    # different directories: `read_patch` opens it from wherever the operator
    # invoked the command, and `git apply` runs with `cwd` set to a temporary
    # worktree that contains none of the operator's files. A relative path --
    # `--patch fix.patch`, which is exactly what the documented flow produces
    # -- therefore passed the identity check and then failed to open, and was
    # reported as though the patch did not apply to the reviewed commit.
    # Resolving once removes the possibility rather than the symptom.
    patch_path = os.path.abspath(patch_path)

    # 1. The patch file is the validated candidate patch. Checked first,
    #    because it is the cheapest refusal and the one that needs no network:
    #    a wrong patch stops the run before GitHub is even asked a question.
    try:
        read_patch(
            patch_path,
            expected_digest=handoff.patch_sha256,
            expected_bytes=handoff.patch_bytes,
        )
    except CandidatePatchError as exc:
        return _result(PushOutcome.PATCH_IDENTITY_MISMATCH, (str(exc),), target=target)

    # 2. Which ref may this run write? Derived from GitHub's pull request
    #    object and from nothing the reviewer, the agent or the handoff says.
    try:
        payload = client.get_pull_request(target.number)
    except GitHubApiError as exc:
        return _result(
            PushOutcome.PUSH_API_ERROR,
            (f"the pull request could not be read before the push: {exc}",),
            target=target,
        )
    try:
        push_target = resolve(payload, repo=target.repo, number=target.number)
    except BranchAuthorityError as exc:
        return _result(PushOutcome.PUSH_BRANCH_REFUSED, (str(exc),), target=target)

    # 2b. Which *repository* receives that ref? The branch came from GitHub,
    #     but the remote came from the command line, so without this the two
    #     halves of the destination have different authorities -- and a remote
    #     holding the same branch at the same commit would be pushed to while
    #     every message said "this repository".
    try:
        remote_urls = read_remote_urls(
            repo_root, remote=git_remote, timeout=git_timeout
        )
    except WorkspaceError as exc:
        return _result(
            PushOutcome.PUSH_BRANCH_REFUSED,
            (str(exc),),
            target=target,
            push_target=push_target,
        )
    try:
        check_remote_repository(
            remote_urls, expected_repo=target.repo, remote=git_remote
        )
    except BranchAuthorityError as exc:
        return _result(
            PushOutcome.PUSH_BRANCH_REFUSED,
            (str(exc),),
            target=target,
            push_target=push_target,
        )

    if push_target.base_ref != target.base_ref:
        return _result(
            PushOutcome.PUSH_TARGET_STALE,
            (
                f"the pull request now targets {push_target.base_ref!r}, not the "
                f"{target.base_ref!r} the fix was validated against",
            ),
            target=target,
            push_target=push_target,
        )

    # 3. What does the branch hold right now? This is where a retry finds out
    #    that it has nothing left to do, from the remote rather than from any
    #    record of a previous run.
    try:
        branch_state = _classify_branch(
            repo_root,
            remote=git_remote,
            branch=push_target.branch,
            reviewed_head_sha=target.head_sha,
            expected_digest=handoff.patch_sha256,
            timeout=git_timeout,
        )
    except WorkspaceError as exc:
        return _result(
            PushOutcome.PUSH_WORKSPACE_INVALID,
            (str(exc),),
            target=target,
            push_target=push_target,
        )

    if branch_state.is_this_fix:
        # Already pushed by an earlier run. GitHub's pull request object may
        # still be reporting the reviewed head for a moment, so both it and
        # the pushed commit are accepted here; the CI wait below is what
        # actually requires the pull request to be at the pushed commit.
        if push_target.head_sha not in (target.head_sha, branch_state.tip):
            return _result(
                PushOutcome.PUSH_TARGET_STALE,
                (
                    branch_state.reason,
                    f"but the pull request head is {push_target.head_sha}, which is "
                    "neither; the branch and the pull request disagree and a human "
                    "should look before anything else is pushed",
                ),
                target=target,
                push_target=push_target,
            )
        if dry_run:
            return _result(
                PushOutcome.PUSH_PREPARED,
                (branch_state.reason, "dry run: nothing further was done"),
                target=target,
                push_target=push_target,
                pushed_sha=branch_state.tip,
                already_pushed=True,
                dry_run=True,
            )
        return _wait_for_ci(
            client=client,
            handoff=handoff,
            push_target=push_target,
            pushed_sha=branch_state.tip,
            commit=None,
            push_performed=False,
            already_pushed=True,
            prior_reasons=(branch_state.reason,),
            ci_timeout=ci_timeout,
            ci_poll_seconds=ci_poll_seconds,
            clock=clock,
            sleep=sleep,
        )

    if branch_state.tip != target.head_sha:
        return _result(
            PushOutcome.PUSH_TARGET_STALE,
            (
                branch_state.reason,
                "the candidate patch is against the reviewed head and this runner "
                "does not rebase or adapt it; a new fix turn against the current "
                "head is required",
            ),
            target=target,
            push_target=push_target,
        )

    if push_target.head_sha != target.head_sha:
        return _result(
            PushOutcome.PUSH_TARGET_STALE,
            (
                f"the pull request head is {push_target.head_sha}, not the reviewed "
                f"{target.head_sha} the candidate patch was generated against",
            ),
            target=target,
            push_target=push_target,
        )

    # 4. Apply, commit, push -- inside a workspace bound to the reviewed head.
    try:
        with workspace.open(target.head_sha) as worktree:
            return _commit_and_push(
                client=client,
                worktree=worktree,
                repo_root=repo_root,
                handoff=handoff,
                push_target=push_target,
                patch_path=patch_path,
                git_remote=git_remote,
                git_timeout=git_timeout,
                ci_timeout=ci_timeout,
                ci_poll_seconds=ci_poll_seconds,
                clock=clock,
                sleep=sleep,
                dry_run=dry_run,
            )
    except WorkspaceError as exc:
        return _result(
            PushOutcome.PUSH_WORKSPACE_INVALID,
            (str(exc),),
            target=target,
            push_target=push_target,
        )


def _commit_and_push(
    *,
    client,
    worktree: str,
    repo_root: str,
    handoff: FixHandoff,
    push_target: PushTarget,
    patch_path: str,
    git_remote: str,
    git_timeout: float,
    ci_timeout: float,
    ci_poll_seconds: float,
    clock,
    sleep,
    dry_run: bool,
) -> PushResult:
    """Everything that happens while the bound worktree exists."""
    target = handoff.target

    def refused(outcome: PushOutcome, reason: str) -> PushResult:
        return _result(
            outcome, (reason,), target=target, push_target=push_target
        )

    # A `WorkspaceError` on any of the three steps below is a git command
    # failing inside a workspace that was already prepared and verified, so it
    # is reported as a refusal to commit rather than as an invalid workspace.
    # Both are no-write outcomes; the difference is which one an operator goes
    # and looks at.
    try:
        require_clean_target(
            worktree, reviewed_head_sha=target.head_sha, timeout=git_timeout
        )
    except (CommitRefused, WorkspaceError) as exc:
        return refused(PushOutcome.COMMIT_REFUSED, str(exc))

    try:
        apply_candidate_patch(
            worktree,
            patch_path=patch_path,
            expected_digest=handoff.patch_sha256,
            expected_paths=handoff.changed_paths,
            timeout=git_timeout,
        )
    except CandidatePatchError as exc:
        return refused(PushOutcome.PATCH_IDENTITY_MISMATCH, str(exc))
    except WorkspaceError as exc:
        return refused(
            PushOutcome.COMMIT_REFUSED,
            f"the candidate patch could not be applied: {exc}",
        )

    if dry_run:
        return _result(
            PushOutcome.PUSH_PREPARED,
            (
                "dry run: the candidate patch is the one the fix turn validated and "
                f"applies cleanly to {target.head_sha}. Nothing was committed and "
                f"nothing was pushed to {push_target.ref}",
            ),
            target=target,
            push_target=push_target,
            dry_run=True,
        )

    try:
        commit = create_fix_commit(
            worktree,
            message=_commit_message(handoff),
            reviewed_head_sha=target.head_sha,
            expected_digest=handoff.patch_sha256,
            expected_paths=handoff.changed_paths,
            timeout=git_timeout,
        )
    except (CommitRefused, WorkspaceError) as exc:
        return refused(PushOutcome.COMMIT_REFUSED, str(exc))

    created = (
        f"created {commit.sha} on top of {commit.parent_sha}, containing exactly "
        f"the candidate patch {commit.patch_sha256}",
    )

    # The write. Everything above this line left the pull request branch
    # untouched; everything below it may not have.
    push_failure: str | None = None
    refspec = push_target.refspec(commit.sha)
    try:
        remote_said = push_fix_commit(
            worktree,
            remote=git_remote,
            refspec=refspec,
            # The read that authorised this write said the branch was at the
            # reviewed head. The lease makes the write conditional on that
            # still being true, closing the window in which the branch could
            # be deleted (a plain push recreates it) or rewound to an ancestor
            # (a plain push accepts it as a fast-forward).
            lease=push_target.lease(target.head_sha),
            timeout=git_timeout,
        )
    except PushRefused as exc:
        push_failure = str(exc)
        # The remote's own per-ref answer, not git's exit status. Only the
        # first of those can distinguish a rejection the remote sent from an
        # answer that never arrived.
        remote_said = exc.report
    except GitTimeoutError as exc:
        # Belt and braces. `push_fix_commit` already translates a timeout into
        # a silent PushRefused, and this catches one raised anywhere else on
        # the push path so that the rule holds at the boundary that matters
        # rather than only at the layer that happens to implement it:
        #
        #   a failure *before* the push process starts may be a verified
        #   no-write; a failure *after* it starts is unknown until remote
        #   evidence proves otherwise.
        #
        # Without this the timeout escapes to the outer WorkspaceError handler
        # and is reported as an invalid workspace -- a pre-write failure --
        # discarding both the mutation and the record of the commit.
        push_failure = str(exc)
        remote_said = REMOTE_SILENT

    # Read the ref back from the remote either way. On the failure path this
    # is what distinguishes a rejected push from a lost response: a push whose
    # answer never arrived still moved the ref, and reporting that as "nothing
    # was written" would send an operator to fix a branch that is already
    # correct.
    try:
        observed = read_remote_tip(
            repo_root, remote=git_remote, branch=push_target.branch, timeout=git_timeout
        )
    except WorkspaceError as exc:
        return _result(
            PushOutcome.PUSH_NOT_VERIFIED,
            created
            + ((f"the push reported: {push_failure}",) if push_failure else ())
            + (
                f"the pull request branch could not be read back afterwards ({exc}), "
                f"so whether {push_target.ref} now holds {commit.sha} is unknown. Do "
                "not re-run blind: read the branch first",
            ),
            target=target,
            push_target=push_target,
            commit=commit,
            commit_created=True,
        )

    if observed == commit.sha:
        # The branch holds the fix. Whether *this run* is what put it there is
        # a separate question, and only the remote saying it applied our
        # update answers it. `= [up to date]` means the ref already held this
        # exact commit and the push moved nothing; a rejection or a missing
        # answer alongside a matching ref means something else got there.
        # Reporting any of those as "pushed by this run" would be a provenance
        # claim the evidence does not support.
        moved_the_ref = remote_said == REMOTE_ACCEPTED
        if moved_the_ref:
            attribution = (f"{git_remote} applied {refspec}",)
        elif remote_said == REMOTE_UP_TO_DATE:
            attribution = (
                f"{git_remote} reports {push_target.ref} was already up to date: "
                "the branch already held this exact fix, and this run did not move "
                "the ref",
            )
        else:
            attribution = (
                f"{push_target.ref} holds {commit.sha}, but {git_remote} gave no "
                "answer establishing that this run's push is what put it there",
            )
        if push_failure is not None:
            attribution = attribution + (
                f"git push reported a failure ({push_failure}) but "
                f"{push_target.ref} reads back as {commit.sha}",
            )
        return _wait_for_ci(
            client=client,
            handoff=handoff,
            push_target=push_target,
            pushed_sha=commit.sha,
            commit=commit,
            push_performed=moved_the_ref,
            already_pushed=not moved_the_ref,
            prior_reasons=created
            + (f"{push_target.ref} reads back as {commit.sha}",)
            + attribution,
            ci_timeout=ci_timeout,
            ci_poll_seconds=ci_poll_seconds,
            clock=clock,
            sleep=sleep,
        )

    # The ref is not our commit. That single observation is compatible with
    # two opposite histories -- the push never landed, or it landed and the
    # branch moved on again -- and only asking git which one settles it. An
    # earlier version skipped this and read "push reported a failure, and the
    # ref is not ours" as proof of no-write; that is wrong exactly when a
    # lost response is followed by someone else's commit, which is the case an
    # operator most needs told correctly.
    landed = contains_commit(
        repo_root,
        remote=git_remote,
        branch=push_target.branch,
        commit=commit.sha,
        tip=observed,
        timeout=git_timeout,
    )

    if landed is True:
        return _result(
            PushOutcome.CI_STALE_TARGET,
            created
            + ((f"the push reported: {push_failure}",) if push_failure else ())
            + (
                f"{push_target.ref} reads back as {observed}, and {commit.sha} is an "
                "ancestor of it: the push DID land, and the branch has since moved "
                "on. This run's fix is in the branch's history but is not its head, "
                "so CI for it is not evidence about the pull request's present "
                "state",
            ),
            target=target,
            push_target=push_target,
            commit=commit,
            pushed_sha=commit.sha,
            push_performed=True,
            commit_created=True,
        )

    if remote_said == REMOTE_REJECTED and landed is not True:
        # The remote *answered*, and its answer was "no". That is the only
        # evidence that establishes a no-write after the fact, and it is why
        # this branch requires it.
        #
        # An absent commit does not establish it on its own: a commit can land
        # and then be erased from the branch's history, and nothing observable
        # afterwards separates that from a push that never happened. An earlier
        # version reported `repository_mutated: false` on absence alone, which
        # was a machine-readable claim of proof that the evidence did not
        # support.
        return _result(
            PushOutcome.PUSH_FAILED,
            created
            + (
                f"the push was refused: {push_failure}",
                f"{remote_said_label(git_remote)} rejected {refspec} outright, so nothing "
                "this run created was written. The fix commit existed only in a "
                "workspace this run removed",
                f"{push_target.ref} reads back as {observed}"
                + (
                    " (still the reviewed head)"
                    if observed == target.head_sha
                    else f", which is not the reviewed head {target.head_sha} either"
                ),
            ),
            target=target,
            push_target=push_target,
            commit=commit,
            commit_created=True,
        )

    if remote_said == REMOTE_ACCEPTED:
        # The remote accepted the ref update and the branch no longer shows
        # it, and git cannot find it in the history either: it landed and the
        # branch was rewritten. Mutated, and not present -- both true, and the
        # report says both.
        return _result(
            PushOutcome.CI_STALE_TARGET,
            created
            + ((f"the push reported: {push_failure}",) if push_failure else ())
            + (
                f"{git_remote} accepted {refspec}, but {push_target.ref} now reads "
                f"back as {observed} and {commit.sha} is not in its history: the "
                "push landed and the branch has since been rewritten",
            ),
            target=target,
            push_target=push_target,
            commit=commit,
            pushed_sha=commit.sha,
            push_performed=True,
            commit_created=True,
        )

    # Everything else is genuinely unknown, and is reported as unknown: a push
    # that exited zero without moving the ref, a local hook or a dropped
    # connection that left the remote with no per-ref answer to give, or an
    # ancestry question git could not answer at all. In every one of these the
    # commit may or may not have reached the remote, and saying either would
    # be a guess wearing a machine-readable field.
    return _result(
        PushOutcome.PUSH_NOT_VERIFIED,
        created
        + (
            (f"the push reported: {push_failure}",)
            if push_failure
            else ("git push reported success",)
        )
        + (
            f"{git_remote} gave no per-ref answer for {refspec}, so whether it "
            "reached the remote at all is not established",
        )
        + (
            f"{push_target.ref} reads back as {observed}, not the {commit.sha} this "
            "run created"
            + (
                ", and whether this run's commit is in its history could not be "
                "determined"
                if landed is None
                else ", and this run's commit is not in its history -- which is "
                "compatible both with a push that never landed and with one that "
                "landed and was then erased"
            )
            + ". Remote state is not known: do not re-run blind, read the branch "
            "first",
        ),
        target=target,
        push_target=push_target,
        commit=commit,
        commit_created=True,
    )


# --------------------------------------------------------------------------
# Authoritative CI for the exact pushed commit
# --------------------------------------------------------------------------


def _wait_for_ci(
    *,
    client,
    handoff: FixHandoff,
    push_target: PushTarget,
    pushed_sha: str,
    commit: FixCommit | None,
    push_performed: bool,
    already_pushed: bool,
    prior_reasons: tuple[str, ...],
    ci_timeout: float,
    ci_poll_seconds: float,
    clock,
    sleep,
) -> PushResult:
    """Wait, boundedly, for authoritative CI on exactly ``pushed_sha``.

    The wait is over a *pull request*, not over a commit in isolation, and
    that is the point: :func:`review_loop.runner.verify_pull_request` resolves
    the pull request's own head and evaluates that. So requiring its target to
    equal ``pushed_sha`` is what binds the CI evidence to this fix rather than
    to whatever the branch holds by the time the answer arrives.
    """
    target = handoff.target
    deadline = clock() + ci_timeout
    failures = 0
    polls = 0

    def done(outcome: PushOutcome, reasons, evaluation=None, verified=None):
        return PushResult(
            outcome=outcome,
            reasons=prior_reasons + tuple(reasons),
            target=target,
            push_target=push_target,
            commit=commit,
            pushed_sha=pushed_sha,
            push_performed=push_performed,
            already_pushed=already_pushed,
            commit_created=commit is not None,
            ci_evaluation=evaluation,
            ci_polls=polls,
            verified_target=verified,
        )

    while True:
        polls += 1
        evaluation = verify_pull_request(client, target.number)

        if evaluation.verdict is Verdict.API_ERROR:
            failures += 1
            if failures >= MAX_CI_API_FAILURES:
                return done(
                    PushOutcome.CI_API_ERROR,
                    (
                        f"GitHub could not be queried {failures} times in a row while "
                        f"waiting for CI on {pushed_sha}",
                    )
                    + evaluation.reasons,
                    evaluation,
                )
            if not _wait(clock, sleep, deadline, ci_poll_seconds):
                return done(
                    PushOutcome.CI_API_ERROR,
                    (
                        f"GitHub could not be queried while waiting for CI on "
                        f"{pushed_sha}, and the {ci_timeout:g}s wait elapsed",
                    )
                    + evaluation.reasons,
                    evaluation,
                )
            continue
        failures = 0

        observed_head = evaluation.target.head_sha if evaluation.target else None

        if observed_head != pushed_sha:
            # The pull request is not (yet) at the pushed commit. Immediately
            # after a push GitHub can still report the previous head, which is
            # lag and worth waiting out; any *other* commit is someone else's
            # push and is not something to wait for.
            if observed_head != target.head_sha:
                return done(
                    PushOutcome.CI_STALE_TARGET,
                    (
                        f"the pull request head is {observed_head}, which is neither "
                        f"the pushed fix {pushed_sha} nor the reviewed head; another "
                        "commit was pushed on top and CI for this fix is no longer "
                        "evidence about the pull request",
                    ),
                    evaluation,
                )
            if not _wait(clock, sleep, deadline, ci_poll_seconds):
                return done(
                    PushOutcome.CI_AMBIGUOUS,
                    (
                        f"{push_target.ref} holds {pushed_sha}, but after "
                        f"{ci_timeout:g}s the pull request still reports its head as "
                        f"{observed_head}. Whether CI for the pushed commit is "
                        "authoritative for this pull request cannot be decided",
                    ),
                    evaluation,
                )
            continue

        if evaluation.verdict is Verdict.PENDING:
            if not _wait(clock, sleep, deadline, ci_poll_seconds):
                return done(
                    PushOutcome.CI_PENDING,
                    (
                        f"authoritative CI for {pushed_sha} had not finished within "
                        f"{ci_timeout:g}s",
                    )
                    + evaluation.reasons,
                    evaluation,
                )
            continue

        if evaluation.verdict is Verdict.FAILED:
            return done(
                PushOutcome.CI_FAILED,
                (f"authoritative CI for the pushed fix {pushed_sha} failed",)
                + evaluation.reasons,
                evaluation,
            )
        if evaluation.verdict is Verdict.STALE_TARGET:
            return done(
                PushOutcome.CI_STALE_TARGET,
                (
                    f"the pushed fix {pushed_sha} is on the branch, but its CI no "
                    "longer describes the pull request's current merge context",
                )
                + evaluation.reasons,
                evaluation,
            )
        if evaluation.verdict is not Verdict.READY:
            return done(
                PushOutcome.CI_AMBIGUOUS,
                (
                    f"CI state for the pushed fix {pushed_sha} could not be "
                    "determined safely",
                )
                + evaluation.reasons,
                evaluation,
            )

        # READY. Everything below is the merge-context currency requirement
        # stated as its own check rather than inferred from the verdict: the
        # next stage is a re-review, and it may only start from a pull request
        # whose green CI describes the merge that exists now.
        try:
            verified = from_evaluation(target.repo, evaluation)
        except TargetNotVerified as exc:  # pragma: no cover - READY implies both
            return done(
                PushOutcome.CI_AMBIGUOUS,
                (
                    f"CI reported READY for {pushed_sha} but the verified merge "
                    f"context could not be captured: {exc}",
                ),
                evaluation,
            )
        if (
            evaluation.base_tip_at_verification is None
            or verified.ci_merge_base_sha != evaluation.base_tip_at_verification
        ):
            return done(
                PushOutcome.CI_STALE_TARGET,
                (
                    f"authoritative CI for {pushed_sha} tested it merged onto "
                    f"{verified.ci_merge_base_sha}, which is not the current "
                    f"{target.base_ref} tip "
                    f"({evaluation.base_tip_at_verification or 'unknown'})",
                ),
                evaluation,
            )

        return done(
            PushOutcome.PUSH_READY,
            (
                f"authoritative CI for the pushed fix {pushed_sha} is READY, tested "
                f"merged onto the current {target.base_ref} tip "
                f"{verified.ci_merge_base_sha}",
            )
            + evaluation.reasons,
            evaluation,
            verified,
        )


def _wait(clock, sleep, deadline: float, interval: float) -> bool:
    """Sleep one interval, or report that the bounded wait is over.

    The deadline is checked *before* sleeping, so the loop always makes one
    observation, and always makes one final observation after the last sleep.
    A bounded wait that could return without ever having looked would be a
    timeout dressed up as an answer.
    """
    if clock() >= deadline:
        return False
    sleep(interval)
    return True
