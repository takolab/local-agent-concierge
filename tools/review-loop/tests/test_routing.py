"""The handoff from a review turn to a fix turn, re-validated on the way in.

The point of these tests is that the handoff is *not* trusted because it came
from this runner. Every invariant the verdict validator enforces is enforced
again here, so a hand-edited or truncated document cannot route a fix that
the review contract would never have produced.
"""

import json

import pytest

from review_loop.routing import RoutingInputError, load_handoff
from review_loop.verdict import Recommendation, Severity

from fix_fakes import FULL_SHA, OTHER_SHA, finding, review_json


def edited(document: str, mutate) -> str:
    payload = json.loads(document)
    mutate(payload)
    return json.dumps(payload)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_validated_review_round_trips():
    handoff = load_handoff(review_json(finding("F1")))

    assert handoff.target.head_sha == FULL_SHA
    assert handoff.target.number == 29
    assert handoff.verdict.recommendation is Recommendation.CHANGES_REQUESTED
    assert handoff.verdict.open_findings[0].finding_id == "F1"
    assert handoff.verdict.open_findings[0].severity is Severity.MAJOR


def test_an_approved_review_round_trips_with_no_findings():
    handoff = load_handoff(review_json())

    assert handoff.verdict.recommendation is Recommendation.APPROVED
    assert handoff.verdict.open_findings == ()


def test_an_already_recorded_review_is_routable():
    """COMMENT_ALREADY_EXISTS still carries a validated verdict."""
    handoff = load_handoff(review_json(finding(), outcome="COMMENT_ALREADY_EXISTS"))

    assert handoff.review_outcome == "COMMENT_ALREADY_EXISTS"


def test_the_optional_scope_boundary_survives():
    document = review_json(finding(scope_boundary="do not touch the CLI"))

    assert load_handoff(document).verdict.open_findings[0].scope_boundary == (
        "do not touch the CLI"
    )


# --------------------------------------------------------------------------
# Not a validated review
# --------------------------------------------------------------------------


def test_text_that_is_not_json_is_refused():
    with pytest.raises(RoutingInputError, match="not JSON"):
        load_handoff("## Independent AI Review\n\nMajor - F1\n")


def test_a_json_array_is_refused():
    with pytest.raises(RoutingInputError, match="not a JSON object"):
        load_handoff("[]")


@pytest.mark.parametrize(
    "outcome",
    ["REVIEW_MALFORMED", "TARGET_STALE", "REVIEWER_FAILED", "TARGET_NOT_READY"],
)
def test_a_review_that_did_not_produce_a_verdict_is_refused(outcome):
    with pytest.raises(RoutingInputError, match="review outcome"):
        load_handoff(review_json(finding(), outcome=outcome))


def test_a_document_with_no_target_is_refused():
    document = edited(review_json(finding()), lambda p: p.pop("target"))

    with pytest.raises(RoutingInputError, match="missing the required field 'target'"):
        load_handoff(document)


def test_a_document_with_no_verdict_is_refused():
    document = edited(review_json(finding()), lambda p: p.pop("verdict"))

    with pytest.raises(RoutingInputError, match="missing the required field 'verdict'"):
        load_handoff(document)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_an_abbreviated_head_sha_is_refused_rather_than_resolved():
    document = edited(
        review_json(finding()),
        lambda p: p["target"].update(head_sha=FULL_SHA[:12]),
    )

    with pytest.raises(RoutingInputError, match="40-character"):
        load_handoff(document)


def test_an_uppercase_sha_is_refused():
    document = edited(
        review_json(finding()),
        lambda p: p["target"].update(head_sha=FULL_SHA.upper()),
    )

    with pytest.raises(RoutingInputError, match="40-character"):
        load_handoff(document)


def test_a_verdict_describing_another_commit_than_its_target_is_refused():
    """The review and the state it describes must be the same commit."""
    document = review_json(finding(), reviewed_head_sha=OTHER_SHA)

    with pytest.raises(RoutingInputError, match="must be the same commit"):
        load_handoff(document)


def test_a_repository_mismatch_with_the_operators_flag_is_refused():
    with pytest.raises(RoutingInputError, match="--repo says"):
        load_handoff(review_json(finding()), expected_repo="someone/else")


