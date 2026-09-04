"""Semantic rules a parsed verdict must satisfy to be recordable."""

import pytest

from review_loop.verdict import (
    Recommendation,
    Severity,
    ShaBindingError,
    VerdictValidationError,
)
from review_loop.verdict_parser import parse
from review_loop.verdict_validation import validate

from fakes import FULL_SHA, OTHER_SHA, verdict_text

MAJOR = {
    "Finding ID": "F1",
    "Severity": "Major",
    "Location": "a.py:1",
    "Problem": "p",
    "Evidence": "e",
    "Required outcome": "r",
}
MINOR = {**MAJOR, "Finding ID": "F2", "Severity": "Minor"}
BLOCKING = {**MAJOR, "Finding ID": "F3", "Severity": "Blocking"}


def _validate(target=FULL_SHA, **kwargs):
    return validate(parse(verdict_text(**kwargs)), target_head_sha=target)


# --- SHA binding -----------------------------------------------------------


def test_the_exact_forty_character_sha_is_accepted():
    verdict = _validate()

    assert verdict.reviewed_head_sha == FULL_SHA


def test_an_abbreviated_sha_is_rejected_rather_than_resolved():
    with pytest.raises(ShaBindingError, match="not the exact"):
        _validate(head_sha=FULL_SHA[:7])


def test_a_different_full_sha_is_rejected():
    with pytest.raises(ShaBindingError):
        _validate(head_sha=OTHER_SHA)


def test_a_malformed_sha_is_rejected():
    with pytest.raises(ShaBindingError):
        _validate(head_sha="not-a-sha")


def test_an_uppercase_sha_is_rejected_rather_than_normalised():
    with pytest.raises(ShaBindingError):
        _validate(head_sha=FULL_SHA.upper())


def test_a_missing_reviewed_head_sha_is_a_plain_validation_error():
    """Absent is malformed; present-but-wrong is a binding failure."""
    text = verdict_text().replace(f"Reviewed head SHA: {FULL_SHA}\n", "")

    with pytest.raises(VerdictValidationError) as caught:
        validate(parse(text), target_head_sha=FULL_SHA)

    assert not isinstance(caught.value, ShaBindingError)


# --- round -----------------------------------------------------------------


def test_round_one_is_accepted():
    assert _validate().round == 1


@pytest.mark.parametrize("value", [2, 0, -1, "one", ""])
def test_any_other_round_is_rejected(value):
    with pytest.raises(VerdictValidationError):
        _validate(round_number=value)


# --- recommendation and finding consistency --------------------------------


def test_approved_with_no_findings_is_accepted():
    verdict = _validate(recommendation="approved", findings=())

    assert verdict.recommendation is Recommendation.APPROVED
    assert verdict.open_findings == ()


def test_changes_requested_with_a_major_finding_is_accepted():
    verdict = _validate(findings=(MAJOR,))

    assert verdict.count(Severity.MAJOR) == 1


def test_changes_requested_with_a_minor_finding_is_accepted():
    verdict = _validate(findings=(MINOR,))

    assert verdict.count(Severity.MINOR) == 1


def test_escalate_with_a_blocking_finding_is_accepted():
    verdict = _validate(recommendation="escalate", findings=(BLOCKING,))

    assert verdict.count(Severity.BLOCKING) == 1


def test_escalate_without_findings_needs_a_stated_reason():
    verdict = _validate(
        recommendation="escalate",
        findings=(),
        escalation_reason="The diff references a service I cannot read.",
    )

    assert verdict.escalation_reason.startswith("The diff references")


def test_escalate_with_neither_a_finding_nor_a_reason_is_rejected():
    with pytest.raises(VerdictValidationError, match="what is being escalated"):
        _validate(recommendation="escalate", findings=())


def test_approved_with_a_major_finding_is_rejected():
    with pytest.raises(VerdictValidationError, match="approval with findings"):
        _validate(recommendation="approved", findings=(MAJOR,))


