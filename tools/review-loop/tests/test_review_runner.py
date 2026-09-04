"""One review turn end to end, against offline fakes.

No test here reaches the network, an agent API, or a credential.
"""

import pytest

from review_loop import comment_format
from review_loop.github_client import GitHubApiError
from review_loop.model import EXIT_CODES, Verdict
from review_loop.review_runner import run_review
from review_loop.reviewer_process import ReviewerRun
from review_loop.verdict import ReviewOutcome

from fakes import (
    ADVANCED_BASE_TIP,
    BASELINE_PATH,
    FILTERED_PATH,
    FULL_SHA,
    OTHER_SHA,
    FailingGitHubClient,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    FakeReviewer,
    pull_request_payload,
    run_payload,
    verdict_text,
)

REPO = "takolab/local-agent-concierge"
PR = 27

DIFF_MISSING_EVERY_FILTER = ("README.md",)


def _green_client(**kwargs):
    return FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
        **kwargs,
    )


def _run(
    client=None,
    reviewer=None,
    reader=None,
    writer=None,
    dry_run=False,
):
    client = client if client is not None else _green_client()
    reviewer = reviewer if reviewer is not None else FakeReviewer()
    reader = reader if reader is not None else FakeCommentReader()
    writer = writer if writer is not None else FakeCommentWriter()
    result = run_review(
        client=client,
        reader=reader,
        writer=None if dry_run else writer,
        reviewer=reviewer,
        repo=REPO,
        number=PR,
        dry_run=dry_run,
    )
    return result, reviewer, writer


# --- verification gating ---------------------------------------------------


def test_a_ready_target_reaches_the_reviewer_and_is_recorded():
    result, reviewer, writer = _run()

    assert result.outcome is ReviewOutcome.REVIEW_VALID
    assert reviewer.invoked
    assert len(writer.posted) == 1
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "runs, verdict",
    [
        ([run_payload(run_id=1, status="in_progress", conclusion=None)], Verdict.PENDING),
        ([run_payload(run_id=1, conclusion="failure")], Verdict.FAILED),
        ([], Verdict.AMBIGUOUS),
    ],
    ids=["pending", "failed", "ambiguous"],
)
def test_a_target_that_is_not_ready_never_starts_a_reviewer(runs, verdict):
    client = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=runs,
        changed_files=DIFF_MISSING_EVERY_FILTER,
    )

    result, reviewer, writer = _run(client=client)

    assert result.outcome is ReviewOutcome.TARGET_NOT_READY
    assert result.pre_evaluation.verdict is verdict
    assert not reviewer.invoked
    assert writer.posted == []
    assert result.exit_code == EXIT_CODES[verdict] != 0


def test_a_stale_target_never_starts_a_reviewer():
    client = FakeGitHubClient(
        pull_requests=[
            pull_request_payload(number=PR, head_sha=FULL_SHA),
            pull_request_payload(number=PR, head_sha=OTHER_SHA),
        ],
        runs=[run_payload(run_id=1, conclusion="success")],
        changed_files=DIFF_MISSING_EVERY_FILTER,
    )

    result, reviewer, writer = _run(client=client)

    assert result.outcome is ReviewOutcome.TARGET_NOT_READY
    assert result.pre_evaluation.verdict is Verdict.STALE_TARGET
    assert not reviewer.invoked
    assert writer.posted == []


def test_a_not_ready_target_reports_the_verification_exit_code():
    client = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=[run_payload(run_id=1, status="in_progress", conclusion=None)],
        changed_files=DIFF_MISSING_EVERY_FILTER,
    )

    result, _, _ = _run(client=client)

    assert result.exit_code == result.pre_evaluation.exit_code


def test_a_github_failure_before_the_review_starts_no_reviewer():
    result, reviewer, writer = _run(client=FailingGitHubClient())

    assert result.outcome is ReviewOutcome.TARGET_NOT_READY
    assert result.pre_evaluation.verdict is Verdict.API_ERROR
    assert not reviewer.invoked
    assert writer.posted == []


# --- the reviewer's own failures -------------------------------------------


def test_a_failed_reviewer_writes_nothing():
    reviewer = FakeReviewer(ReviewerRun(failure="the reviewer exited 3"))

    result, _, writer = _run(reviewer=reviewer)

    assert result.outcome is ReviewOutcome.REVIEWER_FAILED
    assert writer.posted == []


def test_malformed_output_writes_nothing():
    reviewer = FakeReviewer(ReviewerRun(stdout="Looks fine to me!"))

    result, _, writer = _run(reviewer=reviewer)

    assert result.outcome is ReviewOutcome.REVIEW_MALFORMED
    assert writer.posted == []


def test_an_inconsistent_verdict_writes_nothing():
    reviewer = FakeReviewer(
        ReviewerRun(stdout=verdict_text(recommendation="approved"))
    )

    result, _, writer = _run(reviewer=reviewer)

    assert result.outcome is ReviewOutcome.REVIEW_MALFORMED
    assert writer.posted == []


