"""``review-loop review`` at the command line, against offline fakes."""

import io
import json

import pytest

from review_loop import cli
from review_loop.github_client import GitHubApiError
from review_loop.model import EXIT_CODES, EXIT_USAGE, Verdict
from review_loop.review_cli import build_review_parser
from review_loop.reviewer_process import ReviewerRun
from review_loop.verdict import REVIEW_EXIT_CODES, ReviewOutcome

from fakes import (
    AUTOMATION_LOGIN,
    BASE_TIP,
    BASELINE_PATH,
    FILTERED_PATH,
    FULL_SHA,
    OTHER_SHA,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    FakeReviewer,
    pull_request_payload,
    run_payload,
    verdict_text,
)

REPO = "takolab/local-agent-concierge"


def _green_client():
    return FakeGitHubClient(
        pull_requests=[pull_request_payload(number=27, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
    )


def _run(argv, *, client=None, reviewer=None, reader=None, writer=None):
    stream = io.StringIO()
    code = cli.main(
        list(argv),
        client=client if client is not None else _green_client(),
        reviewer=reviewer if reviewer is not None else FakeReviewer(),
        reader=reader if reader is not None else FakeCommentReader(),
        writer=writer if writer is not None else FakeCommentWriter(),
        # Injected so no test resolves the account from a live GitHub.
        expected_author=AUTOMATION_LOGIN,
        stream=stream,
    )
    return code, stream.getvalue()


BASE_ARGV = ("review", "--pr", "27", "--repo", REPO)


# --- the verification command is unchanged ---------------------------------


def test_the_verification_command_still_works_without_a_subcommand():
    stream = io.StringIO()

    code = cli.main(["--pr", "27", "--dry-run"], client=_green_client(), stream=stream)

    assert code == 0
    assert "CI verdict:           READY" in stream.getvalue()
    assert "GitHub write performed: No" in stream.getvalue()


def test_the_verification_help_points_at_the_review_command():
    assert "review-loop review" in cli.build_parser().format_help()


# --- the review command ----------------------------------------------------


def test_a_valid_review_is_recorded_and_exits_zero():
    writer = FakeCommentWriter()

    code, output = _run(BASE_ARGV, writer=writer)

    assert code == 0
    assert "Outcome:              REVIEW_VALID" in output
    assert f"GitHub write performed: Yes (comment {writer.comment_id})" in output
    assert len(writer.posted) == 1


def test_the_report_shows_the_exact_reviewed_head_sha():
    _, output = _run(BASE_ARGV)

    assert f"Reviewed head SHA:    {FULL_SHA} (matches target)" in output


def test_a_dry_run_writes_nothing_and_prints_the_comment_it_would_record():
    writer = FakeCommentWriter()

    code, output = _run(BASE_ARGV + ("--dry-run",), writer=writer)

    assert code == 0
    assert writer.posted == []
    assert "GitHub write performed: No" in output
    assert "--- comment that would be recorded ---" in output
    assert "## Independent AI Review" in output


def test_a_dry_run_still_runs_the_reviewer_and_the_revalidation():
    reviewer = FakeReviewer()

    _, output = _run(BASE_ARGV + ("--dry-run",), reviewer=reviewer)

    assert reviewer.invoked
    assert "Revalidation:         READY" in output


@pytest.mark.parametrize(
    "reviewer, outcome",
    [
        (FakeReviewer(ReviewerRun(failure="the reviewer exited 3")), ReviewOutcome.REVIEWER_FAILED),
        (FakeReviewer(ReviewerRun(stdout="ship it")), ReviewOutcome.REVIEW_MALFORMED),
        (
            FakeReviewer(ReviewerRun(stdout=verdict_text(head_sha=OTHER_SHA))),
            ReviewOutcome.REVIEW_SHA_MISMATCH,
        ),
    ],
    ids=["failed", "malformed", "sha-mismatch"],
)
def test_each_reviewer_failure_has_its_own_exit_code_and_writes_nothing(reviewer, outcome):
    writer = FakeCommentWriter()

    code, output = _run(BASE_ARGV, reviewer=reviewer, writer=writer)

    assert code == REVIEW_EXIT_CODES[outcome]
    assert f"Outcome:              {outcome.value}" in output
    assert writer.posted == []
    assert "GitHub write performed: No" in output


def test_a_target_that_is_not_ready_reports_the_verification_exit_code():
    client = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=27, head_sha=FULL_SHA)],
        runs=[run_payload(run_id=1, status="in_progress", conclusion=None)],
        changed_files=("README.md",),
    )
    reviewer = FakeReviewer()
    writer = FakeCommentWriter()

    code, output = _run(BASE_ARGV, client=client, reviewer=reviewer, writer=writer)

    assert code == EXIT_CODES[Verdict.PENDING]
    assert "Outcome:              TARGET_NOT_READY" in output
    assert not reviewer.invoked
    assert writer.posted == []


def test_a_write_failure_has_its_own_exit_code():
    writer = FakeCommentWriter(error=GitHubApiError("HTTP 403"))

    code, output = _run(BASE_ARGV, writer=writer)

    assert code == REVIEW_EXIT_CODES[ReviewOutcome.GITHUB_WRITE_FAILED]
    assert "GitHub write performed: No" in output


