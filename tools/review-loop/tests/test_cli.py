"""End-to-end CLI behaviour against an offline fake, and the read-only guarantee."""

import ast
import io
import json
from pathlib import Path

import pytest

from review_loop import cli
from review_loop.github_client import ALLOWED_HTTP_METHOD
from review_loop.model import EXIT_CODES, EXIT_USAGE, Verdict

from fakes import (
    BASELINE_PATH,
    FILTERED_PATH,
    FULL_SHA,
    OTHER_SHA,
    FailingGitHubClient,
    FakeGitHubClient,
    pull_request_payload,
    run_payload,
)

_CLIENT_SOURCE = Path(
    Path(cli.__file__).parent / "github_client.py"
).read_text()


def _run(client, argv=("--pr", "27", "--dry-run")):
    stream = io.StringIO()
    code = cli.main(list(argv), client=client, stream=stream)
    return code, stream.getvalue()


def _green_client(**kwargs):
    return FakeGitHubClient(
        pull_requests=[pull_request_payload(number=27, head_sha=FULL_SHA)],
        runs=[
            run_payload(run_id=1, path=BASELINE_PATH, conclusion="success"),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, conclusion="success"
            ),
        ],
        **kwargs,
    )


def test_a_green_exact_head_exits_zero_and_reports_ready():
    code, output = _run(_green_client())

    assert code == 0
    assert "CI verdict:           READY" in output
    assert FULL_SHA in output
    assert "GitHub write performed: No" in output


def test_the_report_shows_the_full_head_sha_alongside_the_short_form():
    _, output = _run(_green_client())

    assert FULL_SHA in output
    assert f"[{FULL_SHA[:7]}]" in output


def test_a_head_that_moves_during_verification_exits_stale_target():
    client = FakeGitHubClient(
        pull_requests=[
            pull_request_payload(head_sha=FULL_SHA),
            pull_request_payload(head_sha=OTHER_SHA),
        ],
        runs=[run_payload(run_id=1, path=BASELINE_PATH, conclusion="success")],
    )

    code, output = _run(client)

    assert code == EXIT_CODES[Verdict.STALE_TARGET]
    assert "Head stable:          No" in output


@pytest.mark.parametrize(
    "runs, expected_verdict",
    [
        ([run_payload(run_id=1, path=BASELINE_PATH, conclusion="failure")], Verdict.FAILED),
        (
            [
                run_payload(
                    run_id=1, path=BASELINE_PATH, status="in_progress", conclusion=None
                )
            ],
            Verdict.PENDING,
        ),
        ([], Verdict.AMBIGUOUS),
    ],
    ids=["failed", "pending", "no-runs"],
)
def test_non_ready_states_exit_with_their_own_codes(runs, expected_verdict):
    client = FakeGitHubClient(
        pull_requests=[pull_request_payload(head_sha=FULL_SHA)], runs=runs
    )

    code, output = _run(client)

    assert code == EXIT_CODES[expected_verdict]
    assert code != 0
    assert f"CI verdict:           {expected_verdict.value}" in output


def test_a_github_failure_exits_api_error_rather_than_reporting_no_ci():
    code, output = _run(FailingGitHubClient())

    assert code == EXIT_CODES[Verdict.API_ERROR]
    assert "API_ERROR" in output


def test_a_non_positive_pr_number_is_a_usage_error():
    code, _ = _run(_green_client(), argv=("--pr", "0"))

    assert code == EXIT_USAGE
    assert EXIT_USAGE not in EXIT_CODES.values()


def test_json_output_carries_the_full_sha_and_the_verdict():
    code, output = _run(_green_client(), argv=("--pr", "27", "--dry-run", "--json"))
    payload = json.loads(output)

    assert code == 0
    assert payload["verdict"] == "READY"
    assert payload["pull_request"]["head_sha"] == FULL_SHA
    assert payload["github_write_performed"] is False
    assert {w["workflow_path"] for w in payload["observed_workflows"]} == {
        BASELINE_PATH,
        FILTERED_PATH,
    }


def test_json_output_never_abbreviates_a_sha_it_reports_as_an_identity():
    _, output = _run(_green_client(), argv=("--pr", "27", "--json"))
    payload = json.loads(output)

    assert len(payload["pull_request"]["head_sha"]) == 40
    assert len(payload["head_sha_at_verification"]) == 40
    for workflow in payload["observed_workflows"]:
        assert len(workflow["head_sha"]) == 40


def test_a_dry_run_calls_only_read_endpoints():
    client = _green_client()

    _run(client, argv=("--pr", "27", "--dry-run"))

    assert {name for name, _ in client.calls} <= {
        "get_pull_request",
        "list_workflow_runs_for_sha",
        "list_workflow_files",
    }


def test_the_run_without_dry_run_makes_exactly_the_same_calls():
    """``--dry-run`` reserves a name; today every mode is already read-only."""
    with_flag = _green_client()
    without_flag = _green_client()

    _run(with_flag, argv=("--pr", "27", "--dry-run"))
    _run(without_flag, argv=("--pr", "27"))

    assert with_flag.calls == without_flag.calls


def test_the_client_issues_no_http_method_other_than_get():
    tree = ast.parse(_CLIENT_SOURCE)
    string_literals = {
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    }

    assert ALLOWED_HTTP_METHOD == "GET"
    assert not {"POST", "PATCH", "PUT", "DELETE"} & string_literals


@pytest.mark.parametrize(
    "write_fragment",
    ["/comments", "/reviews", "/labels", "/merge", "/dispatches", "/rerun", "/issues"],
)
def test_the_client_references_no_write_endpoint(write_fragment):
    assert write_fragment not in _CLIENT_SOURCE


def test_help_documents_the_exit_codes():
    text = cli.build_parser().format_help()

    for verdict in Verdict:
        assert verdict.value in text
        assert str(EXIT_CODES[verdict]) in text