def test_approved_with_a_minor_finding_is_rejected():
    with pytest.raises(VerdictValidationError, match="approval with findings"):
        _validate(recommendation="approved", findings=(MINOR,))


def test_changes_requested_with_no_findings_is_rejected():
    with pytest.raises(VerdictValidationError, match="nothing to change"):
        _validate(recommendation="changes_requested", findings=())


def test_a_blocking_finding_may_not_be_routed_as_a_bounded_fix():
    """This project's standing decision: Blocking always escalates."""
    with pytest.raises(VerdictValidationError, match="always escalates"):
        _validate(recommendation="changes_requested", findings=(BLOCKING,))


def test_an_unknown_recommendation_is_rejected():
    with pytest.raises(VerdictValidationError, match="unknown recommendation"):
        _validate(recommendation="looks_fine_to_me")


def test_recommendation_case_and_spacing_are_normalised():
    verdict = _validate(recommendation="Changes Requested")

    assert verdict.recommendation is Recommendation.CHANGES_REQUESTED


# --- findings --------------------------------------------------------------


def test_an_unknown_severity_is_rejected():
    with pytest.raises(VerdictValidationError, match="unknown severity"):
        _validate(findings=({**MAJOR, "Severity": "Critical"},))


def test_severity_case_is_normalised():
    verdict = _validate(findings=({**MAJOR, "Severity": "major"},))

    assert verdict.open_findings[0].severity is Severity.MAJOR


def test_duplicate_finding_ids_are_rejected():
    with pytest.raises(VerdictValidationError, match="more than once"):
        _validate(findings=(MAJOR, {**MINOR, "Finding ID": "F1"}))


def test_an_empty_finding_id_is_rejected():
    with pytest.raises(VerdictValidationError, match="empty 'Finding ID'"):
        _validate(findings=({**MAJOR, "Finding ID": ""},))


@pytest.mark.parametrize(
    "label", ["Severity", "Location", "Problem", "Evidence", "Required outcome"]
)
def test_every_required_finding_field_must_be_present_and_non_empty(label):
    with pytest.raises(VerdictValidationError, match=label):
        _validate(findings=({**MAJOR, label: ""},))


def test_a_finding_without_evidence_is_not_a_trusted_finding():
    missing = {k: v for k, v in MAJOR.items() if k != "Evidence"}

    with pytest.raises(VerdictValidationError, match="Evidence"):
        _validate(findings=(missing,))


def test_scope_boundary_is_optional():
    verdict = _validate(findings=(MAJOR,))

    assert verdict.open_findings[0].scope_boundary is None


def test_a_finding_id_that_is_not_a_plain_token_is_rejected():
    with pytest.raises(VerdictValidationError, match="not a usable finding id"):
        _validate(findings=({**MAJOR, "Finding ID": "F 1 <script>"},))


def test_a_field_may_not_carry_html_comment_syntax():
    """Otherwise reviewer text could forge the record's identity marker."""
    forged = {
        **MAJOR,
        "Problem": "see <!-- local-agent-concierge:independent-review:v1 --> below",
    }

    with pytest.raises(VerdictValidationError, match="machine marker"):
        _validate(findings=(forged,))


def test_an_oversized_field_is_rejected():
    with pytest.raises(VerdictValidationError, match="above the"):
        _validate(findings=({**MAJOR, "Problem": "x" * 5000},))


def test_too_many_findings_are_rejected():
    findings = tuple({**MAJOR, "Finding ID": f"F{n}"} for n in range(60))

    with pytest.raises(VerdictValidationError, match="above the 50"):
        _validate(findings=findings)


# --- resolved --------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "(none)", "none", "-"])
def test_an_empty_resolved_list_is_accepted_in_round_one(value):
    assert _validate(resolved=value).resolved_finding_ids == ()


def test_resolved_findings_in_round_one_are_rejected():
    with pytest.raises(VerdictValidationError, match="no earlier round"):
        _validate(resolved="F1, F2")