def test_an_already_recorded_review_exits_zero_without_writing():
    from review_loop import comment_format

    identity = comment_format.RecordIdentity(
        repo=REPO, number=27, head_sha=FULL_SHA, base_sha=BASE_TIP, round=1
    )
    reader = FakeCommentReader([comment_format.marker(identity)])
    writer = FakeCommentWriter()

    code, output = _run(BASE_ARGV, reader=reader, writer=writer)

    assert code == 0
    assert "Outcome:              COMMENT_ALREADY_EXISTS" in output
    assert writer.posted == []


# --- usage -----------------------------------------------------------------


def test_a_reviewer_command_is_required_when_none_is_injected():
    stream = io.StringIO()

    code = cli.main(
        ["review", "--pr", "27", "--repo", REPO],
        client=_green_client(),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        expected_author=AUTOMATION_LOGIN,
        stream=stream,
    )

    assert code == EXIT_USAGE
    assert "--reviewer-command is required" in stream.getvalue()


def test_an_unparseable_reviewer_command_is_a_usage_error():
    stream = io.StringIO()

    code = cli.main(
        ["review", "--pr", "27", "--repo", REPO, "--reviewer-command", "'unterminated"],
        client=_green_client(),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        expected_author=AUTOMATION_LOGIN,
        stream=stream,
    )

    assert code == EXIT_USAGE


def test_a_non_positive_pr_number_is_a_usage_error():
    code, _ = _run(("review", "--pr", "0", "--repo", REPO))

    assert code == EXIT_USAGE


def test_the_help_documents_every_review_outcome_exit_code():
    text = build_review_parser().format_help()

    for outcome, code in REVIEW_EXIT_CODES.items():
        assert outcome.value in text
        assert str(code) in text


def test_the_help_states_the_single_permitted_write_and_its_scope():
    """The claim is about this command's own writes, not the reviewer's."""
    text = " ".join(build_review_parser().format_help().split())

    assert "only write this command itself performs" in text
    assert "creating one pull request comment" in text
    assert "nothing here can stop a reviewer command that decides to write" in text


# --- json ------------------------------------------------------------------


def test_json_output_carries_the_outcome_and_the_bound_sha():
    code, output = _run(BASE_ARGV + ("--json",))
    payload = json.loads(output)

    assert code == 0
    assert payload["outcome"] == "REVIEW_VALID"
    assert payload["verdict"]["reviewed_head_sha"] == FULL_SHA
    assert payload["target"]["head_sha"] == FULL_SHA
    assert payload["github_write_performed"] is True
    assert payload["ci_verification"] == "READY"
    assert payload["ci_reverification"] == "READY"


def test_json_output_of_a_dry_run_records_that_nothing_was_written():
    _, output = _run(BASE_ARGV + ("--dry-run", "--json"))
    payload = json.loads(output)

    assert payload["dry_run"] is True
    assert payload["github_write_performed"] is False
    assert payload["comment_id"] is None


def test_json_output_never_abbreviates_a_sha_it_reports_as_an_identity():
    _, output = _run(BASE_ARGV + ("--json",))
    payload = json.loads(output)

    assert len(payload["target"]["head_sha"]) == 40
    assert len(payload["target"]["ci_merge_base_sha"]) == 40
    assert len(payload["verdict"]["reviewed_head_sha"]) == 40


def test_an_unready_pull_request_still_reports_which_commit_it_looked_at():
    client = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=27, head_sha=FULL_SHA)],
        runs=[run_payload(run_id=1, status="in_progress", conclusion=None)],
        changed_files=("README.md",),
    )

    _, output = _run(BASE_ARGV, client=client)

    assert f"Head SHA:             {FULL_SHA}" in output
    assert "(not resolved)" not in output


def test_the_account_the_runner_would_comment_as_is_resolved_when_not_injected(monkeypatch):
    from review_loop import review_cli

    calls = []
    monkeypatch.setattr(
        review_cli, "resolve_comment_author", lambda: calls.append(1) or AUTOMATION_LOGIN
    )
    stream = io.StringIO()

    code = cli.main(
        list(BASE_ARGV),
        client=_green_client(),
        reviewer=FakeReviewer(),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        stream=stream,
    )

    assert calls == [1]
    assert code == 0


def test_a_dry_run_also_resolves_it(monkeypatch):
    """A dry run that could not tell its own record from anyone's is useless."""
    from review_loop import review_cli

    calls = []
    monkeypatch.setattr(
        review_cli, "resolve_comment_author", lambda: calls.append(1) or AUTOMATION_LOGIN
    )

    cli.main(
        list(BASE_ARGV) + ["--dry-run"],
        client=_green_client(),
        reviewer=FakeReviewer(),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        stream=io.StringIO(),
    )

    assert calls == [1]


def test_an_unresolvable_account_stops_the_review_rather_than_guessing(monkeypatch):
    from review_loop import review_cli

    def boom():
        raise GitHubApiError("gh api user failed (exit 1): HTTP 401")

    monkeypatch.setattr(review_cli, "resolve_comment_author", boom)
    reviewer = FakeReviewer()
    writer = FakeCommentWriter()
    stream = io.StringIO()

    code = cli.main(
        list(BASE_ARGV),
        client=_green_client(),
        reviewer=reviewer,
        reader=FakeCommentReader(),
        writer=writer,
        stream=stream,
    )

    assert code == REVIEW_EXIT_CODES[ReviewOutcome.API_ERROR]
    assert not reviewer.invoked
    assert writer.posted == []
    assert "GitHub write performed: No" in stream.getvalue()
