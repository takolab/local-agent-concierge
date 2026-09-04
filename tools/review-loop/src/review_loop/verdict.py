"""The Structured Verdict contract an Independent Reviewer must satisfy.

A reviewer's output is untrusted input. It arrives as text produced by a
process this runner does not control, describing a code review this runner
cannot itself perform. Nothing here tries to understand that text: the
contract is a fixed vocabulary of labels, and anything outside it is a
malformed verdict rather than a verdict to be interpreted charitably.

The one field that carries the whole correctness argument is
``reviewed_head_sha``. A review is only evidence about the exact commit it
read, so a verdict whose SHA is abbreviated, absent, or merely close to the
target is not weaker evidence -- it is evidence about an unknown commit, and
is rejected outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: The verdict must appear between these two lines. A reviewer is free to
#: think out loud around them: only the delimited block is ever parsed, and
#: only validated fields from it are ever rendered into a comment.
VERDICT_BEGIN = "BEGIN INDEPENDENT REVIEW VERDICT v1"
VERDICT_END = "END INDEPENDENT REVIEW VERDICT v1"

#: The only round this slice supports. Re-review is a later slice, and a
#: verdict claiming to be one is unsupported input, not a verdict to record.
SUPPORTED_ROUND = 1

#: Bounds on a single verdict. These are not tuning knobs: an unreasonable
#: value means the output is not the thing the contract describes.
MAX_FINDINGS = 50
MAX_FIELD_CHARS = 4000
MAX_LOCATION_CHARS = 500
MAX_FINDING_ID_CHARS = 64

#: GitHub rejects an issue comment body above this length.
MAX_COMMENT_CHARS = 65536


class Severity(Enum):
    """The three severities the review design admits. No others exist."""

    BLOCKING = "Blocking"
    MAJOR = "Major"
    MINOR = "Minor"


class Recommendation(Enum):
    """What the reviewer says should happen to the pull request."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    ESCALATE = "escalate"


class ReviewOutcome(Enum):
    """How one review turn ended.

    Failure modes stay distinct because they call for different responses: a
    malformed verdict is a reviewer-contract problem, a stale target is a
    timing problem, and an existing comment is not a problem at all.
    """

    #: A validated verdict was recorded as a new comment.
    REVIEW_VALID = "REVIEW_VALID"
    #: Verification did not report READY, so no reviewer was started.
    TARGET_NOT_READY = "TARGET_NOT_READY"
    #: The reviewer process failed, timed out, or produced nothing usable.
    REVIEWER_FAILED = "REVIEWER_FAILED"
    #: The reviewer's working directory is not a clean checkout of the target,
    #: so no reviewer was started. Distinct from REVIEWER_FAILED because
    #: nothing ran: the fault is in the workspace, not in the reviewer.
    REVIEWER_WORKSPACE_INVALID = "REVIEWER_WORKSPACE_INVALID"
    #: Output could not be parsed, or failed a semantic rule of the contract.
    REVIEW_MALFORMED = "REVIEW_MALFORMED"
    #: The verdict describes a commit other than the exact review target.
    REVIEW_SHA_MISMATCH = "REVIEW_SHA_MISMATCH"
    #: The target changed while the reviewer was running.
    TARGET_STALE = "TARGET_STALE"
    #: A record for this exact identity already exists; nothing was written.
    COMMENT_ALREADY_EXISTS = "COMMENT_ALREADY_EXISTS"
    #: The verdict was valid but the comment could not be created.
    GITHUB_WRITE_FAILED = "GITHUB_WRITE_FAILED"
    #: GitHub could not be queried.
    API_ERROR = "API_ERROR"


#: Exit code per outcome. ``TARGET_NOT_READY`` is absent on purpose: it
#: reports the underlying verification verdict's own exit code, so the
#: existing "why is this not ready?" vocabulary is not duplicated.
REVIEW_EXIT_CODES: dict[ReviewOutcome, int] = {
    ReviewOutcome.REVIEW_VALID: 0,
    ReviewOutcome.COMMENT_ALREADY_EXISTS: 0,
    ReviewOutcome.API_ERROR: 20,
    ReviewOutcome.REVIEWER_FAILED: 30,
    ReviewOutcome.REVIEWER_WORKSPACE_INVALID: 35,
    ReviewOutcome.REVIEW_MALFORMED: 31,
    ReviewOutcome.REVIEW_SHA_MISMATCH: 32,
    ReviewOutcome.TARGET_STALE: 33,
    ReviewOutcome.GITHUB_WRITE_FAILED: 34,
}


class VerdictParseError(ValueError):
    """The reviewer's output is not a Structured Verdict at all."""


class VerdictValidationError(ValueError):
    """The output parsed, but the verdict it describes is not admissible."""


class ShaBindingError(VerdictValidationError):
    """The verdict does not describe the exact commit that was reviewed.

    Separate from every other validation failure because it is the one that
    would attach a real review to the wrong commit rather than record nothing.
    """


@dataclass(frozen=True)
class Finding:
    """One open finding. Every field here is required and non-empty.

    ``evidence`` is required rather than optional: a finding without it is an
    assertion, and this runner records reviews as evidence-bearing artifacts.
    """

    finding_id: str
    severity: Severity
    location: str
    problem: str
    evidence: str
    required_outcome: str
    scope_boundary: str | None = None


@dataclass(frozen=True)
class ReviewVerdict:
    """A validated verdict, bound to the exact commit it reviewed."""

    round: int
    reviewed_head_sha: str
    recommendation: Recommendation
    open_findings: tuple[Finding, ...] = ()
    resolved_finding_ids: tuple[str, ...] = ()
    escalation_reason: str | None = None

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.open_findings if f.severity is severity)
