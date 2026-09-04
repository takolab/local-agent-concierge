"""The exact pull request state a review turn is bound to.

The head SHA alone is not the review target. A ``pull_request`` run tests the
head merged onto the base, so the verified state is a *merge context*: this
head, onto this base commit, with this CI evidence. PR #28 established that
distinction for starting a review; this snapshot carries it across the
reviewer's run so the same claim can be re-checked before anything is
recorded.

There is no generic snapshot machinery here. This is one frozen record of the
five facts that make a review record truthful.
"""

from __future__ import annotations

from dataclasses import dataclass

from .evaluate import AUTHORITATIVE_EVENT
from .model import CiEvaluation, Verdict, require_full_sha


@dataclass(frozen=True)
class ReviewTarget:
    """A verified, READY review target, captured before the reviewer starts."""

    repo: str
    number: int
    head_sha: str
    base_ref: str
    #: The base commit CI actually merged this head onto. ``None`` only if
    #: verification somehow reported READY without establishing one, which
    #: :func:`from_evaluation` refuses to build a target from.
    ci_merge_base_sha: str
    #: ``(workflow path, run id, conclusion)`` for each authoritative run, kept
    #: so the recorded comment can state what CI evidence the review rested on.
    ci_evidence: tuple[tuple[str, int, str], ...] = ()

    def __post_init__(self) -> None:
        require_full_sha(self.head_sha, label="review target head sha")
        require_full_sha(self.ci_merge_base_sha, label="review target merge base sha")


class TargetNotVerified(ValueError):
    """Raised when a review target is built from a non-READY evaluation."""


def from_evaluation(repo: str, evaluation: CiEvaluation) -> ReviewTarget:
    """Capture the review target described by a READY evaluation."""
    if evaluation.verdict is not Verdict.READY:
        raise TargetNotVerified(
            f"verification reported {evaluation.verdict.value}, not READY"
        )
    if evaluation.target is None or not evaluation.ci_merge_base_sha:
        raise TargetNotVerified(
            "verification reported READY without a resolved target and merge base"
        )

    evidence = tuple(
        (outcome.workflow_path, outcome.run.run_id, outcome.run.conclusion or "")
        for outcome in evaluation.outcomes
        if outcome.run.event == AUTHORITATIVE_EVENT
    )
    return ReviewTarget(
        repo=repo,
        number=evaluation.target.number,
        head_sha=evaluation.target.head_sha,
        base_ref=evaluation.target.base_ref,
        ci_merge_base_sha=evaluation.ci_merge_base_sha,
        ci_evidence=evidence,
    )


def drift_reasons(original: ReviewTarget, current: ReviewTarget) -> tuple[str, ...]:
    """Describe how a freshly verified target differs from the reviewed one.

    An empty result means the review still describes the current state. The
    merge base is compared as well as the head: the base branch can advance,
    CI can re-run green against the new merge, and verification will report
    READY again -- for a merge context nobody reviewed.
    """
    reasons: list[str] = []
    if current.number != original.number:
        reasons.append(
            f"the target is now pull request #{current.number}, not #{original.number}"
        )
    if current.head_sha != original.head_sha:
        reasons.append(
            f"the head moved from {original.head_sha} to {current.head_sha} while the "
            "reviewer was running"
        )
    if current.base_ref != original.base_ref:
        reasons.append(
            f"the base branch changed from {original.base_ref!r} to {current.base_ref!r}"
        )
    if current.ci_merge_base_sha != original.ci_merge_base_sha:
        reasons.append(
            f"CI now validates this head merged onto {current.ci_merge_base_sha}, not "
            f"{original.ci_merge_base_sha} as when the review started"
        )
    return tuple(reasons)
