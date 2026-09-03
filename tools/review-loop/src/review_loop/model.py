"""Value objects shared by the review-loop runner.

The central invariant is that a commit is identified by its exact
40-character hexadecimal SHA. Abbreviated SHAs are accepted nowhere as an
identity: the GitHub Actions run listing silently returns zero runs for an
abbreviated ``head_sha`` instead of failing, so an abbreviated SHA would be
indistinguishable from "this commit has no CI".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

FULL_SHA_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")


class NotAFullShaError(ValueError):
    """Raised when a value is used as a commit identity but is not a full SHA."""


def require_full_sha(value: object, *, label: str = "sha") -> str:
    """Return ``value`` unchanged if it is an exact 40-character lowercase SHA."""
    if not isinstance(value, str) or not FULL_SHA_PATTERN.match(value):
        raise NotAFullShaError(
            f"{label} must be an exact 40-character lowercase hex SHA, got {value!r}"
        )
    return value


def short_sha(sha: str) -> str:
    """Display-only abbreviation. Never used as an identity."""
    return sha[:7]


class Verdict(Enum):
    """Whether an Independent Review may be started against the target."""

    READY = "READY"
    PENDING = "PENDING"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    STALE_TARGET = "STALE_TARGET"
    API_ERROR = "API_ERROR"


#: Exit code per verdict. READY is the only zero, so ``if review-loop ...``
#: in a shell is a correct "may I start the review?" test.
EXIT_CODES: dict[Verdict, int] = {
    Verdict.READY: 0,
    Verdict.PENDING: 10,
    Verdict.FAILED: 11,
    Verdict.AMBIGUOUS: 12,
    Verdict.STALE_TARGET: 13,
    Verdict.API_ERROR: 20,
}

#: Exit code for CLI usage errors, distinct from every verdict.
EXIT_USAGE = 2


@dataclass(frozen=True)
class PullRequestTarget:
    """The exact commit a review would be started against."""

    number: int
    head_sha: str
    base_ref: str
    head_ref: str
    state: str

    def __post_init__(self) -> None:
        require_full_sha(self.head_sha, label="pull request head sha")


@dataclass(frozen=True)
class WorkflowRun:
    """One GitHub Actions workflow run, reduced to the fields we reason about.

    ``workflow_path`` is the identity we group on. The job/check display name
    is deliberately absent: this repository runs three different workflows
    whose only job is named ``test``, so display names collide.
    """

    run_id: int
    workflow_id: int
    workflow_path: str
    workflow_name: str
    head_sha: str
    status: str
    conclusion: str | None
    run_attempt: int
    event: str
    created_at: str

    def __post_init__(self) -> None:
        require_full_sha(self.head_sha, label="workflow run head sha")


class TriggerExpectation(Enum):
    """How a workflow definition is expected to behave for a pull request."""

    #: Triggered by pull_request with no path filter: it always runs, so its
    #: absence is never explainable by the PR's diff.
    REQUIRED = "REQUIRED"
    #: Triggered by pull_request but path-filtered: absence may be legitimate.
    CONDITIONAL = "CONDITIONAL"
    #: Not triggered by pull_request for this base branch at all.
    NOT_EXPECTED = "NOT_EXPECTED"
    #: The trigger block could not be understood. Never treated as absence.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WorkflowDefinition:
    """A workflow file as configured at the exact head SHA under review."""

    path: str
    name: str
    expectation: TriggerExpectation


@dataclass(frozen=True)
class WorkflowOutcome:
    """The authoritative run selected for one workflow identity."""

    workflow_path: str
    workflow_name: str
    run: WorkflowRun
    superseded_run_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CiEvaluation:
    """The result of evaluating one exact head SHA."""

    verdict: Verdict
    reasons: tuple[str, ...]
    target: PullRequestTarget | None = None
    outcomes: tuple[WorkflowOutcome, ...] = ()
    definitions: tuple[WorkflowDefinition, ...] = field(default=())
    head_sha_at_verification: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]
