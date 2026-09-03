"""Decide whether an Independent Review may be started against an exact SHA.

The evaluator is deliberately biased: anything it cannot explain becomes
``AMBIGUOUS`` rather than collapsing into ``READY``. A wrong ``READY`` starts a
review against a commit whose CI state is not actually known, which is the one
failure mode this slice exists to prevent.

Two things make "this exact commit's CI is green" narrower than it sounds, and
both are handled here rather than assumed away:

* With a plain ``actions/checkout``, a ``pull_request`` run does not test the
  head commit. It tests ``refs/pull/N/merge`` -- the head merged onto the base
  at that moment. The evidence is therefore about a *merge context*, and that
  context goes stale when the base branch moves even though the head has not.
* A path-filtered workflow that produced no run is only harmless if this pull
  request's own diff misses its filter. "A filter exists" is not the same
  claim as "this diff misses it".
"""

from __future__ import annotations

from . import path_filter
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

#: Only runs from this event are evidence for the gate. A ``push`` or
#: ``workflow_dispatch`` run on the same commit tests the head tree rather than
#: the merge, so it is reported but never used to satisfy or override.
AUTHORITATIVE_EVENT = "pull_request"

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


def _merge_context(
    target: PullRequestTarget,
    outcomes: list[WorkflowOutcome],
    base_tip: str | None,
) -> tuple[str | None, list[str], list[str]]:
    """Establish which base commit the observed runs were merged onto.

    Returns ``(merge base, ambiguous reasons, stale reasons)``.
    """
    ambiguous: list[str] = []
    stale: list[str] = []

    for outcome in outcomes:
        numbers = outcome.run.pull_request_numbers
        if not numbers:
            ambiguous.append(
                f"{outcome.workflow_path} (run {outcome.run.run_id}) carries no pull "
                "request association, so the merge context it tested is unknown"
            )
        elif target.number not in numbers:
            ambiguous.append(
                f"{outcome.workflow_path} (run {outcome.run.run_id}) belongs to pull "
                f"request(s) {list(numbers)}, not #{target.number}"
            )

    bases = sorted({sha for outcome in outcomes for sha in outcome.run.merge_base_shas})
    if len(bases) > 1:
        ambiguous.append(
            "the observed runs tested different merge bases: "
            + ", ".join(short_sha(sha) for sha in bases)
        )
        return None, ambiguous, stale
    if not bases:
        return None, ambiguous, stale

    merge_base = bases[0]
    if base_tip and merge_base != base_tip:
        stale.append(
            f"CI merged this head onto {short_sha(merge_base)}, but {target.base_ref} is "
            f"now at {short_sha(base_tip)}, so the tested merge no longer exists"
        )
    return merge_base, ambiguous, stale


def _unexplained_absences(
    definitions: tuple[WorkflowDefinition, ...],
    observed_paths: set[str],
    target: PullRequestTarget,
) -> tuple[list[str], list[str]]:
    """Find workflows that produced no run without a legitimate explanation."""
    ambiguous: list[str] = []
    explained: list[str] = []

    for definition in definitions:
        if definition.path in observed_paths:
            continue
        if definition.expectation is TriggerExpectation.REQUIRED:
            ambiguous.append(
                f"{definition.path} always runs for a pull request but has no run "
                "for this commit"
            )
        elif definition.expectation is TriggerExpectation.CONDITIONAL:
            outcome = path_filter.evaluate(definition.path_filter, target.changed_files)
            if outcome is path_filter.FilterOutcome.MATCHES:
                ambiguous.append(
                    f"{definition.path} is path-filtered but this diff matches its "
                    "filter, yet it has no run for this commit"
                )
            elif outcome is path_filter.FilterOutcome.UNDECIDABLE:
                ambiguous.append(
                    f"{definition.path} has no run and its path filter could not be "
                    "evaluated against this diff, so the absence is unexplained"
                )
            else:
                explained.append(
                    f"{definition.path} did not run: this diff misses its path filter"
                )
    return ambiguous, explained