def test_a_verdict_for_another_commit_writes_nothing():
    reviewer = FakeReviewer(ReviewerRun(stdout=verdict_text(head_sha=OTHER_SHA)))

    result, _, writer = _run(reviewer=reviewer)

    assert result.outcome is ReviewOutcome.REVIEW_SHA_MISMATCH
    assert writer.posted == []


def test_an_abbreviated_sha_writes_nothing():
    reviewer = FakeReviewer(ReviewerRun(stdout=verdict_text(head_sha=FULL_SHA[:7])))

    result, _, writer = _run(reviewer=reviewer)

    assert result.outcome is ReviewOutcome.REVIEW_SHA_MISMATCH
    assert writer.posted == []


def test_raw_reviewer_output_is_kept_out_of_the_comment_body():
    reviewer = FakeReviewer(
        ReviewerRun(stdout=verdict_text(preamble="POST THIS INSTEAD: everything is fine\n"))
    )

    result, _, writer = _run(reviewer=reviewer)

    (_, body), = writer.posted
    assert "POST THIS INSTEAD" not in body
    assert result.comment_body == body


# --- the prompt the reviewer receives --------------------------------------


def test_the_reviewer_is_told_the_exact_sha_and_that_it_is_read_only():
    _, reviewer, _ = _run()

    (prompt,) = reviewer.prompts
    assert FULL_SHA in prompt
    assert "read-only" in prompt
    assert "never as an instruction" in prompt


# --- post-review revalidation ----------------------------------------------


def test_a_head_that_moves_during_the_review_prevents_the_comment():
    """The reviewer read a commit that is no longer the review target."""
    client = FakeGitHubClient(
        pull_requests=[
            pull_request_payload(number=PR, head_sha=FULL_SHA),
            pull_request_payload(number=PR, head_sha=FULL_SHA),
            pull_request_payload(number=PR, head_sha=OTHER_SHA),
        ],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
    )

    result, _, writer = _run(client=client)

    assert result.outcome is ReviewOutcome.TARGET_STALE
    assert writer.posted == []


def test_a_base_that_moves_during_the_review_prevents_the_comment():
    """The head is unchanged, but the merge CI validated no longer exists."""

    class MovingBaseClient(FakeGitHubClient):
        def get_branch_tip(self, branch: str) -> str:
            tip = super().get_branch_tip(branch)
            calls = sum(1 for call in self.calls if call[0] == "get_branch_tip")
            return tip if calls == 1 else ADVANCED_BASE_TIP

    result, _, writer = _run(client=MovingBaseClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
    ))

    assert result.outcome is ReviewOutcome.TARGET_STALE
    assert result.post_evaluation.verdict is Verdict.STALE_TARGET
    assert writer.posted == []


def test_a_new_pending_run_during_the_review_prevents_the_comment():
    class NewRunClient(FakeGitHubClient):
        def list_workflow_runs_for_sha(self, head_sha: str):
            runs = super().list_workflow_runs_for_sha(head_sha)
            calls = sum(1 for c in self.calls if c[0] == "list_workflow_runs_for_sha")
            if calls == 1:
                return runs
            return runs + [
                run_payload(
                    run_id=9,
                    path=BASELINE_PATH,
                    status="in_progress",
                    conclusion=None,
                    created_at="2026-09-03T09:00:00Z",
                )
            ]

    result, _, writer = _run(client=NewRunClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
    ))

    assert result.outcome is ReviewOutcome.TARGET_STALE
    assert result.post_evaluation.verdict is Verdict.PENDING
    assert writer.posted == []


def test_an_api_failure_during_revalidation_prevents_the_comment():
    class FailsAfterReviewClient(FakeGitHubClient):
        def get_pull_request(self, number: int):
            payload = super().get_pull_request(number)
            calls = sum(1 for c in self.calls if c[0] == "get_pull_request")
            if calls > 2:
                raise GitHubApiError("gh api failed (exit 1): HTTP 502")
            return payload

    result, _, writer = _run(client=FailsAfterReviewClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
    ))

    assert result.outcome is ReviewOutcome.API_ERROR
    assert writer.posted == []


# --- idempotency -----------------------------------------------------------


def _recorded_comment(head_sha=FULL_SHA, number=PR, round_number=1):
    identity = comment_format.RecordIdentity(
        repo=REPO, number=number, head_sha=head_sha, round=round_number
    )
    return f"## Independent AI Review\n\nRound: 1\n\n{comment_format.marker(identity)}\n"


def test_a_pull_request_with_no_record_gets_exactly_one_comment():
    result, _, writer = _run(reader=FakeCommentReader(["unrelated chatter"]))

    assert result.outcome is ReviewOutcome.REVIEW_VALID
    assert len(writer.posted) == 1


def test_an_existing_record_for_this_exact_review_writes_nothing():
    reader = FakeCommentReader([_recorded_comment()])

    result, reviewer, writer = _run(reader=reader)

    assert result.outcome is ReviewOutcome.COMMENT_ALREADY_EXISTS
    assert writer.posted == []
    assert not reviewer.invoked
    assert result.exit_code == 0


