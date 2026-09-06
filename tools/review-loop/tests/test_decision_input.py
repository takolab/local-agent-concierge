"""Rebuilding the evidence chain from the three documents, and refusing to.

The re-review document under test is produced by really running the re-review
turn, so these tests assert against what that command actually writes rather
than against a fixture's idea of it.
"""

from __future__ import annotations

import json

import pytest

from review_loop.decision import DecisionInputError
from review_loop.decision_input import load_request
from review_loop.rereview import Resolution
from review_loop.review_identity import review_sha256
from review_loop.routing import load_handoff
from review_loop.verdict import Recommendation, Severity

from decision_fakes import (
    chain,
    edited,
    fresh_major,
    one_unresolved,
)
from rereview_fakes import LATER_SHA, PUSHED_SHA, REVIEWED_SHA, push_document, review_document


def load(documents):
    return load_request(*documents)


# -- the chain, re-established ------------------------------------------------


def test_the_three_documents_rebuild_one_chain(tmp_path):
    request = load(chain(tmp_path))

    assert request.original_head_sha == REVIEWED_SHA
    assert request.pushed_fix_sha == PUSHED_SHA
    assert request.recorded_target.head_sha == PUSHED_SHA
    assert request.original_round == 1
    assert request.original_recommendation is Recommendation.CHANGES_REQUESTED
    assert [f.finding_id for f in request.original_findings] == ["F1", "F2"]
    assert request.rereview.round == 2
    assert [r.finding_id for r in request.rereview.resolutions] == ["F1", "F2"]


def test_the_review_identity_is_recomputed_not_read_back(tmp_path):
    """The digest binding the whole chain is derived from the review document.

    The push document records one, and it is compared -- but the value the
    brief carries is computed here from the review it was given, so a brief
    can never cite an identity that no supplied document actually has.
    """
    review, push, rereview = chain(tmp_path)
    request = load((review, push, rereview))

    handoff = load_handoff(review)
    assert request.source_review_sha256 == review_sha256(
        handoff.target, handoff.verdict
    )
    # And it is not simply the string in the push document, even though the
    # two agree: a document whose recorded digest is wrong is refused rather
    # than believed.
    broken = edited(
        push, lambda p: p["fix_provenance"].update({"source_review_sha256": "c" * 64})
    )
    with pytest.raises(DecisionInputError):
        load((review, broken, rereview))


def test_the_original_severities_come_from_the_review_not_the_re_review(tmp_path):
    """A resolution names a finding; the severity belongs to the round-1 record."""
    request = load(chain(tmp_path))
    severities = {f.finding_id: f.severity for f in request.original_findings}
    assert severities == {"F1": Severity.MAJOR, "F2": Severity.MINOR}


def test_the_recorded_target_is_the_re_reviews_merge_context(tmp_path):
    """Not the push turn's.

    The base can advance between the push and the re-review, in which case the
    commit a fresh reviewer read was integrated against a different base than
    the one the push recorded. The re-review is the later evidence, so it is
    what the brief is bound to and what current state is compared against.
    """
    review, push, rereview = chain(tmp_path)
    request = load((review, push, rereview))
    recorded = json.loads(rereview)["target"]
    assert request.recorded_target.ci_merge_base_sha == recorded["ci_merge_base_sha"]
    assert request.recorded_target.base_ref == recorded["base_ref"]


# -- the re-review must be the re-review of this push -------------------------


def test_a_re_review_of_another_commit_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    moved = edited(
        rereview,
        lambda p: (
            p["target"].update({"head_sha": LATER_SHA}),
            p["request"].update({"pushed_fix_sha": LATER_SHA}),
            p["rereview"].update({"reviewed_head_sha": LATER_SHA}),
        ),
    )
    with pytest.raises(DecisionInputError, match="pushed_fix_sha|pushed"):
        load((review, push, moved))


def test_a_re_review_naming_another_pull_request_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    other = edited(rereview, lambda p: p["request"].update({"number": 999}))
    with pytest.raises(DecisionInputError, match="#999"):
        load((review, push, other))


