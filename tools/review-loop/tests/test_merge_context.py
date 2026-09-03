"""What a `pull_request` run actually tested, and whether it still holds.

With a plain ``actions/checkout``, a ``pull_request`` run builds
``refs/pull/N/merge`` -- the head merged onto the base -- not the head commit.
So a green run attests to a *merge context*, and that context can go stale
while the head sits perfectly still.
"""

import pytest

from review_loop.evaluate import evaluate
from review_loop.model import Verdict
from review_loop.runner import build_target
from review_loop.runs import parse_runs
from review_loop.workflow_config import classify_workflow_files

from fakes import (
    ADVANCED_BASE_TIP,
    BASE_TIP,
    BASELINE_PATH,
    DEFAULT_WORKFLOW_FILES,
    FILTERED_PATH,
    FULL_SHA,
    OTHER_SHA,
    pull_request_payload,
    run_payload,
)

DEFINITIONS = classify_workflow_files(DEFAULT_WORKFLOW_FILES, "master")
DIFF = ("README.md",)


def _target(state="open", number=27):
    return build_target(
        pull_request_payload(number=number, head_sha=FULL_SHA, state=state), number, DIFF
    )


def _evaluate(run_payloads, *, target=None, base_tip=BASE_TIP):
    return evaluate(
        target=target or _target(),
        runs=parse_runs(list(run_payloads)),
        definitions=DEFINITIONS,
        head_sha_at_verification=FULL_SHA,
        base_tip_at_verification=base_tip,
    )


def test_a_green_run_records_the_base_it_was_merged_onto():
    evaluation = _evaluate([run_payload(run_id=1, path=BASELINE_PATH)])

    assert evaluation.verdict is Verdict.READY
    assert evaluation.ci_merge_base_sha == BASE_TIP


def test_a_base_that_moved_after_ci_is_stale_even_though_the_head_did_not():
    """The second review finding: a stable head is not a stable merge.

    CI merged this head onto BASE_TIP and passed. The head never moved, but the
    base advanced, so the merge that was actually validated no longer exists.
    """
    evaluation = _evaluate(
        [run_payload(run_id=1, path=BASELINE_PATH)], base_tip=ADVANCED_BASE_TIP
    )

    assert evaluation.verdict is Verdict.STALE_TARGET
    assert evaluation.head_sha_at_verification == evaluation.target.head_sha
    assert any("no longer exists" in reason for reason in evaluation.reasons)


def test_runs_that_merged_onto_different_bases_are_ambiguous():
    evaluation = _evaluate(
        [
            run_payload(run_id=1, path=BASELINE_PATH, merge_base=BASE_TIP),
            run_payload(
                run_id=2,
                workflow_id=347481064,
                path=FILTERED_PATH,
                merge_base=ADVANCED_BASE_TIP,
            ),
        ]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("different merge bases" in reason for reason in evaluation.reasons)


def test_a_run_belonging_to_another_pull_request_is_not_evidence_for_this_one():
    """Sharing a head SHA does not make a run this pull request's evidence."""
    evaluation = _evaluate([run_payload(run_id=1, path=BASELINE_PATH, pr_number=99)])

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("not #27" in reason for reason in evaluation.reasons)


def test_a_run_with_no_pull_request_association_cannot_establish_the_context():
    evaluation = _evaluate([run_payload(run_id=1, path=BASELINE_PATH, pr_number=None)])

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert evaluation.ci_merge_base_sha is None


def test_a_closed_pull_request_is_not_a_review_target():
    """GitHub empties the run-to-pull-request association once a PR closes.

    So a merged pull request can neither be reviewed nor have its merge context
    verified, and it must not report READY.
    """
    evaluation = _evaluate(
        [run_payload(run_id=1, path=BASELINE_PATH, pr_number=None)],
        target=_target(state="closed"),
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("not a review target" in reason for reason in evaluation.reasons)


def test_a_push_run_never_supersedes_a_pull_request_run_of_the_same_workflow():
    """They test different trees, so the newer one does not simply win."""
    evaluation = _evaluate(
        [
            run_payload(
                run_id=1,
                path=BASELINE_PATH,
                created_at="2026-09-03T07:00:00Z",
                conclusion="success",
            ),
            run_payload(
                run_id=2,
                path=BASELINE_PATH,
                created_at="2026-09-03T09:00:00Z",
                event="push",
                conclusion="failure",
                pr_number=None,
            ),
        ]
    )

    assert evaluation.verdict is Verdict.READY
    events = {o.run.event: o.run.run_id for o in evaluation.outcomes}
    assert events == {"pull_request": 1, "push": 2}


def test_a_push_run_alone_cannot_satisfy_the_baseline_requirement():
    """A push run built the head tree, not the merge, so it is not evidence."""
    evaluation = _evaluate(
        [
            run_payload(
                run_id=1,
                path=BASELINE_PATH,
                event="push",
                conclusion="success",
                pr_number=None,
            )
        ]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("always runs for a pull request" in reason for reason in evaluation.reasons)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_only_pull_request_runs_are_treated_as_evidence(event):
    evaluation = _evaluate(
        [
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2,
                path=BASELINE_PATH,
                event=event,
                conclusion="failure",
                pr_number=None,
                created_at="2026-09-03T23:00:00Z",
            ),
        ]
    )

    assert evaluation.verdict is Verdict.READY


def test_a_moved_head_outranks_a_moved_base():
    evaluation = evaluate(
        target=_target(),
        runs=parse_runs([run_payload(run_id=1, path=BASELINE_PATH)]),
        definitions=DEFINITIONS,
        head_sha_at_verification=OTHER_SHA,
        base_tip_at_verification=ADVANCED_BASE_TIP,
    )

    assert evaluation.verdict is Verdict.STALE_TARGET
    assert any("head moved" in reason for reason in evaluation.reasons)
