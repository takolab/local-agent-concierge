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
    review_sha256_of,
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
    document = push_document(fix_parent_sha=LATER_SHA, commit_parent=LATER_SHA)
    with pytest.raises(ReReviewInputError, match="not a fix for this review"):
        _load(push=document)


def test_a_commit_block_disagreeing_with_the_provenance_is_refused():
    document = json.loads(push_document())
    document["commit"]["sha"] = LATER_SHA
    with pytest.raises(ReReviewInputError, match="disagrees with its own provenance"):
        _load(push=json.dumps(document))


def test_a_commit_parent_disagreeing_with_the_provenance_is_refused():
    with pytest.raises(ReReviewInputError, match="disagrees with its own provenance"):
        _load(push=push_document(commit_parent=LATER_SHA))


# --- provenance: which review caused this fix -----------------------------


def test_the_pushed_fixs_provenance_is_required():
    # A push document from a build that does not report it cannot be paired:
    # nothing in it says which review caused the fix to exist.
    with pytest.raises(ReReviewInputError, match="fix_provenance"):
        _load(push=push_document(include_provenance=False))


def test_the_parent_check_survives_a_push_that_created_no_commit():
    # The regression this exists for: PUSH_READY with commit == null used to
    # skip the parent check entirely, even though that path establishes it.
    document = push_document(include_commit=False, fix_parent_sha=LATER_SHA)
    with pytest.raises(ReReviewInputError, match="not a fix for this review"):
        _load(push=document)


def test_an_already_pushed_fix_still_binds_to_this_reviews_findings():
    document = push_document(include_commit=False, source_finding_ids=("F9",))
    with pytest.raises(ReReviewInputError, match="different finding set"):
        _load(push=document)


def test_a_push_that_fixed_another_reviews_findings_is_refused():
    # Review A of H1 raises F1 and F2; review B of H1 raises F3. Pairing
    # review B with the push of A's fix lines up on repo, PR, reviewed head
    # and pushed head -- and is still the wrong pairing. The finding-set check
    # names the difference; the identity digest behind it would catch this
    # pair even if the ids had matched.
    review_b = review_document(
        findings=(
            {
                "finding_id": "F3",
                "severity": "Minor",
                "location": "README.md",
                "problem": "A different problem entirely.",
                "evidence": "The file says so.",
                "required_outcome": "It stops saying so.",
                "scope_boundary": None,
            },
        )
    )
    with pytest.raises(ReReviewInputError, match="not fixed: F3"):
        _load(review=review_b, push=push_document())


def test_two_reviews_of_one_commit_sharing_finding_ids_are_told_apart():
    """The identity gap finding-id equality cannot close.

    `F1`, `F2` is the convention the reviewer prompt itself suggests, so two
    independent reviews of the same commit carrying the same ids over
    completely different findings is the ordinary case, not an exotic one.
    Round, reviewed head, merge base and finding-id set are all identical
    here; only the findings differ.
    """

    def finding(fid, location, problem, outcome, severity="Major"):
        return {
            "finding_id": fid,
            "severity": severity,
            "location": location,
            "problem": problem,
            "evidence": "the code says so",
            "required_outcome": outcome,
            "scope_boundary": None,
        }

    review_a = review_document(
        findings=(
            finding("F1", "worker.py", "claim is not atomic", "make claim atomic"),
            finding("F2", "ci.py", "stale CI is accepted", "reject stale merge context"),
        )
    )
    review_b = review_document(
        findings=(
            finding("F1", "process.py", "credentials reach the reviewer", "remove them"),
            finding("F2", "README.md", "the guarantee is false", "correct the contract"),
        )
    )
    push_a = push_document(review=review_a)

    # The pair that really belongs together is accepted...
    assert _load(review=review_a, push=push_a).original_finding_ids == ("F1", "F2")

    # ...and the one that only looks like it does is not.
    with pytest.raises(ReReviewInputError, match="different validated reviews"):
        _load(review=review_b, push=push_a)


def test_two_reviews_of_one_commit_against_different_bases_are_told_apart():
    """Same head, same findings, different verified integration state.

    This package already treats a review of `H` onto `B1` as a different
    record from one of the same `H` onto `B2` -- that is why the merge base is
    in the record identity. The fix provenance has to agree, or the two
    disagree about what "the same review" means.
    """
    review_b2 = review_document(merge_base=ADVANCED_BASE_TIP)
    push_b1 = push_document()

    with pytest.raises(ReReviewInputError, match="different integration states"):
        _load(review=review_b2, push=push_b1)


def test_the_review_identity_is_recomputed_not_read_back():
    # A push document carrying a digest that is merely well-formed is not a
    # match: the value compared against it comes from canonicalising the
    # review document supplied here.
    with pytest.raises(ReReviewInputError, match="different validated reviews"):
        _load(push=push_document(source_review_sha256="d" * 64))


def test_a_malformed_review_identity_is_refused():
    with pytest.raises(ReReviewInputError, match="source_review_sha256"):
        _load(push=push_document(source_review_sha256="not-a-digest"))


def test_a_push_document_with_no_review_identity_is_refused():
    document = json.loads(push_document())
    del document["fix_provenance"]["source_review_sha256"]
    with pytest.raises(ReReviewInputError, match="source_review_sha256"):
        _load(push=json.dumps(document))


