"""``review-loop re-review`` end to end, against offline fakes."""

import io
import json


from review_loop import comment_format
from review_loop.cli import main
from review_loop.model import EXIT_USAGE
from review_loop.rereview import RE_REVIEW_EXIT_CODES, RE_REVIEW_ROUND, ReReviewOutcome
from review_loop.reviewer_process import ReviewerRun

from fakes import (
    AUTOMATION_LOGIN,
    BASE_TIP,
    BASELINE_PATH,
    FILTERED_PATH,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    FakeReviewer,
    pull_request_payload,
    run_payload,
)
from rereview_fakes import (
    PR,
    PUSHED_SHA,
    REVIEWED_SHA,
    fresh_block,
    push_document,
    rereview_text,
    resolution_block,
    review_document,
)


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _client():
    runs = [
        run_payload(
            run_id=1, path=BASELINE_PATH, head_sha=PUSHED_SHA, pr_number=PR,
            merge_base=BASE_TIP,
        ),
        run_payload(
            run_id=2, workflow_id=347481064, path=FILTERED_PATH, head_sha=PUSHED_SHA,
            pr_number=PR, merge_base=BASE_TIP,
        ),
    ]
    return FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=PUSHED_SHA)],
        runs=runs,
    )


def _resolved_both(**kwargs):
    return rereview_text(
        resolutions=(resolution_block("F1"), resolution_block("F2")), **kwargs
    )


def _invoke(tmp_path, *extra, review=None, push=None, reviewer=None, writer=None,
            reader=None, client=None):
    out = io.StringIO()
    argv = [
        "re-review",
        "--review-json",
        _write(tmp_path, "review.json", review if review is not None else review_document()),
        "--push-json",
        _write(tmp_path, "push.json", push if push is not None else push_document()),
        *extra,
    ]
    writer = writer if writer is not None else FakeCommentWriter()
    code = main(
        argv,
        client=client if client is not None else _client(),
        reader=reader if reader is not None else FakeCommentReader(),
        writer=writer,
        reviewer=reviewer
        if reviewer is not None
        else FakeReviewer(ReviewerRun(stdout=_resolved_both())),
        expected_author=AUTOMATION_LOGIN,
        stream=out,
    )
    return code, out.getvalue(), writer


# --- dispatch ---------------------------------------------------------------


def test_the_subcommand_is_dispatched_and_records_one_comment(tmp_path):
    code, text, writer = _invoke(tmp_path)

    assert code == 0
    assert "Outcome:              RE_REVIEW_VALID" in text
    assert len(writer.posted) == 1
    assert writer.posted[0][1].startswith(comment_format.RE_REVIEW_HEADING)


def test_the_bare_verification_form_is_untouched(tmp_path):
    # A guard on the by-name dispatch: 're-review' must not shadow anything.
    out = io.StringIO()
    code = main(["--pr", str(PR), "--dry-run"], client=_client(), stream=out)

    assert code == 0
    assert "Independent AI Re-Review" not in out.getvalue()


# --- input refusals ---------------------------------------------------------


def test_a_missing_document_is_an_input_refusal(tmp_path):
    out = io.StringIO()
    code = main(
        [
            "re-review",
            "--review-json",
            str(tmp_path / "absent.json"),
            "--push-json",
            _write(tmp_path, "push.json", push_document()),
        ],
        client=_client(),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        reviewer=FakeReviewer(),
        expected_author=AUTOMATION_LOGIN,
        stream=out,
    )

    assert code == RE_REVIEW_EXIT_CODES[ReReviewOutcome.RE_REVIEW_INPUT_INVALID]


def test_a_push_that_is_not_push_ready_is_an_input_refusal(tmp_path):
    code, _, writer = _invoke(tmp_path, push=push_document(outcome="CI_PENDING"))

    assert code == RE_REVIEW_EXIT_CODES[ReReviewOutcome.RE_REVIEW_INPUT_INVALID]
    assert writer.posted == []


