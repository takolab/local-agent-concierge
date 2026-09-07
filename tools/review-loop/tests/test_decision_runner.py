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
    records,
    rereview_retry_document,
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
    documents = (
        chain(tmp_path, reviewer_output=reviewer_output)
        if documents is None
        else documents
    )
    request = load_request(*documents)
    writer = FakeCommentWriter() if writer is None else writer
    result = run_decision(
        client=green_client() if client is None else client,
        reader=records(documents[2]) if reader is None else reader,
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


# -- the re-review's own record, confirmed rather than assumed ----------------


def test_the_brief_names_the_comment_that_records_its_re_review(tmp_path):
    documents = chain(tmp_path)
    reader = records(documents[2])
    result, _ = run(tmp_path, documents=documents, reader=reader)

    recorded_id = reader.list_comments(PR)[0].comment_id
    assert result.rereview_record_id == recorded_id
    assert f"Re-review record: comment {recorded_id}" in result.brief


def test_a_re_review_that_is_not_recorded_produces_no_brief(tmp_path):
    """A brief must never cite evidence a human cannot go and read.

    The documents are valid and the pull request is current; what is missing
    is the re-review comment itself -- deleted, or never written by the run
    that produced the document. Read back rather than assumed, so the
    difference is caught here instead of appearing as a brief pointing at
    nothing.
    """
    result, writer = run(tmp_path, reader=FakeCommentReader())

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert result.rereview_record_id is None
    assert writer.posted == []
    assert "is not there for a human to read" in " ".join(result.reasons)


def test_a_re_review_record_from_another_account_is_not_this_evidence(tmp_path):
    """The same provenance rule the duplicate checks use, applied to the input.

    The marker is public and deterministic, so a copy of it in someone else's
    comment proves only that someone wrote one.
    """
    documents = chain(tmp_path)
    body = json.loads(documents[2])["comment_body"]
    reader = FakeCommentReader([("someone-else", body)])

    result, writer = run(tmp_path, documents=documents, reader=reader)

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []


def test_a_record_for_another_merge_context_is_not_this_evidence(tmp_path):
    """A re-review of the same commit onto a different base is a different record."""
    other = json.loads(chain(tmp_path, merge_base=ADVANCED_BASE_TIP)[2])["comment_body"]
    result, writer = run(tmp_path, reader=FakeCommentReader([other]))

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []


def test_a_lost_write_response_is_resolved_by_reading_the_pull_request(tmp_path):
    """The end-to-end retry recovery, over the real documents.

    Run one: the POST lands and the response is lost, so the re-review turn
    reports `GITHUB_WRITE_FAILED` while the comment is really there. That
    document carries the validated re-review, and this turn settles the part
    it could not: the record is on the pull request, so the brief is produced.
    """
    review, push, rereview = chain(tmp_path)
    body = json.loads(rereview)["comment_body"]
    lost = rereview_retry_document(
        tmp_path,
        review=review,
        push=push,
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(error=GitHubApiError("HTTP 502 (response lost)")),
    )
    assert json.loads(lost)["outcome"] == "GITHUB_WRITE_FAILED"

    result, writer = run(
        tmp_path,
        documents=(review, push, lost),
        reader=FakeCommentReader([body]),
    )

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert result.next_action is NextAction.READY_FOR_HUMAN_MERGE_DECISION
    assert result.rereview_record_id is not None
    assert len(writer.posted) == 1


def test_a_write_that_really_failed_produces_no_brief(tmp_path):
    """The other half of the same document: the POST was rejected.

    Same outcome label, same carried re-review, and no comment on the pull
    request. The label cannot tell the two apart; reading the pull request
    can, and does.
    """
    review, push, _ = chain(tmp_path)
    lost = rereview_retry_document(
        tmp_path,
        review=review,
        push=push,
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(error=GitHubApiError("HTTP 422 (rejected)")),
    )

    result, writer = run(
        tmp_path, documents=(review, push, lost), reader=FakeCommentReader()
    )

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []


def test_the_record_is_not_looked_up_when_the_state_is_already_stale(tmp_path):
    """A record for a merge context nobody is looking at settles nothing."""
    reader = FakeCommentReader()
    run(tmp_path, client=green_client(base_tip=ADVANCED_BASE_TIP), reader=reader)
    assert reader.calls == 0


def test_one_listing_answers_both_questions(tmp_path):
    """The re-review's record and the brief's own duplicate check share a read."""
    documents = chain(tmp_path)
    reader = records(documents[2])
    run(tmp_path, documents=documents, reader=reader)
    assert reader.calls == 1


# -- the record's contents, not just its identity -----------------------------


def test_the_confirmed_record_is_the_rendering_of_the_supplied_evidence(tmp_path):
    """The positive control, and the property the refusals below rest on.

    A re-review comment is a deterministic function of the validated model and
    the target it was verified against, so the model carried in the document
    renders back to exactly the bytes that were recorded. If that stopped
    being true, every check in this section would silently become a refusal.
    """
    documents = chain(tmp_path, reviewer_output=fresh_major())
    request = load_request(*documents)
    recorded_body = json.loads(documents[2])["comment_body"]

    rendered = comment_format.render_rereview(
        request.recorded_target, request.chain, request.rereview
    )
    assert comment_format.rendered_record_matches(recorded_body, rendered)

    result, _ = run(tmp_path, documents=documents, reader=records(documents[2]))
    assert result.outcome is DecisionOutcome.BRIEF_RECORDED
    assert result.rereview_record_id is not None


def test_a_different_re_review_of_the_same_state_is_not_this_evidence(tmp_path):
    """Identity is not contents, and the difference decides the brief.

    The pull request records a re-review reporting a fresh Major finding. The
    supplied document is an internally valid re-review of the same repository,
    pull request, head, merge base and round -- everything `RecordIdentity`
    names -- that resolved everything and found nothing. Its marker matches
    the recorded comment, so an identity-only check confirms it and the brief
    reads READY while citing a comment that says the opposite.
    """
    recorded = json.loads(chain(tmp_path, reviewer_output=fresh_major())[2])
    supplied = chain(tmp_path)

    # The gap being closed: the two are indistinguishable by identity alone.
    request = load_request(*supplied)
    identity = comment_format.rereview_identity_for(
        request.recorded_target, request.rereview
    )
    assert comment_format.body_records(recorded["comment_body"], identity)

    result, writer = run(
        tmp_path,
        documents=supplied,
        reader=FakeCommentReader([recorded["comment_body"]]),
    )

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert result.next_action is not NextAction.READY_FOR_HUMAN_MERGE_DECISION
    assert result.rereview_record_id is None
    assert writer.posted == []
    assert "not the rendering of the re-review supplied here" in " ".join(result.reasons)


def test_the_reverse_direction_is_refused_too(tmp_path):
    """Recorded `approved`, supplied a fresh Major: same marker, different evidence.

    Refused for the same reason and not because one classification is worse
    than the other -- the check is about which re-review is recorded, not
    about what it concluded.
    """
    recorded = json.loads(chain(tmp_path)[2])
    supplied = chain(tmp_path, reviewer_output=fresh_major())

    result, writer = run(
        tmp_path,
        documents=supplied,
        reader=FakeCommentReader([recorded["comment_body"]]),
    )

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []


def test_a_lost_response_document_is_refused_when_another_run_recorded_something_else(
    tmp_path,
):
    """The route this gap is actually reached by, end to end.

    Run A's `POST` is rejected, so it reports `GITHUB_WRITE_FAILED` while
    carrying its own validated model. Run B's reviewer then reaches a
    different conclusion and records it. A's document is legitimate, its
    identity matches B's record, and briefing it would report A's findings
    while citing B's comment.
    """
    review, push, _ = chain(tmp_path)
    run_a = rereview_retry_document(
        tmp_path,
        review=review,
        push=push,
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(error=GitHubApiError("HTTP 422 (rejected)")),
    )
    assert json.loads(run_a)["outcome"] == "GITHUB_WRITE_FAILED"

    run_b_body = json.loads(
        chain(tmp_path, review=review, push=push, reviewer_output=fresh_major())[2]
    )["comment_body"]

    result, writer = run(
        tmp_path,
        documents=(review, push, run_a),
        reader=FakeCommentReader([run_b_body]),
    )

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []
    assert "different re-reviews of the same integration state" in " ".join(
        result.reasons
    )


def test_an_edited_re_review_comment_is_no_longer_the_record(tmp_path):
    """A record that was changed after it was written is not the evidence.

    The marker survives an edit; the rendering does not, which is the point.
    """
    documents = chain(tmp_path)
    tampered = json.loads(documents[2])["comment_body"].replace(
        "RESOLVED: F1, F2", "RESOLVED: F1"
    )
    result, writer = run(
        tmp_path, documents=documents, reader=FakeCommentReader([tampered])
    )

    assert result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT
    assert writer.posted == []


def test_line_endings_do_not_decide_whether_a_record_matches(tmp_path):
    """Transport, not text. GitHub may hand back CRLF for a body posted as LF."""
    documents = chain(tmp_path)
    crlf = json.loads(documents[2])["comment_body"].replace("\n", "\r\n")
    result, _ = run(
        tmp_path, documents=documents, reader=FakeCommentReader([crlf])
    )

    assert result.outcome is DecisionOutcome.BRIEF_RECORDED


# -- identity and idempotency -------------------------------------------------


def test_an_exact_retry_writes_nothing(tmp_path):
    documents = chain(tmp_path)
    first, writer = run(tmp_path, documents=documents)
    assert first.github_write_performed is True

    reader = records(documents[2], writer.posted[0][1])
    second, second_writer = run(tmp_path, documents=documents, reader=reader)

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

    documents = chain(tmp_path, merge_base=ADVANCED_BASE_TIP)
    result, writer = run(
        tmp_path,
        documents=documents,
        client=green_client(merge_base=ADVANCED_BASE_TIP),
        reader=records(documents[2], stale_brief),
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
    documents = chain(tmp_path)
    first, writer = run(tmp_path, documents=documents)
    reader = records(documents[2], ("someone-else", writer.posted[0][1]))

    result, second_writer = run(tmp_path, documents=documents, reader=reader)

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
    documents = chain(tmp_path)
    request = load_request(*documents)
    result = run_decision(
        client=green_client(),
        reader=records(documents[2]),
        writer=None,
        request=request,
        expected_author=AUTOMATION_LOGIN,
        dry_run=True,
    )
    assert result.outcome is DecisionOutcome.BRIEF_RECORDED


def test_a_real_run_without_a_writer_is_a_programming_error(tmp_path):
    documents = chain(tmp_path)
    request = load_request(*documents)
    with pytest.raises(ValueError, match="writer is required"):
        run_decision(
            client=green_client(),
            reader=records(documents[2]),
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


def test_a_comment_listing_failure_classifies_nothing_and_writes_nothing(tmp_path):
    """Fail closed, and specifically: do not classify.

    The listing answers whether the re-review is recorded at all, so a run
    that could not read it does not know whether the evidence a brief would
    cite exists. Reporting `READY_FOR_HUMAN_MERGE_DECISION` from the findings
    alone would be a decision-shaped answer derived from evidence this turn
    could not confirm.
    """
    reader = FakeCommentReader(error=GitHubApiError("HTTP 502"))
    result, writer = run(tmp_path, reader=reader)

    assert result.outcome is DecisionOutcome.API_ERROR
    assert writer.posted == []
    assert result.next_action is None
    assert result.brief is None


def test_a_brief_above_githubs_comment_limit_is_not_posted(tmp_path, monkeypatch):
    monkeypatch.setattr("review_loop.decision_runner.MAX_COMMENT_CHARS", 10)
    result, writer = run(tmp_path)

    assert result.outcome is DecisionOutcome.GITHUB_WRITE_FAILED
    assert writer.posted == []
    assert "comment limit" in " ".join(result.reasons)
