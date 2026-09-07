"""The classification itself: facts in, one next action out, nothing else.

These tests build the models directly rather than going through the three
documents, because what is under test is a pure function and the point of
having one is that it can be read and exercised on its own. The end-to-end
path that produces the same facts from real documents is covered in
``test_decision_runner.py`` and ``test_decision_cli.py``.
"""

from __future__ import annotations

import pytest

from review_loop.decision import NextAction, classify, gather_facts
from review_loop.rereview import FindingResolution, ReReview, Resolution
from review_loop.verdict import Finding, Recommendation, Severity

HEAD = "5a0d6cbb0f0f4b0e0d0b9a1c2d3e4f5061728394"


def original(finding_id: str = "F1", severity: Severity = Severity.MAJOR) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=severity,
        location="services/orchestrator/src/orchestrator/http_server.py:42",
        problem="The dispatch handler swallows the agent error.",
        evidence="test_dispatch_error asserts only the status code.",
        required_outcome="The error is surfaced and a test proves it.",
    )


def resolution(
    finding_id: str = "F1", value: Resolution = Resolution.RESOLVED
) -> FindingResolution:
    return FindingResolution(
        finding_id=finding_id,
        resolution=value,
        evidence="http_server.py now raises and the test asserts it.",
        reason=None if value is Resolution.RESOLVED else "The handler still swallows it.",
    )


def fresh(finding_id: str = "R2.F1", severity: Severity = Severity.MAJOR) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=severity,
        location="services/orchestrator/src/orchestrator/http_server.py:51",
        problem="The new error path leaks the upstream request id.",
        evidence="The raised message interpolates the x-request-id header.",
        required_outcome="The id is not included in the surfaced error.",
    )


def rereview(
    *,
    resolutions=(),
    fresh_findings=(),
    recommendation: Recommendation = Recommendation.APPROVED,
    escalation_reason: str | None = None,
) -> ReReview:
    return ReReview(
        round=2,
        reviewed_head_sha=HEAD,
        recommendation=recommendation,
        resolutions=tuple(resolutions),
        fresh_findings=tuple(fresh_findings),
        escalation_reason=escalation_reason,
    )


def action(originals, review, *, not_current=()) -> NextAction:
    facts = gather_facts(
        tuple(originals), review, evidence_not_current_reasons=tuple(not_current)
    )
    return classify(facts).next_action


# -- Case 1: clean current evidence ------------------------------------------


def test_all_resolved_with_no_fresh_findings_is_ready_for_a_human_decision():
    assert (
        action(
            (original("F1"), original("F2", Severity.MINOR)),
            rereview(
                resolutions=(resolution("F1"), resolution("F2")),
                recommendation=Recommendation.APPROVED,
            ),
        )
        is NextAction.READY_FOR_HUMAN_MERGE_DECISION
    )


def test_ready_names_the_three_facts_that_produced_it():
    facts = gather_facts(
        (original("F1"),),
        rereview(resolutions=(resolution("F1"),)),
    )
    reasons = " ".join(classify(facts).reasons)
    assert "every original finding is RESOLVED" in reasons
    assert "no fresh Blocking or Major finding" in reasons
    assert "current merge context" in reasons


# -- Case 2: fresh Minor only ------------------------------------------------


def test_a_fresh_minor_finding_does_not_withhold_the_human_decision():
    """A Minor finding is surfaced for the human, not used to withhold them.

    Note what the recommendation is here. A re-review that resolved
    everything and raised one Minor finding *must* recommend
    ``changes_requested`` -- the re-review contract refuses ``approved``
    alongside any fresh finding -- so a classification that copied the
    recommendation would make a Minor finding indistinguishable from a Major
    one.
    """
    result = classify(
        gather_facts(
            (original("F1"),),
            rereview(
                resolutions=(resolution("F1"),),
                fresh_findings=(fresh("R2.F1", Severity.MINOR),),
                recommendation=Recommendation.CHANGES_REQUESTED,
            ),
        )
    )
    assert result.next_action is NextAction.READY_FOR_HUMAN_MERGE_DECISION
    # ...and it is surfaced rather than silently dropped, because accepting or
    # deferring it is part of what the human is deciding.
    assert "R2.F1" in " ".join(result.reasons)


