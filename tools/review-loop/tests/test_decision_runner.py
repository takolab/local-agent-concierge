"""The merge-brief turn against a live pull request: currency, identity, limits.

Every test here drives the real command through the real chain loader, with
GitHub replaced by the same offline fakes the other turns use.
"""

from __future__ import annotations

import json

import pytest

from review_loop import comment_format
from review_loop.decision import DECISION_EXIT_CODES, DecisionOutcome, NextAction
from review_loop.decision_input import load_request
from review_loop.decision_runner import run_decision
from review_loop.github_client import GitHubApiError
from review_loop.model import short_sha

from fakes import (
    ADVANCED_BASE_TIP,
    AUTOMATION_LOGIN,
    BASE_TIP,
    BASELINE_PATH,
    FILTERED_PATH,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    FailingGitHubClient,
    pull_request_payload,
    run_payload,
)
from decision_fakes import (
    chain,
    fresh_blocking,
    fresh_major,
    fresh_minor,
    green_client,
    one_escalated,
    one_unresolved,
)
from rereview_fakes import LATER_SHA, PR, PUSHED_SHA


def run(
    tmp_path,
    *,
    reviewer_output=None,
    documents=None,
    client=None,
    reader=None,
    writer=None,
    dry_run=False,
):
    request = load_request(
        *(chain(tmp_path, reviewer_output=reviewer_output)
          if documents is None
          else documents)
    )
    writer = FakeCommentWriter() if writer is None else writer
    result = run_decision(
        client=green_client() if client is None else client,
        reader=FakeCommentReader() if reader is None else reader,
        writer=None if dry_run else writer,
        request=request,
        expected_author=AUTOMATION_LOGIN,
        dry_run=dry_run,
    )
    return result, writer


# -- the happy path -----------------------------------------------------------


def test_a_current_clean_chain_records_one_brief(tmp_path):
    result, writer = run(tmp_path)

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert result.next_action is NextAction.READY_FOR_HUMAN_MERGE_DECISION
    assert result.exit_code == 0
    assert result.github_write_performed is True
    assert len(writer.posted) == 1
    number, body = writer.posted[0]
    assert number == PR
    assert body.startswith(comment_format.MERGE_BRIEF_HEADING)


def test_the_brief_states_the_exact_state_it_describes(tmp_path):
    result, _ = run(tmp_path)
    body = result.brief

    assert f"Head SHA: {PUSHED_SHA}" in body
    assert f"Base: master at {BASE_TIP}" in body
    assert "Authoritative CI: READY" in body
    assert BASELINE_PATH in body and FILTERED_PATH in body
    assert f"Pull request: #{PR}" in body


def test_the_brief_states_the_evidence_chain(tmp_path):
    request = load_request(*chain(tmp_path))
    result, _ = run(tmp_path)
    body = result.brief

    assert f"Review identity: {request.source_review_sha256}" in body
    assert f"Original review: round 1 of {request.original_head_sha}" in body
    assert f"Fix commit: {PUSHED_SHA} (parent {request.original_head_sha})" in body
    assert f"Re-review: round 2 of {PUSHED_SHA}" in body


def test_the_brief_names_the_next_action_and_the_human_decision(tmp_path):
    result, _ = run(tmp_path)
    assert "Next action: READY_FOR_HUMAN_MERGE_DECISION" in result.brief
    assert (
        "Human decision required: merge / do not merge / request another fix / "
        "escalate" in result.brief
    )
    assert "not an approval and not a merge" in result.brief


def test_the_brief_keeps_original_resolutions_and_fresh_findings_apart(tmp_path):
    """The regression the brief format exists to prevent.

    ``F1`` was raised Major in round 1 and is RESOLVED; ``R2.F1`` is a Major
    finding this round raised. A brief that reported one "Major findings: 1"
    would state neither fact.
    """
    result, _ = run(tmp_path, reviewer_output=fresh_major())
    body = result.brief

    assert "F1 — Major — RESOLVED" in body
    assert "F2 — Minor — RESOLVED" in body
    assert "R2.F1 — Major — " in body
    assert "Unresolved original findings: (none)" in body
    assert "Fresh Major: R2.F1" in body
    assert result.next_action is NextAction.FIX_REQUIRED


