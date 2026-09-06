"""The Bounded Re-Review contract: what a fresh reviewer must answer, and how.

A re-review turn asks two questions about one pushed fix commit, and this
module exists mostly to keep them apart:

1. **Did each original finding get resolved?** That is a historical claim
   about specific, named findings a previous reviewer raised against an
   earlier commit.
2. **Does the pull request, as it now stands, contain findings?** That is a
   fresh review of the current state, which nobody has reviewed before.

Collapsing the two would destroy information in both directions. "F1 is
resolved" stays true when a new Major finding appears in the fix, and "no
original finding is outstanding" is not the same claim as "there is nothing
wrong here". :class:`ReReview` therefore carries two independent collections
and never derives one from the other.

The rest is the same contract the initial review already has -- a delimited
block, a closed vocabulary, and one field that carries the whole correctness
argument. Here that field is ``Reviewed head SHA``, and it must be the exact
pushed fix commit, not the commit the findings were originally written
against.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .verdict import Finding, Recommendation, Severity

#: The re-review must appear between these two lines. A reviewer may reason
#: out loud around them; only the delimited block is ever parsed.
RE_REVIEW_BEGIN = "BEGIN BOUNDED RE-REVIEW RESPONSE v1"
RE_REVIEW_END = "END BOUNDED RE-REVIEW RESPONSE v1"

#: Round semantics for the whole loop, stated once.
#:
#: A **round is one Independent Review turn**. The initial review is round 1
#: (:data:`review_loop.verdict.SUPPORTED_ROUND`); the fix turn and the push
#: turn do not start rounds of their own -- they carry the round of the review
#: whose findings they are acting on, which is why a push handoff must report
#: ``round: 1``. The first re-review is therefore round 2: the second review
#: turn on this pull request, reading the commit the round-1 fix produced.
#:
#: A later multi-round slice increments this the same way -- fix and push for
#: round 2's findings stay round 2, and the review turn after them is round 3
#: -- so nothing here needs a second numbering scheme to grow into.
RE_REVIEW_ROUND = 2

#: Fresh findings are namespaced by the round that raised them, so an id from
#: this turn cannot be mistaken for one of the originals it is reported
#: beside. ``R2.F1`` is a fresh finding of round 2; ``F1`` is an original.
#:
#: The prefix is a convention imposed on *this* round's reviewer, not a fact
#: about round 1 -- a round-1 reviewer was free to name a finding ``R2.F1``,
#: since nothing told it not to. So the prefix is necessary but never
#: sufficient: :mod:`review_loop.rereview_validation` also checks every fresh
#: id against the actual original ids, and that check is what makes the
#: separation a guarantee rather than a naming habit.
FRESH_FINDING_PREFIX = f"R{RE_REVIEW_ROUND}."


class Resolution(Enum):
    """What a fresh reviewer says became of one original finding.

    Deliberately three values. There is no ``NEW_FINDING``: whatever the fix
    introduced is a *fresh finding*, reported in its own section, and saying
    it here would overwrite the historical fact about the original.
    """

    #: The original finding's required outcome is now true at this commit.
    RESOLVED = "RESOLVED"
    #: It is not. The finding stands, unchanged or partially addressed.
    UNRESOLVED = "UNRESOLVED"
    #: The reviewer cannot decide, or the finding no longer means what it
    #: said. A human is being asked, which is not the same as "not fixed".
    ESCALATE = "ESCALATE"


class ReReviewOutcome(Enum):
    """How one re-review turn ended.

    The vocabulary mirrors :class:`review_loop.verdict.ReviewOutcome` value
    for value where the fact is the same one, so an operator who has read the
    review turn's output does not have to learn a second set of words. The
    two that are genuinely new describe preconditions only a re-review has:
    the input must describe a *pushed fix*, and the pull request must still
    be *at* that fix.
    """

    #: A validated re-review was recorded as a new comment.
    RE_REVIEW_VALID = "RE_REVIEW_VALID"
    #: The inputs are not a validated review plus the PUSH_READY push of its
    #: fix. No GitHub request was made and no reviewer was started.
    RE_REVIEW_INPUT_INVALID = "RE_REVIEW_INPUT_INVALID"
    #: Verification did not report READY for the pull request now, so the
    #: pushed fix's CI evidence is not current. No reviewer was started.
    TARGET_NOT_READY = "TARGET_NOT_READY"
    #: The pull request is no longer at the pushed fix commit, or its merge
    #: context has moved out from under that commit's CI. No reviewer was
    #: started: there is nothing to re-review at the target.
    TARGET_NOT_AT_FIX = "TARGET_NOT_AT_FIX"
    #: The reviewer's working directory is not a clean checkout of the pushed
    #: fix, so no reviewer was started.
    REVIEWER_WORKSPACE_INVALID = "REVIEWER_WORKSPACE_INVALID"
    #: The reviewer process failed, timed out, or produced nothing usable.
    REVIEWER_FAILED = "REVIEWER_FAILED"
    #: Output could not be parsed, or failed a semantic rule of the contract.
    RE_REVIEW_MALFORMED = "RE_REVIEW_MALFORMED"
    #: The response describes a commit other than the exact pushed fix.
    RE_REVIEW_SHA_MISMATCH = "RE_REVIEW_SHA_MISMATCH"
    #: The pull request moved while the reviewer was running.
    TARGET_STALE = "TARGET_STALE"
    #: A record for this exact identity already exists; nothing was written.
    COMMENT_ALREADY_EXISTS = "COMMENT_ALREADY_EXISTS"
    #: The re-review was valid but the comment could not be created.
    GITHUB_WRITE_FAILED = "GITHUB_WRITE_FAILED"
    #: GitHub could not be queried.
    API_ERROR = "API_ERROR"


#: Exit code per outcome, in a block of their own so that no re-review
#: outcome can be confused with a verification verdict (0-20), a review
#: outcome (30-35), a fix outcome (40-51) or a push outcome (60-75).
#:
#: Zero means *a validated re-review exists for this exact pushed fix*. It
#: does **not** mean the findings were resolved, and it does not mean the
#: pull request is mergeable: what the re-review established is in its
#: content, not in its exit code.
RE_REVIEW_EXIT_CODES: dict[ReReviewOutcome, int] = {
    ReReviewOutcome.RE_REVIEW_VALID: 0,
    ReReviewOutcome.COMMENT_ALREADY_EXISTS: 0,
    ReReviewOutcome.RE_REVIEW_INPUT_INVALID: 80,
    ReReviewOutcome.TARGET_NOT_AT_FIX: 81,
    ReReviewOutcome.REVIEWER_WORKSPACE_INVALID: 82,
    ReReviewOutcome.REVIEWER_FAILED: 83,
    ReReviewOutcome.RE_REVIEW_MALFORMED: 84,
    ReReviewOutcome.RE_REVIEW_SHA_MISMATCH: 85,
    ReReviewOutcome.TARGET_STALE: 86,
    ReReviewOutcome.GITHUB_WRITE_FAILED: 87,
    ReReviewOutcome.API_ERROR: 88,
}


class ReReviewParseError(ValueError):
    """The reviewer's output is not a Bounded Re-Review Response at all."""


