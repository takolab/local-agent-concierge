"""Verdicts: only a fully explained, fully passing exact head is READY."""

import pytest

from review_loop.evaluate import evaluate
from review_loop.model import TriggerExpectation, Verdict, WorkflowDefinition
from review_loop.runs import parse_runs
from review_loop.runner import build_target
from review_loop.workflow_config import classify_workflow_files

from fakes import (
    BASELINE_PATH,
    DEFAULT_WORKFLOW_FILES,
    FILTERED_PATH,
    FULL_SHA,
    OTHER_SHA,
    SECOND_FILTERED_PATH,
    pull_request_payload,
    run_payload,
)

TARGET = build_target(pull_request_payload(number=27, head_sha=FULL_SHA), 27)
DEFINITIONS = classify_workflow_files(DEFAULT_WORKFLOW_FILES, "master")

BASELINE_SUCCESS = run_payload(run_id=1, path=BASELINE_PATH, conclusion="success")
FILTERED_SUCCESS = run_payload(
    run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
)


def _evaluate(run_payloads, *, definitions=DEFINITIONS, head_after=FULL_SHA, target=TARGET):
    return evaluate(
        target=target,
        runs=parse_runs(list(run_payloads)),
        definitions=definitions,
        head_sha_at_verification=head_after,
    )


def test_baseline_alone_succeeding_is_ready():
    """Only the unfiltered baseline ran, because the diff matched no filter."""
    assert _evaluate([BASELINE_SUCCESS]).verdict is Verdict.READY


def test_baseline_plus_a_filtered_workflow_succeeding_is_ready():
    assert _evaluate([BASELINE_SUCCESS, FILTERED_SUCCESS]).verdict is Verdict.READY


def test_the_expected_number_of_workflows_is_not_fixed():
    """Path filters make the run count vary between pull requests.

    One and two observed workflows are both READY against the same three-workflow
    configuration, so no count is ever asserted.
    """
    one = _evaluate([BASELINE_SUCCESS])
    two = _evaluate([BASELINE_SUCCESS, FILTERED_SUCCESS])

    assert one.verdict is two.verdict is Verdict.READY
    assert len(one.outcomes) == 1
    assert len(two.outcomes) == 2
    assert len(DEFINITIONS) == 3


def test_no_runs_at_all_is_ambiguous_not_ready():
    evaluation = _evaluate([])

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any(BASELINE_PATH in reason for reason in evaluation.reasons)


def test_a_missing_baseline_run_is_ambiguous_even_when_every_observed_run_passed():
    """A green filtered workflow is not evidence that the commit was built."""
    evaluation = _evaluate([FILTERED_SUCCESS])

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("baseline workflows have no run" in reason for reason in evaluation.reasons)


def test_a_missing_filtered_workflow_is_not_held_against_the_commit():
    evaluation = _evaluate([BASELINE_SUCCESS])

    observed = {o.workflow_path for o in evaluation.outcomes}
    assert SECOND_FILTERED_PATH not in observed
    assert evaluation.verdict is Verdict.READY


@pytest.mark.parametrize("status", ["queued", "requested", "waiting", "in_progress", "pending"])
def test_an_unfinished_baseline_run_is_pending(status):
    evaluation = _evaluate(
        [run_payload(run_id=1, path=BASELINE_PATH, status=status, conclusion=None)]
    )

    assert evaluation.verdict is Verdict.PENDING