def test_a_provenance_naming_another_merge_base_is_refused():
    with pytest.raises(ReReviewInputError, match="different integration states"):
        _load(push=push_document(source_merge_base=ADVANCED_BASE_TIP))


def test_a_push_fixing_only_some_of_this_reviews_findings_is_refused():
    with pytest.raises(ReReviewInputError, match="not fixed: F2"):
        _load(push=push_document(source_finding_ids=("F1",)))


def test_a_push_fixing_a_finding_this_review_never_raised_is_refused():
    with pytest.raises(ReReviewInputError, match="fixed but not in this review: F9"):
        _load(push=push_document(source_finding_ids=("F1", "F2", "F9")))


def test_a_provenance_listing_no_finding_id_is_refused():
    with pytest.raises(ReReviewInputError, match="lists no finding id"):
        _load(push=push_document(source_finding_ids=()))


def test_a_provenance_repeating_a_finding_id_is_refused():
    with pytest.raises(ReReviewInputError, match="more than once"):
        _load(push=push_document(source_finding_ids=("F1", "F2", "F2")))


def test_a_provenance_for_another_round_is_refused():
    with pytest.raises(ReReviewInputError, match="round"):
        _load(push=push_document(source_round=2))


def test_a_provenance_naming_another_reviewed_head_is_refused():
    with pytest.raises(ReReviewInputError, match="fixes a review of"):
        _load(push=push_document(source_head_sha=LATER_SHA))


def test_a_provenance_describing_another_commit_is_refused():
    with pytest.raises(ReReviewInputError, match="not the pushed fix"):
        _load(push=push_document(fix_sha=LATER_SHA))


def test_a_pushed_commit_whose_diff_is_not_the_candidate_patch_is_refused():
    with pytest.raises(ReReviewInputError, match="not to the candidate patch"):
        _load(push=push_document(fix_patch_sha256="c" * 64))


def test_a_malformed_patch_digest_is_refused():
    with pytest.raises(ReReviewInputError, match="SHA-256 digest"):
        _load(push=push_document(source_patch_sha256="not-a-digest"))


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


# --- the real document, not a fixture's idea of one -----------------------


def test_a_real_push_document_pairs_with_its_own_review(tmp_path):
    """End to end against the document `review-loop push` actually emits.

    Every other test here builds the push JSON from a helper, which proves
    the pairing rules and nothing about whether the previous stage emits what
    they require. This runs the real push turn over real git and feeds its
    real `--json` output straight into the pairing -- so a field the push
    stage stops emitting, or renames, fails here rather than in production.
    """
    import io
    import json as _json

    from conftest import build_scenario
    from push_fakes import PushGitHubClient, Timeline, fix_json, git
    from review_loop.push_cli import push_main
    from review_loop.reviewer_workspace import PreparedWorkspace

    def edits(worktree):
        (worktree / "pkg" / "code.py").write_text("value = 2\n")

    live = build_scenario(tmp_path, edits)
    review = review_document(
        head_sha=live.head_sha,
        number=live.number,
        findings=(
            {
                "finding_id": "F1",
                "severity": "Major",
                "location": "pkg/code.py",
                "problem": "The value is wrong.",
                "evidence": "It is 1 and should be 2.",
                "required_outcome": "It is 2.",
                "scope_boundary": None,
            },
            {
                "finding_id": "F2",
                "severity": "Minor",
                "location": "pkg/code.py",
                "problem": "And it is undocumented.",
                "evidence": "No comment explains it.",
                "required_outcome": "It is explained.",
                "scope_boundary": None,
            },
        ),
    )
    fix_path = tmp_path / "fix.json"
    fix_path.write_text(
        fix_json(
            head_sha=live.head_sha,
            changed_paths=live.changed_paths,
            patch_sha256=live.patch_sha256,
            patch_bytes=live.patch_bytes,
            patch_path=live.patch_path,
            number=live.number,
            finding_ids=("F1", "F2"),
            # The identity of the review above, computed the way the real fix
            # turn computes it from the review it was routed from.
            source_review_sha256=review_sha256_of(review),
        )
    )

    timeline = Timeline()
    client = PushGitHubClient(
        number=live.number, head_sha=live.head_sha, branch=live.branch
    )

    def observe_ci():
        pushed = live.remote_tip()
        client.head_sha = pushed
        client.ci[pushed] = "success"

    timeline.steps[1] = observe_ci

    out = io.StringIO()
    code = push_main(
        [
            "--fix-json", str(fix_path),
            "--repo-root", str(live.clone),
            "--json",
        ],
        client=client,
        workspace=PreparedWorkspace(
            str(live.clone), live.number, remote="origin", role="fix commit"
        ),
        stream=out,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    push_json = out.getvalue()
    assert code == 0, push_json
    assert _json.loads(push_json)["outcome"] == "PUSH_READY"

    pushed = live.remote_tip()

    request = load_request(review, push_json)

    assert request.pushed_fix_sha == pushed
    assert request.original_head_sha == live.head_sha
    assert request.original_finding_ids == ("F1", "F2")
    assert git(live.clone, "rev-list", "--count", f"{live.head_sha}..{pushed}") == "1"