def test_an_unresolved_original_finding_carries_its_reason_into_the_brief(tmp_path):
    result, _ = run(tmp_path, reviewer_output=one_unresolved())
    assert "F1 — Major — UNRESOLVED — The handler was renamed" in result.brief
    assert "Unresolved original findings: F1" in result.brief
    assert result.next_action is NextAction.FIX_REQUIRED


def test_a_fresh_minor_finding_is_surfaced_in_a_ready_brief(tmp_path):
    result, _ = run(tmp_path, reviewer_output=fresh_minor())
    assert result.next_action is NextAction.READY_FOR_HUMAN_MERGE_DECISION
    assert "Fresh Minor: R2.F1" in result.brief
    assert "R2.F1 — Minor — " in result.brief


def test_a_fresh_blocking_finding_produces_an_escalation_brief(tmp_path):
    result, _ = run(tmp_path, reviewer_output=fresh_blocking())
    assert result.next_action is NextAction.HUMAN_ESCALATION
    assert "Next action: HUMAN_ESCALATION" in result.brief
    assert "Fresh Blocking: R2.F1" in result.brief
    # Still recorded: an escalation is a current answer, not a failure to give
    # one.
    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert result.exit_code == 0


def test_an_escalated_original_finding_produces_an_escalation_brief(tmp_path):
    result, _ = run(tmp_path, reviewer_output=one_escalated())
    assert result.next_action is NextAction.HUMAN_ESCALATION
    assert "Escalated original findings: F1" in result.brief
    assert "F1 — Major — ESCALATE — Whether the new contract" in result.brief


# -- current-state revalidation ----------------------------------------------


def test_a_head_that_moved_produces_no_decision_ready_brief(tmp_path):
    """Case 7: the re-review targeted H2 and the pull request is now at H3."""
    moved = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=LATER_SHA)],
        runs=[
            run_payload(
                run_id=9, path=BASELINE_PATH, head_sha=LATER_SHA, pr_number=PR,
                merge_base=BASE_TIP,
            ),
            run_payload(
                run_id=10, workflow_id=347481064, path=FILTERED_PATH,
                head_sha=LATER_SHA, pr_number=PR, merge_base=BASE_TIP,
            ),
        ],
    )
    result, writer = run(tmp_path, client=moved)

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert result.next_action is NextAction.EVIDENCE_NOT_CURRENT
    assert result.exit_code == DECISION_EXIT_CODES[DecisionOutcome.EVIDENCE_NOT_CURRENT]
    assert writer.posted == []
    assert result.github_write_performed is False
    assert LATER_SHA in " ".join(result.reasons)


def test_the_same_head_with_an_advanced_base_is_not_current(tmp_path):
    """Case 8: same commit, different integration state, stale CI evidence.

    The head has not moved and CI for it is still green -- against a base that
    is no longer the branch tip. Nobody has re-reviewed the pull request as it
    would now merge.
    """
    advanced = green_client(base_tip=ADVANCED_BASE_TIP)
    result, writer = run(tmp_path, client=advanced)

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []
    reasons = " ".join(result.reasons)
    assert "not READY" in reasons
    assert short_sha(ADVANCED_BASE_TIP) in reasons


def test_ci_that_is_no_longer_green_is_not_current(tmp_path):
    failing = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=PUSHED_SHA)],
        runs=[
            run_payload(
                run_id=1, path=BASELINE_PATH, head_sha=PUSHED_SHA, pr_number=PR,
                merge_base=BASE_TIP, conclusion="failure",
            ),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH,
                head_sha=PUSHED_SHA, pr_number=PR, merge_base=BASE_TIP,
            ),
        ],
    )
    result, writer = run(tmp_path, client=failing)

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert "not READY" in " ".join(result.reasons)
    assert writer.posted == []


