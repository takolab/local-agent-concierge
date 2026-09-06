"""Whether a parsed re-review is admissible, with the two facts kept apart."""

import pytest

from review_loop.rereview import (
    RE_REVIEW_ROUND,
    ReReviewShaBindingError,
    ReReviewValidationError,
    Resolution,
)
from review_loop.rereview_parser import parse
from review_loop.rereview_validation import validate
from review_loop.verdict import MAX_FIELD_CHARS, Severity

from fakes import OTHER_SHA
from rereview_fakes import (
    LATER_SHA,
    PUSHED_SHA,
    fresh_block,
    rereview_text,
    resolution_block,
)

ORIGINALS = ("F1", "F2")


def _validate(text, *, head_sha=PUSHED_SHA, originals=ORIGINALS):
    return validate(parse(text), target_head_sha=head_sha, original_finding_ids=originals)


def _resolved_both(**kwargs):
    return rereview_text(
        resolutions=(resolution_block("F1"), resolution_block("F2")), **kwargs
    )


# --- the envelope -----------------------------------------------------------


def test_a_well_formed_re_review_validates():
    rereview = _validate(_resolved_both())

    assert rereview.round == RE_REVIEW_ROUND
    assert rereview.reviewed_head_sha == PUSHED_SHA
    assert rereview.recommendation.value == "approved"


def test_a_re_review_of_another_commit_is_a_binding_failure():
    with pytest.raises(ReReviewShaBindingError, match="pushed fix SHA"):
        _validate(_resolved_both(head_sha=LATER_SHA))


def test_an_abbreviated_sha_is_a_binding_failure_not_a_near_miss():
    with pytest.raises(ReReviewShaBindingError):
        _validate(_resolved_both(head_sha=PUSHED_SHA[:12]))


def test_a_missing_sha_is_a_validation_failure():
    text = _resolved_both().replace(f"Reviewed head SHA: {PUSHED_SHA}\n", "")
    with pytest.raises(ReReviewValidationError, match="Reviewed head SHA"):
        _validate(text)


@pytest.mark.parametrize("round_number", [1, 3])
def test_only_the_first_re_review_round_is_accepted(round_number):
    with pytest.raises(ReReviewValidationError, match="reports round"):
        _validate(_resolved_both(round_number=round_number))


def test_a_non_numeric_round_is_refused():
    with pytest.raises(ReReviewValidationError, match="not a number"):
        _validate(_resolved_both(round_number="two"))


def test_an_unknown_recommendation_is_refused():
    with pytest.raises(ReReviewValidationError, match="unknown recommendation"):
        _validate(_resolved_both(recommendation="looks_fine"))


# --- original finding resolution -------------------------------------------


def test_every_original_finding_appears_exactly_once():
    rereview = _validate(_resolved_both())

    assert [r.finding_id for r in rereview.resolutions] == ["F1", "F2"]


def test_an_omitted_original_finding_is_refused():
    text = rereview_text(resolutions=(resolution_block("F1"),))
    with pytest.raises(ReReviewValidationError, match="no resolution for original"):
        _validate(text)


def test_a_duplicated_original_finding_is_refused():
    text = rereview_text(
        resolutions=(resolution_block("F1"), resolution_block("F1"), resolution_block("F2"))
    )
    with pytest.raises(ReReviewValidationError, match="resolved more than once"):
        _validate(text)


def test_an_unknown_original_finding_id_is_refused():
    text = rereview_text(
        resolutions=(resolution_block("F1"), resolution_block("F2"), resolution_block("F9"))
    )
    with pytest.raises(ReReviewValidationError, match="not one of the original"):
        _validate(text)


def test_an_unknown_resolution_word_is_refused():
    text = rereview_text(
        resolutions=(resolution_block("F1", "PARTIAL"), resolution_block("F2"))
    )
    with pytest.raises(ReReviewValidationError, match="unknown resolution"):
        _validate(text)


def test_new_finding_is_not_a_resolution_of_an_original_finding():
    # The vocabulary has no way to say it, which is the point: a new problem
    # is a fresh finding, reported in its own section.
    text = rereview_text(
        resolutions=(resolution_block("F1", "NEW_FINDING"), resolution_block("F2"))
    )
    with pytest.raises(ReReviewValidationError, match="unknown resolution"):
        _validate(text)


def test_a_resolved_finding_needs_evidence():
    text = rereview_text(
        resolutions=(resolution_block("F1", evidence=""), resolution_block("F2"))
    )
    with pytest.raises(ReReviewValidationError, match="empty 'Evidence'"):
        _validate(text)


