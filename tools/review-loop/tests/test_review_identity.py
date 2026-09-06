"""The canonical identity of one validated review.

The digest's whole job is to differ when the review differs, so these tests
are mostly "change one field, expect a different digest" -- and one that
matters more than the rest: the digest must survive a round trip through the
handoff, because the two computations that are compared happen in different
processes, on either side of a JSON document.
"""

import json

import pytest

from review_loop.review_identity import (
    CANONICAL_VERSION,
    canonical_document,
    review_sha256,
)
from review_loop.review_target import ReviewTarget
from review_loop.routing import load_handoff
from review_loop.verdict import Finding, Recommendation, ReviewVerdict, Severity

from fakes import ADVANCED_BASE_TIP, BASE_TIP, FULL_SHA, OTHER_SHA, REPO
from rereview_fakes import review_document

TARGET = ReviewTarget(
    repo=REPO,
    number=27,
    head_sha=FULL_SHA,
    base_ref="master",
    ci_merge_base_sha=BASE_TIP,
)


def _finding(**overrides) -> Finding:
    fields = {
        "finding_id": "F1",
        "severity": Severity.MAJOR,
        "location": "worker.py:42",
        "problem": "the claim is not atomic",
        "evidence": "two processes can both win",
        "required_outcome": "make the claim atomic",
        "scope_boundary": None,
    }
    fields.update(overrides)
    return Finding(**fields)


def _verdict(**overrides) -> ReviewVerdict:
    fields = {
        "round": 1,
        "reviewed_head_sha": FULL_SHA,
        "recommendation": Recommendation.CHANGES_REQUESTED,
        "open_findings": (_finding(),),
        "escalation_reason": None,
    }
    fields.update(overrides)
    return ReviewVerdict(**fields)


def _digest(target=TARGET, verdict=None) -> str:
    return review_sha256(target, verdict if verdict is not None else _verdict())


# --- stability --------------------------------------------------------------


def test_the_same_review_always_has_the_same_identity():
    assert _digest() == _digest()


def test_the_identity_is_a_sha256_hex_digest():
    digest = _digest()

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_the_canonical_document_is_parseable_and_names_its_version():
    payload = json.loads(canonical_document(TARGET, _verdict()))

    assert payload["version"] == CANONICAL_VERSION
    assert payload["head_sha"] == FULL_SHA
    assert payload["ci_merge_base_sha"] == BASE_TIP
    assert payload["findings"][0]["finding_id"] == "F1"


def test_the_canonical_document_has_no_incidental_whitespace():
    document = canonical_document(TARGET, _verdict())

    assert ", " not in document and ": " not in document


# --- what changes the identity ---------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo", "someone/else"),
        ("number", 99),
        ("head_sha", OTHER_SHA),
        ("base_ref", "release"),
        ("ci_merge_base_sha", ADVANCED_BASE_TIP),
    ],
)
def test_a_different_review_target_is_a_different_identity(field, value):
    from dataclasses import replace

    assert _digest(target=replace(TARGET, **{field: value})) != _digest()


def test_the_ci_merge_base_alone_changes_the_identity():
    """The merge-context half, stated on its own because it is the subtle one.

    The same head reviewed against a different base is a different verified
    integration state, and this package's record identity already says so.
    """
    from dataclasses import replace

    other_context = replace(TARGET, ci_merge_base_sha=ADVANCED_BASE_TIP)

    assert _digest(target=other_context) != _digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("finding_id", "F9"),
        ("severity", Severity.MINOR),
        ("location", "process.py:7"),
        ("problem", "something else entirely"),
        ("evidence", "a different observation"),
        ("required_outcome", "a different fix"),
        ("scope_boundary", "do not touch the parser"),
    ],
)
def test_any_change_to_a_finding_changes_the_identity(field, value):
    assert _digest(verdict=_verdict(open_findings=(_finding(**{field: value}),))) != _digest()


def test_the_same_finding_id_over_a_different_finding_is_a_different_identity():
    """The gap finding-id equality cannot close, at its source."""
    other = _finding(
        location="process.py",
        problem="inherited credentials reach the reviewer",
        required_outcome="remove them",
    )

    assert other.finding_id == _finding().finding_id
    assert _digest(verdict=_verdict(open_findings=(other,))) != _digest()


def test_finding_order_is_part_of_the_identity():
    a, b = _finding(finding_id="F1"), _finding(finding_id="F2")

    assert _digest(verdict=_verdict(open_findings=(a, b))) != _digest(
        verdict=_verdict(open_findings=(b, a))
    )


def test_the_round_the_recommendation_and_the_escalation_reason_all_count():
    base = _digest()

    assert _digest(verdict=_verdict(round=2)) != base
    assert (
        _digest(
            verdict=_verdict(
                recommendation=Recommendation.ESCALATE,
                escalation_reason="a human should look",
            )
        )
        != base
    )
    assert _digest(verdict=_verdict(escalation_reason="a human should look")) != base


def test_an_extra_finding_changes_the_identity():
    two = _verdict(open_findings=(_finding(), _finding(finding_id="F2")))

    assert _digest(verdict=two) != _digest()


# --- the round trip that actually has to hold ------------------------------


def test_the_identity_survives_the_handoff_the_chain_carries_it_through():
    """Computed at fix time, recomputed at re-review time, from one document.

    Those two computations happen in different processes on either side of a
    JSON file. If serialisation dropped or reshaped anything the digest
    covers, the chain would reject every legitimate pair -- so this is the
    property the whole mechanism rests on.
    """
    document = review_document()
    first = load_handoff(document)
    second = load_handoff(document)

    assert review_sha256(first.target, first.verdict) == review_sha256(
        second.target, second.verdict
    )


def test_two_review_documents_differing_only_in_finding_text_differ():
    def body(problem):
        return review_document(
            findings=(
                {
                    "finding_id": "F1",
                    "severity": "Major",
                    "location": "worker.py",
                    "problem": problem,
                    "evidence": "the code says so",
                    "required_outcome": "fix it",
                    "scope_boundary": None,
                },
            )
        )

    a, b = load_handoff(body("one thing")), load_handoff(body("another thing"))

    assert review_sha256(a.target, a.verdict) != review_sha256(b.target, b.verdict)


def test_ci_evidence_is_outside_the_identity():
    """It has to be: the handoff does not carry it.

    `routing.load_handoff` rebuilds the target without `ci_evidence`, so a
    digest that covered it could be computed before the handoff and never
    after. It is CI observation rather than review content anyway.
    """
    from dataclasses import replace

    with_evidence = replace(
        TARGET, ci_evidence=((".github/workflows/pytest.yml", 42, "success"),)
    )

    assert _digest(target=with_evidence) == _digest()