def test_a_mismatched_repo_flag_is_an_input_refusal(tmp_path):
    code, _, writer = _invoke(tmp_path, "--repo", "someone/else")

    assert code == RE_REVIEW_EXIT_CODES[ReReviewOutcome.RE_REVIEW_INPUT_INVALID]
    assert writer.posted == []


def test_a_reviewer_command_is_required_when_none_is_injected(tmp_path):
    out = io.StringIO()
    code = main(
        [
            "re-review",
            "--review-json",
            _write(tmp_path, "review.json", review_document()),
            "--push-json",
            _write(tmp_path, "push.json", push_document()),
        ],
        client=_client(),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        expected_author=AUTOMATION_LOGIN,
        stream=out,
    )

    assert code == EXIT_USAGE
    assert "--reviewer-command is required" in out.getvalue()


# --- output -----------------------------------------------------------------


def test_the_text_report_keeps_the_two_facts_apart(tmp_path):
    reviewer = FakeReviewer(
        ReviewerRun(
            stdout=rereview_text(
                recommendation="changes_requested",
                resolutions=(
                    resolution_block("F1"),
                    resolution_block(
                        "F2", "UNRESOLVED", evidence="unchanged", reason="not touched"
                    ),
                ),
                fresh=(fresh_block("R2.F1", severity="Major"),),
            )
        )
    )
    _, text, _ = _invoke(tmp_path, reviewer=reviewer)

    assert "RESOLVED F1" in text
    assert "UNRESOLVED F2" in text
    assert "Fresh findings:       1" in text
    assert "Major 1" in text


def test_the_json_report_carries_resolutions_and_fresh_findings_separately(tmp_path):
    reviewer = FakeReviewer(
        ReviewerRun(
            stdout=rereview_text(
                recommendation="changes_requested",
                resolutions=(
                    resolution_block("F1"),
                    resolution_block(
                        "F2", "UNRESOLVED", evidence="unchanged", reason="not touched"
                    ),
                ),
                fresh=(fresh_block("R2.F1", severity="Major"),),
            )
        )
    )
    code, text, _ = _invoke(tmp_path, "--json", reviewer=reviewer)
    payload = json.loads(text)

    assert code == 0
    assert payload["outcome"] == "RE_REVIEW_VALID"
    assert payload["round"] == RE_REVIEW_ROUND
    assert payload["request"]["pushed_fix_sha"] == PUSHED_SHA
    assert payload["request"]["original_head_sha"] == REVIEWED_SHA
    assert payload["request"]["original_finding_ids"] == ["F1", "F2"]

    rereview = payload["rereview"]
    assert [r["finding_id"] for r in rereview["resolutions"]] == ["F1", "F2"]
    assert rereview["unresolved_finding_ids"] == ["F2"]
    assert [f["finding_id"] for f in rereview["fresh_findings"]] == ["R2.F1"]
    assert rereview["fresh_major"] == 1
    assert rereview["fresh_major_findings_present"] is True
    assert rereview["fresh_blocking_findings_present"] is False
    # No combined status anywhere: the two collections are the answer.
    assert "resolved" not in payload
    assert "merge_ready" not in payload
    # And no key whose name reads across both collections while counting one.
    assert not [key for key in rereview if key.endswith("_findings_remain")]


def test_an_unresolved_original_major_is_not_reported_as_no_major_finding(tmp_path):
    """The regression the fresh-only booleans are named for.

    An original Major finding is unresolved and this turn raised nothing new.
    Every severity key here counts fresh findings, so they are all zero --
    which is only safe to publish because each one says `fresh` in its name
    and `unresolved_finding_ids` carries the other half.
    """
    reviewer = FakeReviewer(
        ReviewerRun(
            stdout=rereview_text(
                recommendation="changes_requested",
                resolutions=(
                    resolution_block(
                        "F1", "UNRESOLVED", evidence="still returns 200",
                        reason="the handler was not touched",
                    ),
                    resolution_block("F2"),
                ),
            )
        )
    )
    _, text, _ = _invoke(tmp_path, "--json", reviewer=reviewer)
    rereview = json.loads(text)["rereview"]

    assert rereview["unresolved_finding_ids"] == ["F1"]
    assert rereview["fresh_major"] == 0
    assert rereview["fresh_major_findings_present"] is False
    assert "major_findings_remain" not in rereview