def test_a_pending_filtered_workflow_blocks_a_green_baseline():
    evaluation = _evaluate(
        [
            BASELINE_SUCCESS,
            run_payload(
                run_id=2,
                workflow_id=347481064,
                path=FILTERED_PATH,
                status="in_progress",
                conclusion=None,
            ),
        ]
    )

    assert evaluation.verdict is Verdict.PENDING


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "startup_failure", "cancelled", "action_required", "stale"],
)
def test_recognised_failing_conclusions_are_failed(conclusion):
    evaluation = _evaluate([run_payload(run_id=1, path=BASELINE_PATH, conclusion=conclusion)])

    assert evaluation.verdict is Verdict.FAILED


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "success_with_warnings", None])
def test_unrecognised_or_evidence_free_conclusions_are_ambiguous_not_success(conclusion):
    """``skipped`` and ``neutral`` are not proof that this commit was built.

    This repository's workflows produce neither, so treating them as anything
    but ambiguous would mean guessing at semantics we have not observed.
    """
    evaluation = _evaluate(
        [
            run_payload(
                run_id=1, path=BASELINE_PATH, status="completed", conclusion=conclusion
            )
        ]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS


def test_an_unrecognised_status_is_ambiguous():
    evaluation = _evaluate(
        [run_payload(run_id=1, path=BASELINE_PATH, status="teleported", conclusion=None)]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS


def test_an_older_failed_attempt_does_not_block_a_newer_successful_one():
    evaluation = _evaluate(
        [
            run_payload(run_id=1, path=BASELINE_PATH, run_attempt=1, conclusion="failure"),
            run_payload(run_id=1, path=BASELINE_PATH, run_attempt=2, conclusion="success"),
        ]
    )

    assert evaluation.verdict is Verdict.READY


def test_an_older_successful_run_does_not_mask_a_newer_failed_one():
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
                conclusion="failure",
            ),
        ]
    )

    assert evaluation.verdict is Verdict.FAILED


def test_a_moved_head_is_stale_regardless_of_how_green_the_old_commit_was():
    evaluation = _evaluate([BASELINE_SUCCESS, FILTERED_SUCCESS], head_after=OTHER_SHA)

    assert evaluation.verdict is Verdict.STALE_TARGET
    assert evaluation.outcomes == ()


def test_a_workflow_identity_collision_is_ambiguous():
    evaluation = _evaluate(
        [
            run_payload(run_id=1, workflow_id=331860080, path=BASELINE_PATH),
            run_payload(run_id=2, workflow_id=999999999, path=BASELINE_PATH),
        ]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("multiple workflow ids" in reason for reason in evaluation.reasons)


def test_a_run_for_a_workflow_absent_from_the_configuration_is_ambiguous():
    evaluation = _evaluate(
        [
            BASELINE_SUCCESS,
            run_payload(
                run_id=9,
                workflow_id=1234,
                path=".github/workflows/ghost.yml",
                name="Ghost",
                conclusion="success",
            ),
        ]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("absent from the configuration" in reason for reason in evaluation.reasons)


def test_an_unreadable_trigger_block_is_ambiguous_rather_than_ignored():
    definitions = (
        WorkflowDefinition(BASELINE_PATH, "Python tests", TriggerExpectation.REQUIRED),
        WorkflowDefinition(FILTERED_PATH, "Orchestrator tests", TriggerExpectation.UNKNOWN),
    )

    evaluation = _evaluate([BASELINE_SUCCESS], definitions=definitions)

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("could not be interpreted" in reason for reason in evaluation.reasons)


def test_a_configuration_with_no_baseline_workflow_is_ambiguous():
    definitions = (
        WorkflowDefinition(FILTERED_PATH, "Orchestrator tests", TriggerExpectation.CONDITIONAL),
    )

    evaluation = _evaluate([FILTERED_SUCCESS], definitions=definitions)

    assert evaluation.verdict is Verdict.AMBIGUOUS
    assert any("no always-run baseline workflow" in reason for reason in evaluation.reasons)


def test_ambiguity_outranks_a_green_result():
    """The ordering that matters: unknown never collapses into READY."""
    evaluation = _evaluate(
        [
            BASELINE_SUCCESS,
            run_payload(
                run_id=2,
                workflow_id=347481064,
                path=FILTERED_PATH,
                conclusion="mystery",
            ),
        ]
    )

    assert evaluation.verdict is Verdict.AMBIGUOUS


def test_every_verdict_has_a_distinct_exit_code_and_only_ready_is_zero():
    codes = {verdict: _code(verdict) for verdict in Verdict}

    assert codes[Verdict.READY] == 0
    assert len(set(codes.values())) == len(Verdict)
    assert all(code != 0 for verdict, code in codes.items() if verdict is not Verdict.READY)


def _code(verdict):
    from review_loop.model import EXIT_CODES

    return EXIT_CODES[verdict]
