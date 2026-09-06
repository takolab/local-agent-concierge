"""How one push turn ended, and what it says about repository state.

This is the first stage in the review loop that can change something a human
did not do themselves, so the outcome vocabulary has one job the earlier ones
did not: **every value must answer "what is now different?" without being
read alongside anything else.** An operator reading a single line of output
after an interrupted run has to be able to tell "nothing happened" from "a
commit is on your branch" from "I do not know".

Three groups, and the boundary between them is the push:

* **Before any write** -- ``PUSH_INPUT_INVALID`` through ``PUSH_FAILED``.
  **This run performed no repository write.** That is deliberately narrower
  than "the branch is where it was": another actor can move it at any moment,
  and after a refused push it very often has. What is established is this
  runner's own inaction, which is the only part it can prove. A commit may
  have been created inside a throwaway worktree that this run then removed,
  which is not repository state and is never reported as though it were.
* **After a verified write** -- ``PUSH_READY`` and every ``CI_*`` value. The
  fix commit is on the pull request branch, and
  :attr:`PushResult.pushed_sha` names it. What differs between them is only
  what CI then said.
* **Mutated beyond authority** -- ``PUSH_WROTE_UNEXPECTED_REFS`` alone. The
  remote reported writing a ref nobody asked for. Something was written for
  certain, and more than the boundary permits.
* **Unknown** -- ``PUSH_NOT_VERIFIED`` alone. ``git push`` ran and the remote
  does not show the expected commit. This is not "the push failed": the
  remote may have taken it and answered late, or something else may have
  moved the ref. It is called out separately because it is the one state a
  human has to resolve by looking, and collapsing it into either neighbour
  would be a lie in one direction or the other.

The CI values deliberately reuse the verification vocabulary
:mod:`review_loop.model` already defines -- READY, FAILED, PENDING,
STALE_TARGET, AMBIGUOUS -- rather than inventing a second set of words for
the same facts. A ``CI_`` prefix marks which side of the push they describe.
"""

from __future__ import annotations

from enum import Enum


class PushOutcome(Enum):
    """How one push turn ended."""

    # -- after a verified push ---------------------------------------------

    #: The fix commit is on the pull request branch and authoritative CI for
    #: that exact commit is READY against the current merge context. This is
    #: the only outcome from which a fresh Independent Re-Review may start.
    PUSH_READY = "PUSH_READY"
    #: Authoritative CI for the exact pushed commit failed.
    CI_FAILED = "CI_FAILED"
    #: Authoritative CI for the exact pushed commit had not finished within
    #: the bounded wait. The commit is pushed; the answer is not in yet.
    CI_PENDING = "CI_PENDING"
    #: The pull request moved off the pushed commit, or the merge context CI
    #: tested is no longer current. The push happened; its CI is not evidence
    #: about the pull request's present state.
    CI_STALE_TARGET = "CI_STALE_TARGET"
    #: CI state for the pushed commit could not be determined safely.
    CI_AMBIGUOUS = "CI_AMBIGUOUS"
    #: GitHub could not be queried while waiting for CI. The push is verified;
    #: its CI is unobserved.
    CI_API_ERROR = "CI_API_ERROR"

    # -- unknown -----------------------------------------------------------

    #: ``git push`` was attempted and reading the remote ref back afterwards
    #: did not show the created commit. Remote state is not known.
    PUSH_NOT_VERIFIED = "PUSH_NOT_VERIFIED"

    # -- mutated beyond what was authorised --------------------------------

    #: The remote reported *updating* a ref this runner did not ask for -- a
    #: tag carried along by ``push.followTags``, or anything else. Something
    #: was certainly written, and more than the one ref the write boundary
    #: permits, so the run stops and a human looks rather than continuing to
    #: CI on the strength of the branch alone. A ref the remote merely
    #: mentioned and demonstrably did not update is not this: the flag on the
    #: report line decides, and this outcome means an established write.
    PUSH_WROTE_UNEXPECTED_REFS = "PUSH_WROTE_UNEXPECTED_REFS"

    # -- before any write --------------------------------------------------

    #: ``--dry-run``: the candidate patch was verified and applied in a
    #: throwaway worktree, and nothing was committed or pushed.
    PUSH_PREPARED = "PUSH_PREPARED"
    #: The push input is not a validated candidate patch this runner produced.
    PUSH_INPUT_INVALID = "PUSH_INPUT_INVALID"
    #: The branch this runner would push to could not be established safely:
    #: a fork head, a closed pull request, the default branch, or a name that
    #: is not an ordinary branch name.
    PUSH_BRANCH_REFUSED = "PUSH_BRANCH_REFUSED"
    #: The pull request is no longer at the reviewed head, or its branch is
    #: somewhere this runner cannot account for. A new fix turn is needed.
    PUSH_TARGET_STALE = "PUSH_TARGET_STALE"
    #: The patch is not the validated candidate patch, or does not apply to
    #: the reviewed head, or applying it produced something else.
    PATCH_IDENTITY_MISMATCH = "PATCH_IDENTITY_MISMATCH"
    #: The workspace was not a clean checkout of the reviewed head, or the
    #: commit that was created is not exactly the candidate patch.
    COMMIT_REFUSED = "COMMIT_REFUSED"
    #: ``git push`` was refused by the remote, which is what establishes
    #: that this run wrote nothing. Says nothing about where the branch is
    #: now -- another actor may have moved it, and often has.
    PUSH_FAILED = "PUSH_FAILED"
    #: The workspace could not be prepared or verified. Nothing ran.
    PUSH_WORKSPACE_INVALID = "PUSH_WORKSPACE_INVALID"
    #: GitHub could not be queried before the push. Nothing was written.
    PUSH_API_ERROR = "PUSH_API_ERROR"