class ReReviewValidationError(ValueError):
    """The output parsed, but what it describes is not admissible."""


class ReReviewShaBindingError(ReReviewValidationError):
    """The response does not describe the exact pushed fix commit."""


@dataclass(frozen=True)
class FindingResolution:
    """What became of one original finding, at the pushed fix commit.

    ``evidence`` is required for every resolution, including ``RESOLVED``:
    "it is fixed" without the code, test or behaviour that shows it is an
    assertion, and this pipeline records evidence-bearing artifacts only.

    ``reason`` is required for ``UNRESOLVED`` and ``ESCALATE`` and optional
    for ``RESOLVED``. The distinction is what a human does next with it: an
    unresolved finding needs to say *why* the fix fell short, and an
    escalation needs to say what the human is being asked.
    """

    finding_id: str
    resolution: Resolution
    evidence: str
    reason: str | None = None


@dataclass(frozen=True)
class ReReview:
    """A validated re-review, bound to the exact pushed fix commit.

    The two collections are independent by construction. ``resolutions``
    answers only "what happened to the findings round 1 raised"; ``fresh``
    answers only "what is wrong with this pull request now". Neither is
    derived from the other, and no property here combines them into a single
    status.
    """

    round: int
    reviewed_head_sha: str
    recommendation: Recommendation
    resolutions: tuple[FindingResolution, ...] = ()
    fresh_findings: tuple[Finding, ...] = ()
    escalation_reason: str | None = None

    def count(self, severity: Severity) -> int:
        """How many *fresh* findings have this severity.

        Fresh only, deliberately. An unresolved original finding keeps the
        severity it was given in round 1, and that severity belongs to the
        round-1 record; counting it here would silently re-raise a finding
        this turn did not independently make.
        """
        return sum(1 for f in self.fresh_findings if f.severity is severity)

    def resolutions_with(self, resolution: Resolution) -> tuple[FindingResolution, ...]:
        return tuple(r for r in self.resolutions if r.resolution is resolution)

    @property
    def unresolved_finding_ids(self) -> tuple[str, ...]:
        return tuple(
            r.finding_id for r in self.resolutions_with(Resolution.UNRESOLVED)
        )

    @property
    def blocking_findings_remain(self) -> bool:
        """Whether this turn raised a fresh Blocking finding."""
        return self.count(Severity.BLOCKING) > 0

    @property
    def major_findings_remain(self) -> bool:
        """Whether this turn raised a fresh Major finding."""
        return self.count(Severity.MAJOR) > 0