def test_a_matching_repository_flag_is_accepted():
    handoff = load_handoff(
        review_json(finding()), expected_repo="takolab/local-agent-concierge"
    )

    assert handoff.target.repo == "takolab/local-agent-concierge"


def test_a_repository_that_is_not_owner_slash_name_is_refused():
    with pytest.raises(RoutingInputError, match="owner/name"):
        load_handoff(review_json(finding(), repo="local-agent-concierge"))


@pytest.mark.parametrize("number", [0, -1, "29", 29.0, True])
def test_a_pull_request_number_that_is_not_a_positive_integer_is_refused(number):
    document = edited(review_json(finding()), lambda p: p["target"].update(number=number))

    with pytest.raises(RoutingInputError, match="positive integer"):
        load_handoff(document)


def test_a_later_round_is_refused():
    """Re-review is a later slice, so a round-2 handoff is unsupported input."""
    with pytest.raises(RoutingInputError, match="round 2"):
        load_handoff(review_json(finding(), round=2))


# --------------------------------------------------------------------------
# The findings themselves
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["finding_id", "severity", "location", "problem", "evidence", "required_outcome"]
)
def test_a_finding_missing_a_required_field_is_refused(field):
    document = edited(
        review_json(finding()),
        lambda p: p["verdict"]["open_findings"][0].pop(field),
    )

    with pytest.raises(RoutingInputError):
        load_handoff(document)


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_finding_with_an_empty_required_field_is_refused(value):
    document = edited(
        review_json(finding()),
        lambda p: p["verdict"]["open_findings"][0].update(problem=value),
    )

    with pytest.raises(RoutingInputError):
        load_handoff(document)


def test_an_unknown_severity_is_refused():
    document = edited(
        review_json(finding()),
        lambda p: p["verdict"]["open_findings"][0].update(severity="Critical"),
    )

    with pytest.raises(RoutingInputError, match="unknown severity"):
        load_handoff(document)


def test_a_finding_id_outside_the_contracts_pattern_is_refused():
    document = edited(
        review_json(finding()),
        lambda p: p["verdict"]["open_findings"][0].update(finding_id="F 1; rm -rf /"),
    )

    with pytest.raises(RoutingInputError, match="not a usable finding id"):
        load_handoff(document)


def test_duplicate_finding_ids_are_refused():
    document = review_json(finding("F1"), finding("F1", location="a/b.py"))

    with pytest.raises(RoutingInputError, match="more than once"):
        load_handoff(document)


def test_an_oversized_field_is_refused():
    document = review_json(finding(problem="x" * 5000))

    with pytest.raises(RoutingInputError, match="above the"):
        load_handoff(document)


def test_an_unknown_recommendation_is_refused():
    document = edited(
        review_json(finding()),
        lambda p: p["verdict"].update(recommendation="looks fine to me"),
    )

    with pytest.raises(RoutingInputError, match="unknown recommendation"):
        load_handoff(document)


# --------------------------------------------------------------------------
# The review contract's own coherence rules, re-applied
# --------------------------------------------------------------------------


def test_approved_with_findings_is_refused():
    document = review_json(finding(), recommendation="approved")

    with pytest.raises(RoutingInputError, match="'approved' while reporting"):
        load_handoff(document)


def test_changes_requested_with_no_finding_is_refused():
    document = review_json(recommendation="changes_requested")

    with pytest.raises(RoutingInputError, match="reports no open finding"):
        load_handoff(document)


def test_changes_requested_with_a_blocking_finding_is_refused():
    """A Blocking finding always escalates; the review contract says so."""
    document = review_json(
        finding(severity=Severity.BLOCKING), recommendation="changes_requested"
    )

    with pytest.raises(RoutingInputError, match="always escalates"):
        load_handoff(document)


def test_an_escalating_verdict_loads_and_carries_its_reason():
    document = review_json(
        recommendation="escalate", escalation_reason="the design decision is unclear"
    )

    handoff = load_handoff(document)

    assert handoff.verdict.recommendation is Recommendation.ESCALATE
    assert handoff.verdict.escalation_reason == "the design decision is unclear"