# -- Case 3: an unresolved original finding ----------------------------------


def test_an_unresolved_original_finding_requires_another_fix():
    assert (
        action(
            (original("F1"), original("F2", Severity.MINOR)),
            rereview(
                resolutions=(
                    resolution("F1", Resolution.UNRESOLVED),
                    resolution("F2"),
                ),
                recommendation=Recommendation.CHANGES_REQUESTED,
            ),
        )
        is NextAction.FIX_REQUIRED
    )


def test_fix_required_says_that_it_starts_nothing():
    result = classify(
        gather_facts(
            (original("F1"),),
            rereview(
                resolutions=(resolution("F1", Resolution.UNRESOLVED),),
                recommendation=Recommendation.CHANGES_REQUESTED,
            ),
        )
    )
    assert result.next_action is NextAction.FIX_REQUIRED
    assert "no fix round is started by this command" in " ".join(result.reasons)


def test_an_unresolved_minor_original_finding_still_requires_a_fix():
    assert (
        action(
            (original("F1", Severity.MINOR),),
            rereview(
                resolutions=(resolution("F1", Resolution.UNRESOLVED),),
                recommendation=Recommendation.CHANGES_REQUESTED,
            ),
        )
        is NextAction.FIX_REQUIRED
    )


# -- Case 4: a fresh Major finding -------------------------------------------


def test_a_fresh_major_finding_requires_another_fix():
    assert (
        action(
            (original("F1"),),
            rereview(
                resolutions=(resolution("F1"),),
                fresh_findings=(fresh("R2.F1", Severity.MAJOR),),
                recommendation=Recommendation.CHANGES_REQUESTED,
            ),
        )
        is NextAction.FIX_REQUIRED
    )


def test_a_resolved_original_beside_a_fresh_major_stays_two_facts():
    """The regression this whole stage is most likely to lose.

    ``F1 RESOLVED`` and ``R2.F1 Major`` are both true, and flattening them
    into "a Major finding exists" would erase the first -- reporting that the
    fix did not work when it did.
    """
    facts = gather_facts(
        (original("F1"),),
        rereview(
            resolutions=(resolution("F1"),),
            fresh_findings=(fresh("R2.F1", Severity.MAJOR),),
            recommendation=Recommendation.CHANGES_REQUESTED,
        ),
    )
    assert facts.resolved_original_finding_ids == ("F1",)
    assert facts.unresolved_original_finding_ids == ()
    assert facts.fresh_major_finding_ids == ("R2.F1",)
    assert classify(facts).next_action is NextAction.FIX_REQUIRED


# -- Case 5: a fresh Blocking finding ----------------------------------------


def test_a_fresh_blocking_finding_escalates_rather_than_requesting_a_fix():
    assert (
        action(
            (original("F1"),),
            rereview(
                resolutions=(resolution("F1"),),
                fresh_findings=(fresh("R2.F1", Severity.BLOCKING),),
                recommendation=Recommendation.ESCALATE,
            ),
        )
        is NextAction.HUMAN_ESCALATION
    )


def test_blocking_outranks_an_unresolved_original_finding():
    """Escalation is never downgraded by the presence of a fixable problem."""
    assert (
        action(
            (original("F1"), original("F2", Severity.MINOR)),
            rereview(
                resolutions=(
                    resolution("F1", Resolution.UNRESOLVED),
                    resolution("F2"),
                ),
                fresh_findings=(fresh("R2.F1", Severity.BLOCKING),),
                recommendation=Recommendation.ESCALATE,
            ),
        )
        is NextAction.HUMAN_ESCALATION
    )


def test_an_outstanding_original_blocking_finding_escalates():
    """One rule over both collections: any Blocking finding escalates.

    Unreachable through the real chain -- a round-1 Blocking finding escalates
    instead of requesting changes, and the re-review input admits only a
    ``changes_requested`` review -- so this asserts the rule holds over the
    collection it would otherwise only be enforced in.
    """
    assert (
        action(
            (original("F1", Severity.BLOCKING),),
            rereview(
                resolutions=(resolution("F1", Resolution.UNRESOLVED),),
                recommendation=Recommendation.CHANGES_REQUESTED,
            ),
        )
        is NextAction.HUMAN_ESCALATION
    )