def evaluate(
    target: PullRequestTarget,
    runs: tuple[WorkflowRun, ...],
    definitions: tuple[WorkflowDefinition, ...],
    head_sha_at_verification: str,
    base_tip_at_verification: str | None = None,
) -> CiEvaluation:
    """Evaluate one pull request head against its observed Actions runs."""

    def result(verdict, reasons, outcomes=(), merge_base=None):
        return CiEvaluation(
            verdict=verdict,
            reasons=tuple(reasons),
            target=target,
            outcomes=tuple(outcomes),
            definitions=definitions,
            head_sha_at_verification=head_sha_at_verification,
            ci_merge_base_sha=merge_base,
            base_tip_at_verification=base_tip_at_verification,
        )

    # 1. The head must not have moved while we were looking at it. Everything
    #    below describes a commit that would no longer be the review target.
    if head_sha_at_verification != target.head_sha:
        return result(
            Verdict.STALE_TARGET,
            [
                f"head moved from {short_sha(target.head_sha)} to "
                f"{short_sha(head_sha_at_verification)} during verification"
            ],
        )

    ambiguous: list[str] = []
    stale: list[str] = []

    # 2. A closed pull request is not a review target, and GitHub drops the
    #    run-to-pull-request association once it closes, so the merge context
    #    could not be established for one anyway.
    if target.state != "open":
        ambiguous.append(
            f"pull request #{target.number} is {target.state or 'in an unknown state'}, "
            "so it is not a review target and its merge context cannot be verified"
        )

    # 3. Guard the query contract: every run must belong to the exact commit.
    foreign = sorted({run.head_sha for run in runs if run.head_sha != target.head_sha})
    if foreign:
        ambiguous.append(
            "the Actions API returned runs for other commits: "
            + ", ".join(short_sha(sha) for sha in foreign)
        )

    try:
        outcomes = normalize(runs)
    except WorkflowIdentityCollision as exc:
        return result(Verdict.AMBIGUOUS, [str(exc)])

    # 4. Only pull_request runs are evidence. A push or workflow_dispatch run
    #    on the same commit tested the head tree rather than the merge, so it
    #    is still displayed but cannot satisfy or override the gate.
    authoritative = [o for o in outcomes if o.run.event == AUTHORITATIVE_EVENT]

    merge_base, context_ambiguous, context_stale = _merge_context(
        target, authoritative, base_tip_at_verification
    )
    ambiguous.extend(context_ambiguous)
    stale.extend(context_stale)

    configured_paths = {definition.path for definition in definitions}
    observed_paths = {outcome.workflow_path for outcome in authoritative}

    # 5. The workflow configuration must be understandable.
    unknown = sorted(
        d.path for d in definitions if d.expectation is TriggerExpectation.UNKNOWN
    )
    if unknown:
        ambiguous.append(
            "the pull_request trigger could not be interpreted for: " + ", ".join(unknown)
        )

    # 6. Runs and configuration must describe the same set of workflows.
    unconfigured = sorted({o.workflow_path for o in outcomes} - configured_paths)
    if unconfigured:
        ambiguous.append(
            "runs exist for workflows absent from the configuration at this commit: "
            + ", ".join(unconfigured)
        )

    # 7. Every absent workflow needs an explanation, and "a filter exists" is
    #    not one. A baseline workflow must also exist to anchor the verdict.
    if not any(d.expectation is TriggerExpectation.REQUIRED for d in definitions):
        ambiguous.append(
            "no always-run baseline workflow could be identified at this commit"
        )
    absence_ambiguous, explained = _unexplained_absences(
        definitions, observed_paths, target
    )
    ambiguous.extend(absence_ambiguous)

    classified = [_classify_outcome(outcome) for outcome in authoritative]
    ambiguous.extend(reason for bucket, reason in classified if bucket == "ambiguous")
    failed = [reason for bucket, reason in classified if bucket == "failed"]
    pending = [reason for bucket, reason in classified if bucket == "pending"]
    succeeded = [reason for bucket, reason in classified if bucket == "success"]

    if stale:
        verdict, reasons = Verdict.STALE_TARGET, stale
    elif ambiguous:
        verdict, reasons = Verdict.AMBIGUOUS, ambiguous
    elif failed:
        verdict, reasons = Verdict.FAILED, failed
    elif pending:
        verdict, reasons = Verdict.PENDING, pending
    else:
        verdict, reasons = Verdict.READY, succeeded + explained

    return result(verdict, reasons, outcomes, merge_base)