def test_a_stale_chain_gets_a_diagnostic_that_is_not_a_record(tmp_path):
    """It explains itself without ever becoming this state's decision.

    The diagnostic carries no machine marker, so a later run cannot find it
    and report that a brief already exists for a state nobody briefed.
    """
    result, _ = run(tmp_path, client=green_client(base_tip=ADVANCED_BASE_TIP))

    assert result.brief is not None
    assert "not produced: evidence is not current" in result.brief
    assert comment_format.parse_markers(result.brief) == ()
    assert "Why this is not decision-ready:" in result.brief


def test_a_stale_chain_still_reports_what_the_re_review_said(tmp_path):
    """History is not erased by being out of date, only relabelled."""
    result, _ = run(
        tmp_path,
        reviewer_output=one_unresolved(),
        client=green_client(base_tip=ADVANCED_BASE_TIP),
    )
    assert result.facts is not None
    assert result.facts.unresolved_original_finding_ids == ("F1",)
    assert result.facts.evidence_current is False


def test_a_pull_request_that_cannot_be_read_is_an_api_error_not_a_decision(tmp_path):
    result, writer = run(tmp_path, client=FailingGitHubClient())

    assert result.outcome is DecisionOutcome.API_ERROR
    assert result.next_action is None
    assert writer.posted == []


def test_a_closed_pull_request_is_not_current(tmp_path):
    closed = FakeGitHubClient(
        pull_requests=[
            pull_request_payload(number=PR, head_sha=PUSHED_SHA, state="closed")
        ],
        runs=[
            run_payload(
                run_id=1, path=BASELINE_PATH, head_sha=PUSHED_SHA, pr_number=PR,
                merge_base=BASE_TIP,
            ),
        ],
    )
    result, writer = run(tmp_path, client=closed)
    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []


# -- identity and idempotency -------------------------------------------------


def test_an_exact_retry_writes_nothing(tmp_path):
    first, writer = run(tmp_path)
    assert first.github_write_performed is True

    reader = FakeCommentReader([writer.posted[0][1]])
    second, second_writer = run(tmp_path, reader=reader)

    assert second.outcome is DecisionOutcome.COMMENT_ALREADY_EXISTS
    assert second.exit_code == 0
    assert second_writer.posted == []
    assert second.existing_comment_id is not None
    # The classification is still reported: the state has not changed, so the
    # answer has not either.
    assert second.next_action is NextAction.READY_FOR_HUMAN_MERGE_DECISION


def test_a_brief_for_another_merge_context_does_not_suppress_this_one(tmp_path):
    """The regression this identity model exists for.

    Same pull request, same head, a base that advanced and CI that re-ran
    green against it. That is a different integration state, and the earlier
    brief is not a decision about it.
    """
    stale_brief = comment_format.render_merge_brief(
        *_target_and_parts(tmp_path, merge_base=BASE_TIP)
    )
    assert BASE_TIP in stale_brief

    result, writer = run(
        tmp_path,
        documents=chain(tmp_path, merge_base=ADVANCED_BASE_TIP),
        client=green_client(merge_base=ADVANCED_BASE_TIP),
        reader=FakeCommentReader([stale_brief]),
    )

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert len(writer.posted) == 1
    assert ADVANCED_BASE_TIP in writer.posted[0][1]


def _target_and_parts(tmp_path, *, merge_base):
    """Render arguments for a brief about ``merge_base``, via the real path."""
    from review_loop.decision import classify, gather_facts
    from review_loop.review_target import ReviewTarget

    request = load_request(*chain(tmp_path))
    target = ReviewTarget(
        repo=request.recorded_target.repo,
        number=request.recorded_target.number,
        head_sha=request.recorded_target.head_sha,
        base_ref=request.recorded_target.base_ref,
        ci_merge_base_sha=merge_base,
    )
    facts = gather_facts(request.original_findings, request.rereview)
    return target, request, facts, classify(facts)


def test_a_marker_from_another_account_does_not_suppress_a_brief(tmp_path):
    first, writer = run(tmp_path)
    forged = [("someone-else", writer.posted[0][1])]

    result, second_writer = run(tmp_path, reader=FakeCommentReader(forged))

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert len(second_writer.posted) == 1


