"""Reducing several runs for one commit to one authoritative run per workflow."""

import pytest

from review_loop.model import NotAFullShaError
from review_loop.runs import (
    RunParseError,
    WorkflowIdentityCollision,
    normalize,
    parse_run,
    parse_runs,
)

from fakes import BASELINE_PATH, FILTERED_PATH, FULL_SHA, run_payload


def _normalize(*payloads):
    return normalize(parse_runs(list(payloads)))


def test_no_runs_normalize_to_no_outcomes():
    assert _normalize() == ()


def test_workflows_are_distinguished_by_path_even_when_job_names_are_identical():
    """All three workflows in this repository expose a single job named ``test``.

    Grouping on a display name would collapse them into one outcome, so the
    file path is the identity.
    """
    outcomes = _normalize(
        run_payload(run_id=1, workflow_id=331860080, path=BASELINE_PATH, name="Python tests"),
        run_payload(
            run_id=2, workflow_id=347481064, path=FILTERED_PATH, name="Orchestrator tests"
        ),
    )

    assert [o.workflow_path for o in outcomes] == [FILTERED_PATH, BASELINE_PATH]
    assert len({o.run.workflow_id for o in outcomes}) == 2


def test_a_newer_successful_attempt_supersedes_an_older_failed_attempt():
    outcomes = _normalize(
        run_payload(run_id=100, run_attempt=1, status="completed", conclusion="failure"),
        run_payload(run_id=100, run_attempt=2, status="completed", conclusion="success"),
    )

    assert len(outcomes) == 1
    assert outcomes[0].run.run_attempt == 2
    assert outcomes[0].run.conclusion == "success"


def test_attempt_order_in_the_payload_does_not_change_the_winner():
    outcomes = _normalize(
        run_payload(run_id=100, run_attempt=2, status="completed", conclusion="success"),
        run_payload(run_id=100, run_attempt=1, status="completed", conclusion="failure"),
    )

    assert outcomes[0].run.run_attempt == 2


@pytest.mark.parametrize(
    "newer_status, newer_conclusion",
    [("in_progress", None), ("completed", "failure"), ("queued", None)],
    ids=["newer-in-progress", "newer-failed", "newer-queued"],
)
def test_an_older_successful_run_never_hides_a_newer_one(newer_status, newer_conclusion):
    outcomes = _normalize(
        run_payload(
            run_id=100,
            created_at="2026-09-03T07:00:00Z",
            status="completed",
            conclusion="success",
        ),
        run_payload(
            run_id=101,
            created_at="2026-09-03T09:00:00Z",
            status=newer_status,
            conclusion=newer_conclusion,
        ),
    )

    assert len(outcomes) == 1
    assert outcomes[0].run.run_id == 101
    assert outcomes[0].superseded_run_ids == (100,)


def test_runs_of_different_workflows_do_not_supersede_each_other():
    outcomes = _normalize(
        run_payload(
            run_id=100,
            path=BASELINE_PATH,
            created_at="2026-09-03T07:00:00Z",
            conclusion="success",
        ),
        run_payload(
            run_id=101,
            workflow_id=347481064,
            path=FILTERED_PATH,
            created_at="2026-09-03T09:00:00Z",
            conclusion="failure",
        ),
    )

    assert {o.workflow_path: o.run.conclusion for o in outcomes} == {
        BASELINE_PATH: "success",
        FILTERED_PATH: "failure",
    }


def test_one_path_reported_under_two_workflow_ids_is_a_collision():
    with pytest.raises(WorkflowIdentityCollision):
        _normalize(
            run_payload(run_id=1, workflow_id=331860080, path=BASELINE_PATH),
            run_payload(run_id=2, workflow_id=999999999, path=BASELINE_PATH),
        )


def test_a_run_carrying_an_abbreviated_head_sha_is_rejected():
    with pytest.raises(NotAFullShaError):
        parse_run(run_payload(run_id=1, head_sha=FULL_SHA[:7]))


@pytest.mark.parametrize(
    "field", ["id", "workflow_id", "path", "name", "head_sha", "status", "run_attempt"]
)
def test_a_run_missing_a_field_we_reason_about_is_rejected(field):
    payload = run_payload(run_id=1)
    payload[field] = None

    with pytest.raises(RunParseError):
        parse_run(payload)


def test_a_pending_run_may_legitimately_have_no_conclusion():
    run = parse_run(run_payload(run_id=1, status="in_progress", conclusion=None))

    assert run.conclusion is None
    assert run.status == "in_progress"