def test_an_unresolved_finding_needs_a_reason_as_well_as_evidence():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(
            resolution_block("F1", "UNRESOLVED", evidence="the handler still returns 200"),
            resolution_block("F2"),
        ),
    )
    with pytest.raises(ReReviewValidationError, match="without a 'Reason'"):
        _validate(text)


def test_an_escalated_finding_needs_a_reason():
    text = rereview_text(
        recommendation="escalate",
        resolutions=(
            resolution_block("F1", "ESCALATE", evidence="the module was deleted"),
            resolution_block("F2"),
        ),
    )
    with pytest.raises(ReReviewValidationError, match="without a 'Reason'"):
        _validate(text)


def test_a_resolved_finding_may_omit_the_reason():
    rereview = _validate(_resolved_both())

    assert rereview.resolutions[0].reason is None
    assert rereview.resolutions[0].evidence


def test_a_resolution_field_longer_than_the_limit_is_refused():
    text = rereview_text(
        resolutions=(
            resolution_block("F1", evidence="x" * (MAX_FIELD_CHARS + 1)),
            resolution_block("F2"),
        )
    )
    with pytest.raises(ReReviewValidationError, match="above the"):
        _validate(text)


def test_resolution_text_may_not_forge_the_record_marker():
    text = rereview_text(
        resolutions=(
            resolution_block("F1", evidence="<!-- sneaky -->"),
            resolution_block("F2"),
        )
    )
    with pytest.raises(ReReviewValidationError, match="machine marker"):
        _validate(text)


# --- fresh findings ---------------------------------------------------------


def test_zero_fresh_findings_is_a_valid_answer():
    rereview = _validate(_resolved_both())

    assert rereview.fresh_findings == ()
    assert rereview.count(Severity.MAJOR) == 0
    assert not rereview.fresh_blocking_findings_present


@pytest.mark.parametrize(
    "severity,recommendation",
    [("Minor", "changes_requested"), ("Major", "changes_requested"), ("Blocking", "escalate")],
)
def test_fresh_findings_of_every_severity_are_accepted(severity, recommendation):
    text = rereview_text(
        recommendation=recommendation,
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block(severity=severity),),
    )
    rereview = _validate(text)

    assert rereview.fresh_findings[0].severity.value == severity
    assert rereview.count(Severity(severity)) == 1


def test_a_fresh_finding_validates_through_the_review_contracts_own_rules():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block(severity="Catastrophic"),),
    )
    with pytest.raises(ReReviewValidationError, match="unknown severity"):
        _validate(text)


def test_a_fresh_finding_missing_a_required_field_is_refused():
    block = [line for line in fresh_block() if not line.startswith("Required outcome:")]
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(block,),
    )
    with pytest.raises(ReReviewValidationError, match="Required outcome"):
        _validate(text)


def test_a_duplicate_fresh_finding_id_is_refused():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block("R2.F1"), fresh_block("R2.F1")),
    )
    with pytest.raises(ReReviewValidationError, match="appears more than once"):
        _validate(text)


def test_a_fresh_finding_must_be_namespaced_by_its_round():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block("F7"),),
    )
    with pytest.raises(ReReviewValidationError, match="does not begin with 'R2.'"):
        _validate(text)


def test_the_namespace_prefix_alone_does_not_identify_a_finding():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block("R2."),),
    )
    with pytest.raises(ReReviewValidationError, match="prefix and nothing else"):
        _validate(text)


def test_a_fresh_finding_cannot_reuse_an_original_finding_id():
    # The namespace prefix constrains this round's reviewer only. A round-1
    # reviewer was free to name a finding 'R2.F1', so the collision is checked
    # against the actual original ids as well.
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("R2.F1"), resolution_block("F2")),
        fresh=(fresh_block("R2.F1"),),
    )
    with pytest.raises(ReReviewValidationError, match="also an original finding id"):
        _validate(text, originals=("R2.F1", "F2"))


# --- evidence separation ----------------------------------------------------


def test_a_resolved_original_and_a_fresh_major_are_both_preserved():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(
            resolution_block("F1"),
            resolution_block("F2"),
        ),
        fresh=(fresh_block("R2.F1", severity="Major"),),
    )
    rereview = _validate(text)

    # Both facts, independently readable. The fresh Major does not turn F1
    # back into an unresolved finding.
    assert [r.finding_id for r in rereview.resolutions_with(Resolution.RESOLVED)] == [
        "F1",
        "F2",
    ]
    assert rereview.unresolved_finding_ids == ()
    assert [f.finding_id for f in rereview.fresh_findings] == ["R2.F1"]
    assert rereview.fresh_major_findings_present