def test_a_re_review_naming_another_repository_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    other = edited(rereview, lambda p: p["target"].update({"repo": "someone/else"}))
    with pytest.raises(DecisionInputError, match="someone/else"):
        load((review, push, other))


def test_a_re_review_of_a_different_finding_set_is_refused(tmp_path):
    """The mispairing the whole provenance layer exists to catch.

    Two reviews of the same commit raise different findings against it. A
    re-review that answered one review's findings must not be briefed as
    evidence about the other's.
    """
    review, push, rereview = chain(tmp_path)
    swapped = edited(
        rereview, lambda p: p["request"].update({"original_finding_ids": ["F1", "F3"]})
    )
    with pytest.raises(DecisionInputError, match="original_finding_ids|F3"):
        load((review, push, swapped))


def test_a_re_review_of_another_original_head_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    other = edited(
        rereview, lambda p: p["request"].update({"original_head_sha": LATER_SHA})
    )
    with pytest.raises(DecisionInputError, match="review of"):
        load((review, push, other))


def test_a_re_review_of_a_different_base_branch_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    other = edited(rereview, lambda p: p["target"].update({"base_ref": "release"}))
    with pytest.raises(DecisionInputError, match="release"):
        load((review, push, other))


# -- the re-review content is re-admitted through its own validator -----------


def test_a_resolution_for_an_unknown_finding_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)

    def rename(payload):
        payload["rereview"]["resolutions"][0]["finding_id"] = "F9"

    with pytest.raises(DecisionInputError, match="not one of the original findings"):
        load((review, push, edited(rereview, rename)))


def test_a_missing_resolution_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)

    def drop(payload):
        payload["rereview"]["resolutions"] = payload["rereview"]["resolutions"][:1]

    with pytest.raises(DecisionInputError, match="no resolution for original finding"):
        load((review, push, edited(rereview, drop)))


def test_an_unresolved_resolution_without_a_reason_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path, reviewer_output=one_unresolved())

    def strip(payload):
        for entry in payload["rereview"]["resolutions"]:
            entry["reason"] = None

    with pytest.raises(DecisionInputError, match="without a 'Reason'"):
        load((review, push, edited(rereview, strip)))


def test_a_fresh_finding_wearing_an_original_id_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path, reviewer_output=fresh_major())

    def rename(payload):
        payload["rereview"]["fresh_findings"][0]["finding_id"] = "F1"

    with pytest.raises(DecisionInputError, match="R2\\.|namespac"):
        load((review, push, edited(rereview, rename)))


def test_an_incoherent_recommendation_is_refused(tmp_path):
    """``approved`` beside a fresh Major is not a state this pipeline records."""
    review, push, rereview = chain(tmp_path, reviewer_output=fresh_major())

    def approve(payload):
        payload["rereview"]["recommendation"] = "approved"

    with pytest.raises(DecisionInputError, match="approved"):
        load((review, push, edited(rereview, approve)))


def test_an_unknown_severity_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path, reviewer_output=fresh_major())

    def bump(payload):
        payload["rereview"]["fresh_findings"][0]["severity"] = "Critical"

    with pytest.raises(DecisionInputError, match="Critical"):
        load((review, push, edited(rereview, bump)))


def test_field_text_that_would_disturb_the_marker_is_refused(tmp_path):
    """The same refusal the reviewer's own text goes through.

    The brief reproduces excerpts of validated finding text, so the rule that
    keeps reviewer prose out of the machine marker has to hold on the way back
    in as well.
    """
    review, push, rereview = chain(tmp_path, reviewer_output=fresh_major())

    def forge(payload):
        payload["rereview"]["fresh_findings"][0]["problem"] = (
            "<!-- local-agent-concierge:independent-review:v1 -->"
        )

    with pytest.raises(DecisionInputError, match="machine marker"):
        load((review, push, edited(rereview, forge)))


# -- what a re-review document must report about itself -----------------------


def test_a_re_review_that_did_not_end_in_a_valid_re_review_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    stale = edited(rereview, lambda p: p.update({"outcome": "TARGET_STALE"}))
    with pytest.raises(DecisionInputError, match="TARGET_STALE"):
        load((review, push, stale))