def test_a_dry_run_writes_nothing_and_prints_the_comment(tmp_path):
    code, text, writer = _invoke(tmp_path, "--dry-run")

    assert code == 0
    assert writer.posted == []
    assert "comment that would be recorded" in text
    assert comment_format.RE_REVIEW_HEADING in text


def test_a_malformed_re_review_never_creates_a_comment(tmp_path):
    reviewer = FakeReviewer(ReviewerRun(stdout="all good"))
    code, _, writer = _invoke(tmp_path, reviewer=reviewer)

    assert code == RE_REVIEW_EXIT_CODES[ReReviewOutcome.RE_REVIEW_MALFORMED]
    assert writer.posted == []


def test_raw_output_is_withheld_unless_it_is_asked_for(tmp_path, capsys):
    reviewer = FakeReviewer(ReviewerRun(stdout="secret prose"))
    _invoke(tmp_path, reviewer=reviewer)

    assert "secret prose" not in capsys.readouterr().err


def test_raw_output_can_be_printed_for_debugging(tmp_path, capsys):
    reviewer = FakeReviewer(ReviewerRun(stdout="secret prose"))
    _invoke(tmp_path, "--print-raw-output", reviewer=reviewer)

    assert "secret prose" in capsys.readouterr().err


def test_a_second_run_against_the_same_target_writes_nothing(tmp_path):
    _, _, writer = _invoke(tmp_path)
    reader = FakeCommentReader([writer.posted[0][1]])
    code, _, second = _invoke(tmp_path, reader=reader)

    assert code == 0
    assert second.posted == []


def test_the_help_text_states_that_zero_is_not_a_merge_decision():
    from review_loop.rereview_cli import build_rereview_parser

    epilog = build_rereview_parser().epilog

    assert "does not mean the findings were resolved" in epilog
    assert "two separate facts" in epilog


def test_the_help_documents_every_re_review_outcome_exit_code():
    """The epilog is the operator's copy of the contract; keep it complete."""
    from review_loop.rereview_cli import build_rereview_parser

    text = build_rereview_parser().format_help()

    for outcome, code in RE_REVIEW_EXIT_CODES.items():
        assert outcome.value in text
        assert str(code) in text


def test_target_not_ready_is_the_one_outcome_without_a_code_of_its_own():
    """It reports the verification verdict's own code, so it must not gain one.

    Asserted rather than assumed because the table would otherwise look
    incomplete to a later reader, who might "fix" it by inventing a code and
    silently duplicate the PENDING / FAILED / AMBIGUOUS / STALE_TARGET
    vocabulary this stage deliberately reuses.
    """
    assert set(RE_REVIEW_EXIT_CODES) == set(ReReviewOutcome) - {
        ReReviewOutcome.TARGET_NOT_READY
    }


def test_re_review_exit_codes_do_not_collide_with_the_earlier_commands():
    from review_loop.fix_response import FIX_EXIT_CODES
    from review_loop.model import EXIT_CODES
    from review_loop.push_response import PUSH_EXIT_CODES
    from review_loop.verdict import REVIEW_EXIT_CODES

    earlier = (
        {code for code in EXIT_CODES.values() if code}
        | {code for code in REVIEW_EXIT_CODES.values() if code}
        | {code for code in FIX_EXIT_CODES.values() if code}
        | {code for code in PUSH_EXIT_CODES.values() if code}
    )
    rereview = {code for code in RE_REVIEW_EXIT_CODES.values() if code}

    assert not earlier & rereview
    assert len(rereview) == len([c for c in RE_REVIEW_EXIT_CODES.values() if c])