def test_an_unresolved_original_with_no_fresh_finding_invents_nothing():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(
            resolution_block(
                "F1", "UNRESOLVED", evidence="still returns 200", reason="untouched"
            ),
            resolution_block("F2"),
        ),
    )
    rereview = _validate(text)

    assert rereview.unresolved_finding_ids == ("F1",)
    assert rereview.fresh_findings == ()
    assert not rereview.fresh_major_findings_present


def test_fresh_severity_counts_never_include_unresolved_originals():
    # F1 was a Major in round 1 and is still unresolved. That severity belongs
    # to the round-1 record; this turn's counts describe its own findings.
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(
            resolution_block("F1", "UNRESOLVED", evidence="unchanged", reason="untouched"),
            resolution_block("F2"),
        ),
    )
    rereview = _validate(text)

    assert rereview.count(Severity.MAJOR) == 0
    assert rereview.unresolved_finding_ids == ("F1",)


# --- recommendation coherence ----------------------------------------------


def test_approved_with_an_unresolved_original_is_refused():
    text = rereview_text(
        recommendation="approved",
        resolutions=(
            resolution_block("F1", "UNRESOLVED", evidence="unchanged", reason="untouched"),
            resolution_block("F2"),
        ),
    )
    with pytest.raises(ReReviewValidationError, match="not RESOLVED"):
        _validate(text)


def test_approved_with_a_fresh_finding_is_refused():
    text = rereview_text(
        recommendation="approved",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block(),),
    )
    with pytest.raises(ReReviewValidationError, match="fresh finding"):
        _validate(text)


def test_changes_requested_with_nothing_outstanding_is_refused():
    with pytest.raises(ReReviewValidationError, match="nothing to change"):
        _validate(_resolved_both(recommendation="changes_requested"))


def test_a_fresh_blocking_finding_requires_escalate():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block(severity="Blocking"),),
    )
    with pytest.raises(ReReviewValidationError, match="always escalates"):
        _validate(text)


def test_an_escalated_resolution_requires_escalate():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(
            resolution_block("F1", "ESCALATE", evidence="module gone", reason="ask a human"),
            resolution_block("F2"),
        ),
    )
    with pytest.raises(ReReviewValidationError, match="not a change to request"):
        _validate(text)


def test_escalate_with_nothing_to_escalate_is_refused():
    with pytest.raises(ReReviewValidationError, match="what is being escalated"):
        _validate(_resolved_both(recommendation="escalate"))


@pytest.mark.parametrize("recommendation", ["approved", "changes_requested"])
def test_an_escalation_reason_without_escalating_is_refused(recommendation):
    # The prompt says the field is for escalating and says the rules are
    # enforced mechanically. A document claiming both "nothing needs doing"
    # and "a human is being asked something" contradicts itself, and would
    # otherwise become a durable comment saying both.
    text = rereview_text(
        recommendation=recommendation,
        resolutions=(
            resolution_block("F1"),
            resolution_block(
                "F2", "UNRESOLVED", evidence="unchanged", reason="untouched"
            )
            if recommendation == "changes_requested"
            else resolution_block("F2"),
        ),
        escalation_reason="the pull request was rewritten under me",
    )
    with pytest.raises(ReReviewValidationError, match="Escalation reason"):
        _validate(text)


def test_an_empty_escalation_reason_is_not_treated_as_one():
    # Only a non-empty reason is a reason; a blank line is the field's absence.
    text = _resolved_both().replace(
        "Recommendation: approved", "Recommendation: approved\nEscalation reason:   "
    )
    rereview = _validate(text)

    assert rereview.escalation_reason is None


def test_escalate_with_a_reason_alone_is_accepted():
    rereview = _validate(
        _resolved_both(
            recommendation="escalate",
            escalation_reason="the pull request was rewritten under me",
        )
    )

    assert rereview.escalation_reason.startswith("the pull request")


def test_a_re_review_reporting_more_fresh_findings_than_the_limit_is_refused():
    text = rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=tuple(fresh_block(f"R2.F{n}") for n in range(1, 52)),
    )
    with pytest.raises(ReReviewValidationError, match="above the"):
        _validate(text)


def test_a_re_review_bound_to_a_commit_that_is_not_the_target_is_refused():
    with pytest.raises(ReReviewShaBindingError):
        _validate(_resolved_both(), head_sha=OTHER_SHA)