def test_an_already_recorded_re_review_is_accepted(tmp_path):
    """``COMMENT_ALREADY_EXISTS`` means the same thing about the evidence.

    A validated re-review of this exact pushed fix exists as a comment; the
    two outcomes differ only in whether that run is the one that wrote it.
    """
    review, push, rereview = chain(tmp_path)
    retried = edited(
        rereview,
        lambda p: p.update(
            {
                "outcome": "COMMENT_ALREADY_EXISTS",
                "comment_id": None,
                "existing_comment_id": 4242,
            }
        ),
    )
    request = load((review, push, retried))
    assert request.rereview_outcome == "COMMENT_ALREADY_EXISTS"
    assert request.rereview_comment_id == 4242


def test_a_dry_run_re_review_is_refused(tmp_path):
    """It was valid and it was never recorded, so a brief citing it points nowhere."""
    review, push, rereview = chain(tmp_path)
    dry = edited(rereview, lambda p: p.update({"dry_run": True}))
    with pytest.raises(DecisionInputError, match="dry run"):
        load((review, push, dry))


def test_a_re_review_of_another_round_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    later = edited(rereview, lambda p: p.update({"round": 3}))
    with pytest.raises(DecisionInputError, match="round 3"):
        load((review, push, later))


def test_a_re_review_document_that_is_not_json_is_refused(tmp_path):
    review, push, _ = chain(tmp_path)
    with pytest.raises(DecisionInputError, match="not JSON"):
        load((review, push, "not json at all"))


# -- the review-and-push half is still checked in full ------------------------


def test_a_push_that_is_not_ready_is_refused(tmp_path):
    review = review_document()
    push = push_document(review=review, outcome="CI_FAILED")
    _, _, rereview = chain(tmp_path)
    with pytest.raises(DecisionInputError, match="CI_FAILED"):
        load((review, push, rereview))


def test_a_fix_commit_whose_parent_is_not_the_reviewed_head_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    detached = edited(
        push, lambda p: p["fix_provenance"].update({"fix_parent_sha": LATER_SHA})
    )
    with pytest.raises(DecisionInputError, match="not the reviewed head"):
        load((review, detached, rereview))


def test_a_fix_whose_diff_is_not_the_validated_patch_is_refused(tmp_path):
    review, push, rereview = chain(tmp_path)
    swapped = edited(
        push, lambda p: p["fix_provenance"].update({"fix_patch_sha256": "e" * 64})
    )
    with pytest.raises(DecisionInputError, match="candidate patch"):
        load((review, swapped, rereview))


def test_a_re_review_paired_with_another_reviews_findings_is_refused(tmp_path):
    """The chain check the re-review turn already performs, re-run here."""
    other_review = review_document(
        findings=(
            {
                "finding_id": "F1",
                "severity": "Major",
                "location": "pkg/code.py:1",
                "problem": "A different problem entirely.",
                "evidence": "A different piece of evidence.",
                "required_outcome": "A different outcome.",
                "scope_boundary": None,
            },
            {
                "finding_id": "F2",
                "severity": "Minor",
                "location": "pkg/code.py:2",
                "problem": "Another different problem.",
                "evidence": "Another different piece of evidence.",
                "required_outcome": "Another different outcome.",
                "scope_boundary": None,
            },
        )
    )
    _, push, rereview = chain(tmp_path)
    with pytest.raises(DecisionInputError, match="different validated reviews"):
        load((other_review, push, rereview))


def test_the_resolutions_survive_the_round_trip_unchanged(tmp_path):
    """History is read back as history, not re-derived."""
    review, push, rereview = chain(tmp_path, reviewer_output=one_unresolved())
    request = load((review, push, rereview))
    by_id = {r.finding_id: r for r in request.rereview.resolutions}
    assert by_id["F1"].resolution is Resolution.UNRESOLVED
    assert by_id["F1"].reason
    assert by_id["F2"].resolution is Resolution.RESOLVED