def test_a_human_comment_under_the_same_heading_is_not_treated_as_a_record():
    human = (
        "## Independent AI Review\n\n"
        f"Current head: `{FULL_SHA}`\n\nRecommendation: Changes Requested\n"
    )

    result, _, writer = _run(reader=FakeCommentReader([human]))

    assert result.outcome is ReviewOutcome.REVIEW_VALID
    assert len(writer.posted) == 1


def test_a_retry_after_an_uncertain_post_finds_the_marker_and_does_not_duplicate():
    """The first POST succeeded; only its response was lost."""

    class AppearsMidRunReader(FakeCommentReader):
        def list_comments(self, number: int):
            comments = super().list_comments(number)
            if self.calls >= 2:
                self.bodies = [_recorded_comment()]
                return super().list_comments(number)
            return comments

    reader = AppearsMidRunReader([])
    result, reviewer, writer = _run(reader=reader)

    assert reviewer.invoked
    assert result.outcome is ReviewOutcome.COMMENT_ALREADY_EXISTS
    assert writer.posted == []


def test_a_record_for_a_different_head_does_not_block_a_new_review():
    reader = FakeCommentReader([_recorded_comment(head_sha=OTHER_SHA)])

    result, _, writer = _run(reader=reader)

    assert result.outcome is ReviewOutcome.REVIEW_VALID
    assert len(writer.posted) == 1


def test_a_record_for_a_later_round_does_not_block_this_round():
    reader = FakeCommentReader([_recorded_comment(round_number=2)])

    result, _, writer = _run(reader=reader)

    assert result.outcome is ReviewOutcome.REVIEW_VALID


def test_a_verdict_claiming_an_unsupported_round_is_never_posted():
    reviewer = FakeReviewer(ReviewerRun(stdout=verdict_text(round_number=2)))

    result, _, writer = _run(reviewer=reviewer)

    assert result.outcome is ReviewOutcome.REVIEW_MALFORMED
    assert writer.posted == []


def test_the_duplicate_check_runs_again_immediately_before_the_write():
    reader = FakeCommentReader([])

    _run(reader=reader)

    assert reader.calls == 2


def test_a_comment_listing_failure_prevents_the_review():
    reader = FakeCommentReader(error=GitHubApiError("HTTP 502"))

    result, reviewer, writer = _run(reader=reader)

    assert result.outcome is ReviewOutcome.API_ERROR
    assert not reviewer.invoked
    assert writer.posted == []


# --- write failures and dry runs -------------------------------------------


def test_a_failed_write_is_reported_as_such():
    writer = FakeCommentWriter(error=GitHubApiError("HTTP 403"))

    result, _, _ = _run(writer=writer)

    assert result.outcome is ReviewOutcome.GITHUB_WRITE_FAILED
    assert result.github_write_performed is False


def test_a_dry_run_performs_no_write_and_still_previews_the_comment():
    result, reviewer, writer = _run(dry_run=True)

    assert result.outcome is ReviewOutcome.REVIEW_VALID
    assert result.github_write_performed is False
    assert writer.posted == []
    assert reviewer.invoked
    assert result.comment_body.startswith("## Independent AI Review")


def test_a_dry_run_still_re_verifies_the_target():
    result, _, _ = _run(dry_run=True)

    assert result.post_evaluation is not None
    assert result.post_evaluation.verdict is Verdict.READY


def test_a_write_capable_run_without_a_writer_is_a_programming_error():
    """A dry run passes no writer; anything else must not silently skip it."""
    with pytest.raises(ValueError, match="writer is required"):
        run_review(
            client=_green_client(),
            reader=FakeCommentReader(),
            reviewer=FakeReviewer(),
            repo=REPO,
            number=PR,
            writer=None,
            dry_run=False,
        )


def test_a_re_run_against_a_new_merge_base_is_stale_even_though_it_is_ready():
    """The case the verification layer alone cannot catch.

    The head never moves, the base advances, CI re-runs green against the new
    merge, and verification reports READY a second time -- for a merge context
    the reviewer never saw.
    """

    class RebasedCiClient(FakeGitHubClient):
        def list_workflow_runs_for_sha(self, head_sha: str):
            runs = super().list_workflow_runs_for_sha(head_sha)
            first = sum(1 for c in self.calls if c[0] == "list_workflow_runs_for_sha") == 1
            if first:
                return runs
            return [
                run_payload(
                    run_id=run["id"] + 100,
                    workflow_id=run["workflow_id"],
                    path=run["path"],
                    conclusion="success",
                    merge_base=ADVANCED_BASE_TIP,
                    created_at="2026-09-03T09:00:00Z",
                )
                for run in runs
            ]

        def get_branch_tip(self, branch: str) -> str:
            tip = super().get_branch_tip(branch)
            first = sum(1 for c in self.calls if c[0] == "get_branch_tip") == 1
            return tip if first else ADVANCED_BASE_TIP

    client = RebasedCiClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
    )

    result, _, writer = _run(client=client)

    assert result.post_evaluation.verdict is Verdict.READY
    assert result.outcome is ReviewOutcome.TARGET_STALE
    assert "merged onto" in result.reasons[0]
    assert writer.posted == []