#: Outcomes after which the pull request branch is verified to hold the fix
#: commit. Everything not listed here either left the branch untouched or --
#: for ``PUSH_NOT_VERIFIED`` alone -- left it in a state this runner does not
#: know. There is no third list on purpose: ``mutated``, ``not mutated`` and
#: ``unknown`` are the only honest answers.
PUSHED_OUTCOMES = frozenset(
    {
        PushOutcome.PUSH_READY,
        PushOutcome.CI_FAILED,
        PushOutcome.CI_PENDING,
        PushOutcome.CI_STALE_TARGET,
        PushOutcome.CI_AMBIGUOUS,
        PushOutcome.CI_API_ERROR,
    }
)

#: Exit code per outcome, in a block of their own so that no push outcome can
#: be confused with a verification verdict (0-20), a review outcome (30-35) or
#: a fix outcome (40-51).
#:
#: Zero means *the fix commit is on the branch and its CI is green against the
#: current merge context* -- the one state a re-review may start from -- or,
#: for a dry run, that the candidate patch verified.
PUSH_EXIT_CODES: dict[PushOutcome, int] = {
    PushOutcome.PUSH_READY: 0,
    PushOutcome.PUSH_PREPARED: 0,
    PushOutcome.PUSH_INPUT_INVALID: 60,
    PushOutcome.PUSH_BRANCH_REFUSED: 61,
    PushOutcome.PUSH_TARGET_STALE: 62,
    PushOutcome.PATCH_IDENTITY_MISMATCH: 63,
    PushOutcome.COMMIT_REFUSED: 64,
    PushOutcome.PUSH_FAILED: 65,
    PushOutcome.PUSH_NOT_VERIFIED: 66,
    PushOutcome.CI_FAILED: 67,
    PushOutcome.CI_PENDING: 68,
    PushOutcome.CI_STALE_TARGET: 69,
    PushOutcome.CI_AMBIGUOUS: 70,
    PushOutcome.PUSH_WORKSPACE_INVALID: 71,
    PushOutcome.PUSH_API_ERROR: 72,
    PushOutcome.CI_API_ERROR: 73,
    PushOutcome.PUSH_WROTE_UNEXPECTED_REFS: 74,
}

#: How long the runner waits for authoritative CI on the pushed commit, and
#: how often it asks. Both are bounded: an unbounded wait would turn a failure
#: to observe into a hang, which is the one failure mode that produces no
#: report at all.
DEFAULT_CI_TIMEOUT_SECONDS = 1800.0
DEFAULT_CI_POLL_SECONDS = 20.0