def test_a_re_review_record_does_not_suppress_a_brief(tmp_path):
    """Different role, same commit and merge context: two distinct artifacts."""
    rereview_comment = json.loads(chain(tmp_path)[2])["comment_body"]
    result, writer = run(tmp_path, reader=FakeCommentReader([rereview_comment]))

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert len(writer.posted) == 1


def test_the_brief_marker_names_the_current_state_and_its_own_role(tmp_path):
    result, _ = run(tmp_path)
    (identity,) = comment_format.parse_markers(result.brief)

    assert identity.head_sha == PUSHED_SHA
    assert identity.base_sha == BASE_TIP
    assert identity.number == PR
    assert identity.round == 2
    assert identity.role == comment_format.DECISION_ROLE
    assert identity.role != comment_format.RE_REVIEWER_ROLE


# -- writes, and the absence of them -----------------------------------------


def test_a_dry_run_writes_nothing_and_still_classifies(tmp_path):
    result, writer = run(tmp_path, dry_run=True, reviewer_output=fresh_major())

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert result.next_action is NextAction.FIX_REQUIRED
    assert result.github_write_performed is False
    assert writer.posted == []
    assert result.brief is not None


def test_a_dry_run_requires_no_writer_at_all(tmp_path):
    request = load_request(*chain(tmp_path))
    result = run_decision(
        client=green_client(),
        reader=FakeCommentReader(),
        writer=None,
        request=request,
        expected_author=AUTOMATION_LOGIN,
        dry_run=True,
    )
    assert result.outcome is DecisionOutcome.BRIEF_RECORDED


def test_a_real_run_without_a_writer_is_a_programming_error(tmp_path):
    request = load_request(*chain(tmp_path))
    with pytest.raises(ValueError, match="writer is required"):
        run_decision(
            client=green_client(),
            reader=FakeCommentReader(),
            writer=None,
            request=request,
            expected_author=AUTOMATION_LOGIN,
        )


def test_a_failed_write_is_reported_without_a_second_attempt(tmp_path):
    writer = FakeCommentWriter(error=GitHubApiError("HTTP 503"))
    result, _ = run(tmp_path, writer=writer)

    assert result.outcome is DecisionOutcome.GITHUB_WRITE_FAILED
    assert result.exit_code == DECISION_EXIT_CODES[DecisionOutcome.GITHUB_WRITE_FAILED]
    assert len(writer.posted) == 1
    assert result.github_write_performed is False


def test_the_turn_only_reads_github_and_posts_one_comment(tmp_path):
    """The authority boundary, asserted on the calls actually made.

    No merge, no push, no reviewer, no Coding Agent -- there is no such
    collaborator threaded through this turn at all, and the client it does
    hold answers only the read-only questions verification asks.
    """
    client = green_client()
    result, writer = run(tmp_path, client=client)

    assert result.github_write_performed is True
    assert len(writer.posted) == 1
    assert {call[0] for call in client.calls} <= {
        "get_pull_request",
        "list_workflow_runs_for_sha",
        "list_workflow_files",
        "list_pull_request_files",
        "get_branch_tip",
    }


def test_a_comment_listing_failure_does_not_write(tmp_path):
    reader = FakeCommentReader(error=GitHubApiError("HTTP 502"))
    result, writer = run(tmp_path, reader=reader)

    assert result.outcome is DecisionOutcome.API_ERROR
    assert writer.posted == []
    # The classification is still reported: it was derived before the read
    # failed, and it is still what the evidence says.
    assert result.next_action is NextAction.READY_FOR_HUMAN_MERGE_DECISION


def test_a_brief_above_githubs_comment_limit_is_not_posted(tmp_path, monkeypatch):
    monkeypatch.setattr("review_loop.decision_runner.MAX_COMMENT_CHARS", 10)
    result, writer = run(tmp_path)

    assert result.outcome is DecisionOutcome.GITHUB_WRITE_FAILED
    assert writer.posted == []
    assert "comment limit" in " ".join(result.reasons)
