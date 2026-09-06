"""Binding a re-review to a validated review and the PUSH_READY push of its fix.

Every test here is about refusing a pair of documents that is not exactly
that. Nothing in this module reaches the network or git.
"""

import json

import pytest

from review_loop.rereview import RE_REVIEW_ROUND
from review_loop.rereview_input import ReReviewInputError, load_request

from fakes import ADVANCED_BASE_TIP, BASE_TIP, OTHER_SHA, REPO
from rereview_fakes import (
    BASE_REF,
    LATER_SHA,
    PR,
    PUSHED_SHA,
    REVIEWED_SHA,
    push_document,
    review_document,
)


def _load(review=None, push=None, **kwargs):
    return load_request(
        review if review is not None else review_document(),
        push if push is not None else push_document(),
        **kwargs,
    )


# --- the accepted pair ------------------------------------------------------


def test_a_validated_review_and_the_push_of_its_fix_bind_to_the_pushed_commit():
    request = _load()

    assert request.pushed_fix_sha == PUSHED_SHA
    assert request.target.repo == REPO
    assert request.target.number == PR
    assert request.target.base_ref == BASE_REF
    assert request.target.ci_merge_base_sha == BASE_TIP
    assert request.round == RE_REVIEW_ROUND


def test_the_original_findings_are_carried_forward_with_their_identities():
    request = _load()

    assert request.original_finding_ids == ("F1", "F2")
    assert request.original_head_sha == REVIEWED_SHA
    assert request.original_round == 1
    assert request.original_findings[0].severity.value == "Major"
    assert request.original_findings[1].required_outcome.startswith("The claim")


def test_a_push_that_found_the_fix_already_on_the_branch_is_accepted():
    # PUSH_READY with no commit of its own: a previous run pushed it. The
    # parent check is unavailable, and everything else still holds.
    request = _load(push=push_document(include_commit=False))

    assert request.pushed_fix_sha == PUSHED_SHA


def test_an_explicit_repo_that_matches_both_documents_is_accepted():
    assert _load(expected_repo=REPO).target.repo == REPO


# --- the review half --------------------------------------------------------


def test_a_review_that_was_not_recorded_is_refused():
    with pytest.raises(ReReviewInputError, match="review input is not usable"):
        _load(review=review_document(outcome="REVIEW_MALFORMED"))


def test_a_review_with_no_open_finding_is_refused():
    document = review_document(recommendation="approved", findings=())
    with pytest.raises(ReReviewInputError, match="no open finding"):
        _load(review=document)


def test_an_escalating_review_is_refused():
    document = json.loads(review_document())
    document["verdict"]["recommendation"] = "escalate"
    with pytest.raises(ReReviewInputError, match="recommends 'escalate'"):
        _load(review=json.dumps(document))


def test_a_review_of_a_later_round_is_refused():
    with pytest.raises(ReReviewInputError):
        _load(review=review_document(round_number=2))


# --- the push half ----------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    ["CI_PENDING", "CI_FAILED", "CI_STALE_TARGET", "PUSH_NOT_VERIFIED", "PUSH_FAILED"],
)
def test_only_push_ready_starts_a_re_review(outcome):
    with pytest.raises(ReReviewInputError, match="push outcome"):
        _load(push=push_document(outcome=outcome))


def test_a_dry_run_push_is_refused():
    with pytest.raises(ReReviewInputError, match="dry_run"):
        _load(push=push_document(dry_run=True))


def test_a_push_that_did_not_mutate_the_repository_is_refused():
    with pytest.raises(ReReviewInputError, match="repository_mutated"):
        _load(push=push_document(repository_mutated=False))


@pytest.mark.parametrize("boundary", ["exceeded", "unknown"])
def test_a_push_whose_write_boundary_is_not_clean_is_refused(boundary):
    with pytest.raises(ReReviewInputError, match="boundary_status"):
        _load(push=push_document(boundary_status=boundary))


def test_a_push_without_a_pushed_sha_is_refused():
    document = json.loads(push_document())
    document["pushed_sha"] = None
    with pytest.raises(ReReviewInputError, match="pushed_sha"):
        _load(push=json.dumps(document))


def test_an_abbreviated_pushed_sha_is_refused():
    with pytest.raises(ReReviewInputError, match="40-character"):
        _load(push=push_document(pushed_sha=PUSHED_SHA[:12]))


def test_a_push_of_the_reviewed_head_itself_is_not_a_fix():
    with pytest.raises(ReReviewInputError, match="no fix commit to re-review"):
        _load(push=push_document(pushed_sha=REVIEWED_SHA))


# --- the pair -------------------------------------------------------------


def test_a_push_for_a_different_pull_request_is_refused():
    with pytest.raises(ReReviewInputError, match="but the review it is paired with"):
        _load(push=push_document(number=99))


def test_a_push_for_a_different_repository_is_refused():
    with pytest.raises(ReReviewInputError, match="but the review it is paired with"):
        _load(push=push_document(repo="someone/else"))


def test_a_push_that_fixes_a_different_commit_is_refused():
    with pytest.raises(ReReviewInputError, match="but the review it is paired with"):
        _load(push=push_document(reviewed_sha=OTHER_SHA))


def test_an_explicit_repo_that_matches_neither_document_is_refused():
    with pytest.raises(ReReviewInputError, match="--repo"):
        _load(expected_repo="someone/else")


def test_a_fix_commit_whose_parent_is_not_the_reviewed_head_is_refused():
    document = push_document(commit_parent=LATER_SHA)
    with pytest.raises(ReReviewInputError, match="not a fix for this review"):
        _load(push=document)


def test_a_commit_that_is_not_the_commit_reported_as_pushed_is_refused():
    document = json.loads(push_document())
    document["commit"]["sha"] = LATER_SHA
    with pytest.raises(ReReviewInputError, match="but reports pushing"):
        _load(push=json.dumps(document))


# --- the CI evidence the push recorded -------------------------------------


def test_a_push_whose_verified_target_is_another_commit_is_refused():
    with pytest.raises(ReReviewInputError, match="but verified"):
        _load(push=push_document(verified_head_sha=LATER_SHA))


def test_a_push_with_no_verified_target_is_refused():
    with pytest.raises(ReReviewInputError, match="verified_target"):
        _load(push=push_document(include_verified_target=False))


def test_a_push_whose_ci_was_not_ready_is_refused():
    with pytest.raises(ReReviewInputError, match="only READY"):
        _load(push=push_document(ci_verdict="PENDING"))


def test_a_push_whose_ci_is_not_bound_to_the_pushed_commit_is_refused():
    with pytest.raises(ReReviewInputError, match="not bound to the pushed commit"):
        _load(push=push_document(bound_to_pushed_commit=False))


def test_a_push_whose_ci_describes_another_head_is_refused():
    with pytest.raises(ReReviewInputError, match="CI describes head"):
        _load(push=push_document(ci_head_sha=LATER_SHA))


def test_a_push_whose_ci_base_had_already_advanced_is_refused():
    # The push document's own staleness rule, re-read: CI tested a merge onto
    # a commit that was not the base tip when it was verified.
    with pytest.raises(ReReviewInputError, match="already stale"):
        _load(push=push_document(base_tip=ADVANCED_BASE_TIP))


# --- shape ------------------------------------------------------------------


def test_a_push_document_that_is_not_json_is_refused():
    with pytest.raises(ReReviewInputError, match="not JSON"):
        _load(push="not json at all")


def test_a_push_document_that_is_not_an_object_is_refused():
    with pytest.raises(ReReviewInputError, match="not a JSON object"):
        _load(push="[1, 2, 3]")