# -- Case 6: an escalated original finding -----------------------------------


def test_an_escalated_original_finding_escalates():
    assert (
        action(
            (original("F1"), original("F2", Severity.MINOR)),
            rereview(
                resolutions=(
                    resolution("F1", Resolution.ESCALATE),
                    resolution("F2"),
                ),
                recommendation=Recommendation.ESCALATE,
            ),
        )
        is NextAction.HUMAN_ESCALATION
    )


def test_an_escalating_recommendation_escalates_even_with_nothing_outstanding():
    """The one thing only the recommendation can say.

    Every original finding is RESOLVED and no fresh finding was raised, so the
    collections alone would read as ready. The reviewer is nonetheless asking
    a human something, and that is not this runner's to overrule.
    """
    result = classify(
        gather_facts(
            (original("F1"),),
            rereview(
                resolutions=(resolution("F1"),),
                recommendation=Recommendation.ESCALATE,
                escalation_reason="The fix changes a public contract.",
            ),
        )
    )
    assert result.next_action is NextAction.HUMAN_ESCALATION
    assert "The fix changes a public contract." in " ".join(result.reasons)


# -- currency outranks everything --------------------------------------------


@pytest.mark.parametrize(
    "review",
    [
        rereview(resolutions=(resolution("F1"),)),
        rereview(
            resolutions=(resolution("F1", Resolution.UNRESOLVED),),
            recommendation=Recommendation.CHANGES_REQUESTED,
        ),
        rereview(
            resolutions=(resolution("F1"),),
            fresh_findings=(fresh("R2.F1", Severity.BLOCKING),),
            recommendation=Recommendation.ESCALATE,
        ),
    ],
)
def test_stale_evidence_is_never_classified_as_a_state_of_the_pull_request(review):
    """"Cannot decide" is not one of the decisions.

    Whatever the findings say, a re-review of a commit the pull request has
    moved off describes something nobody is looking at -- so the answer is
    that there is no current answer, not a current answer of any kind.
    """
    assert (
        action((original("F1"),), review, not_current=("the head moved",))
        is NextAction.EVIDENCE_NOT_CURRENT
    )


def test_stale_evidence_reports_why_rather_than_a_finding_summary():
    facts = gather_facts(
        (original("F1"),),
        rereview(resolutions=(resolution("F1"),)),
        evidence_not_current_reasons=("the head moved from H2 to H3",),
    )
    result = classify(facts)
    assert result.next_action is NextAction.EVIDENCE_NOT_CURRENT
    assert result.reasons == ("the head moved from H2 to H3",)
    assert facts.evidence_current is False
    # The findings are still gathered, so the diagnostic can say what the
    # re-review was about -- it just cannot be classified as current.
    assert facts.resolved_original_finding_ids == ("F1",)


# -- the facts themselves ----------------------------------------------------


def test_every_resolution_lands_in_exactly_one_list():
    facts = gather_facts(
        (original("F1"), original("F2", Severity.MINOR), original("F3")),
        rereview(
            resolutions=(
                resolution("F1"),
                resolution("F2", Resolution.UNRESOLVED),
                resolution("F3", Resolution.ESCALATE),
            ),
            recommendation=Recommendation.ESCALATE,
        ),
    )
    assert facts.resolved_original_finding_ids == ("F1",)
    assert facts.unresolved_original_finding_ids == ("F2",)
    assert facts.escalated_original_finding_ids == ("F3",)


def test_fresh_findings_are_split_by_severity_and_never_mixed_with_originals():
    facts = gather_facts(
        (original("F1"),),
        rereview(
            resolutions=(resolution("F1", Resolution.UNRESOLVED),),
            fresh_findings=(
                fresh("R2.F1", Severity.MAJOR),
                fresh("R2.F2", Severity.MINOR),
                fresh("R2.F3", Severity.MINOR),
            ),
            recommendation=Recommendation.CHANGES_REQUESTED,
        ),
    )
    assert facts.fresh_blocking_finding_ids == ()
    assert facts.fresh_major_finding_ids == ("R2.F1",)
    assert facts.fresh_minor_finding_ids == ("R2.F2", "R2.F3")
    assert "F1" not in facts.fresh_major_finding_ids
    assert facts.unresolved_original_finding_ids == ("F1",)
