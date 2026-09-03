"""Decide whether an Independent Review may be started against an exact SHA.

The evaluator is deliberately biased: anything it cannot explain becomes
``AMBIGUOUS`` rather than collapsing into ``READY``. A wrong ``READY`` starts a
review against a commit whose CI state is not actually known, which is the one
failure mode this slice exists to prevent.
"""

from __future__ import annotations

from .model import (
    CiEvaluation,
    PullRequestTarget,
    TriggerExpectation,
    Verdict,
    WorkflowDefinition,
    WorkflowOutcome,
    WorkflowRun,
    short_sha,
)
from .runs import WorkflowIdentityCollision, normalize

#: A completed run with any of these conclusions is passing CI evidence.
SUCCESS_CONCLUSIONS = frozenset({"success"})

#: Completed conclusions that block a review. ``cancelled`` and
#: ``action_required`` are included because neither demonstrates a passing
#: build of this commit, and ``stale`` marks a run GitHub itself no longer
#: considers valid.
FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "cancelled", "action_required", "stale"}
)

#: Statuses that mean the run has not finished yet.
PENDING_STATUSES = frozenset({"queued", "requested", "waiting", "in_progress", "pending"})

_COMPLETED = "completed"


def _classify_outcome(outcome: WorkflowOutcome) -> tuple[str, str]:
    """Return ``(bucket, human reason)`` for one authoritative run.

    ``bucket`` is one of ``success``, ``pending``, ``failed``, ``ambiguous``.
    ``skipped`` and ``neutral`` land in ``ambiguous`` on purpose: neither is
    evidence that this commit was actually built, and this repository's
    workflows do not produce them, so encountering one means the runner's model
    of CI is out of date.
    """
    run = outcome.run
    label = f"{outcome.workflow_path} (run {run.run_id} attempt {run.run_attempt})"

    if run.status != _COMPLETED:
        if run.status in PENDING_STATUSES:
            return "pending", f"{label} is {run.status}"
        return "ambiguous", f"{label} has an unrecognised status {run.status!r}"

    if run.conclusion in SUCCESS_CONCLUSIONS:
        return "success", f"{label} succeeded"
    if run.conclusion in FAILURE_CONCLUSIONS:
        return "failed", f"{label} concluded {run.conclusion}"
    return "ambiguous", f"{label} has an unrecognised conclusion {run.conclusion!r}"


def evaluate(
    target: PullRequestTarget,
    runs: tuple[WorkflowRun, ...],
    definitions: tuple[WorkflowDefinition, ...],
    head_sha_at_verification: str,
) -> CiEvaluation:
    """Evaluate one pull request head against its observed Actions runs."""

    # 1. The head must not have moved while we were looking at it. Everything
    #    below describes a commit that would no longer be the review target.
    if head_sha_at_verification != target.head_sha:
        return CiEvaluation(
            verdict=Verdict.STALE_TARGET,
            reasons=(
                f"head moved from {short_sha(target.head_sha)} to "
                f"{short_sha(head_sha_at_verification)} during verification",
            ),
            target=target,
            definitions=definitions,
            head_sha_at_verification=head_sha_at_verification,
        )

    ambiguous: list[str] = []

    # 2. Guard the query contract: every run must belong to the exact commit.
    foreign = sorted({run.head_sha for run in runs if run.head_sha != target.head_sha})
    if foreign:
        ambiguous.append(
            "the Actions API returned runs for other commits: "
            + ", ".join(short_sha(sha) for sha in foreign)
        )

    try:
        outcomes = normalize(runs)
    except WorkflowIdentityCollision as exc:
        return CiEvaluation(
            verdict=Verdict.AMBIGUOUS,
            reasons=(str(exc),),
            target=target,
            definitions=definitions,
            head_sha_at_verification=head_sha_at_verification,
        )

    configured_paths = {definition.path for definition in definitions}
    observed_paths = {outcome.workflow_path for outcome in outcomes}

    # 3. The workflow configuration must be understandable.
    unknown = sorted(
        d.path for d in definitions if d.expectation is TriggerExpectation.UNKNOWN
    )
    if unknown:
        ambiguous.append(
            "the pull_request trigger could not be interpreted for: " + ", ".join(unknown)
        )

    # 4. Runs and configuration must describe the same set of workflows.
    unconfigured = sorted(observed_paths - configured_paths)
    if unconfigured:
        ambiguous.append(
            "runs exist for workflows absent from the configuration at this commit: "
            + ", ".join(unconfigured)
        )

    # 5. A baseline workflow must exist and must have produced evidence. Only
    #    unfiltered workflows qualify: a path-filtered workflow's absence is
    #    explainable by the diff, so it can never anchor the verdict.
    required = [d for d in definitions if d.expectation is TriggerExpectation.REQUIRED]
    if not required:
        ambiguous.append(
            "no always-run baseline workflow could be identified at this commit"
        )
    missing_required = sorted(d.path for d in required if d.path not in observed_paths)
    if missing_required:
        ambiguous.append(
            "baseline workflows have no run for this commit: " + ", ".join(missing_required)
        )

    classified = [_classify_outcome(outcome) for outcome in outcomes]
    ambiguous.extend(reason for bucket, reason in classified if bucket == "ambiguous")
    failed = [reason for bucket, reason in classified if bucket == "failed"]
    pending = [reason for bucket, reason in classified if bucket == "pending"]
    succeeded = [reason for bucket, reason in classified if bucket == "success"]

    if ambiguous:
        verdict, reasons = Verdict.AMBIGUOUS, ambiguous
    elif failed:
        verdict, reasons = Verdict.FAILED, failed
    elif pending:
        verdict, reasons = Verdict.PENDING, pending
    else:
        verdict, reasons = Verdict.READY, succeeded

    return CiEvaluation(
        verdict=verdict,
        reasons=tuple(reasons),
        target=target,
        outcomes=outcomes,
        definitions=definitions,
        head_sha_at_verification=head_sha_at_verification,
    )
