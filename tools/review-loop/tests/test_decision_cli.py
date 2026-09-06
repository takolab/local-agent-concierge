"""``review-loop merge-brief`` end to end, against offline fakes."""

from __future__ import annotations

import io
import json

import pytest

from review_loop.cli import main
from review_loop.decision import DECISION_EXIT_CODES, DecisionOutcome
from review_loop.model import EXIT_USAGE

from fakes import (
    ADVANCED_BASE_TIP,
    AUTOMATION_LOGIN,
    BASE_TIP,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    pull_request_payload,
    run_payload,
    BASELINE_PATH,
)
from decision_fakes import (
    chain,
    edited,
    fresh_major,
    fresh_minor,
    green_client,
    invoke,
    one_escalated,
    one_unresolved,
    write,
)
from rereview_fakes import LATER_SHA, PR, PUSHED_SHA


# -- the human-readable output ------------------------------------------------


def test_a_clean_chain_reports_ready_and_records_one_brief(tmp_path):
    code, out, writer = invoke(tmp_path)

    assert code == 0
    assert "Next action:          READY_FOR_HUMAN_MERGE_DECISION" in out
    assert "Outcome:              BRIEF_RECORDED" in out
    assert "GitHub write performed: Yes" in out
    assert len(writer.posted) == 1


def test_the_output_reports_the_two_collections_separately(tmp_path):
    code, out, _ = invoke(
        tmp_path, documents=chain(tmp_path, reviewer_output=fresh_major())
    )

    assert code == 0
    assert "Original findings:    RESOLVED F1, F2" in out
    assert "UNRESOLVED (none)" in out
    assert "Major R2.F1" in out
    assert "Next action:          FIX_REQUIRED" in out


def test_the_output_always_states_that_a_human_decides(tmp_path):
    for output in (None, fresh_minor(), fresh_major(), one_escalated()):
        _, out, _ = invoke(
            tmp_path, documents=chain(tmp_path, reviewer_output=output)
        )
        assert (
            "Human decision required: merge / do not merge / request another fix / "
            "escalate" in out
        )


def test_the_reasons_for_the_classification_are_printed(tmp_path):
    _, out, _ = invoke(
        tmp_path, documents=chain(tmp_path, reviewer_output=one_unresolved())
    )
    assert "Because:" in out
    assert "F1" in out
    assert "no fix round is started by this command" in out


# -- the JSON contract --------------------------------------------------------


def test_the_json_result_reports_the_facts_as_six_explicit_lists(tmp_path):
    code, out, _ = invoke(
        tmp_path,
        documents=chain(tmp_path, reviewer_output=fresh_major()),
        extra=("--json",),
    )
    payload = json.loads(out)

    assert code == 0
    assert payload["next_action"] == "FIX_REQUIRED"
    assert payload["resolved_original_finding_ids"] == ["F1", "F2"]
    assert payload["unresolved_original_finding_ids"] == []
    assert payload["escalated_original_finding_ids"] == []
    assert payload["fresh_blocking_finding_ids"] == []
    assert payload["fresh_major_finding_ids"] == ["R2.F1"]
    assert payload["fresh_minor_finding_ids"] == []
    # No field sums the two collections, so none can be read across them.
    assert not [key for key in payload if key.endswith("_findings_remain")]


def test_the_json_result_identifies_the_exact_state_and_the_chain(tmp_path):
    documents = chain(tmp_path)
    _, out, _ = invoke(tmp_path, documents=documents, extra=("--json",))
    payload = json.loads(out)

    assert payload["repository"] == "takolab/local-agent-concierge"
    assert payload["pr_number"] == PR
    assert payload["head_sha"] == PUSHED_SHA
    assert payload["base_ref"] == "master"
    assert payload["ci_merge_base_sha"] == BASE_TIP
    assert payload["base_tip_at_verification"] == BASE_TIP
    assert payload["ci_verification"] == "READY"
    assert len(payload["source_review_sha256"]) == 64
    assert payload["original_review"]["round"] == 1
    assert payload["original_review"]["finding_ids"] == ["F1", "F2"]
    assert payload["rereview"]["round"] == 2
    assert payload["rereview"]["reviewed_head_sha"] == PUSHED_SHA
    assert payload["evidence_current"] is True
    assert payload["human_decision_required"] is True
    assert payload["decision_brief"].startswith("## Merge Decision Brief")
    assert payload["diagnostic"] is None


def test_the_json_result_carries_the_recorded_comment_id(tmp_path):
    _, out, writer = invoke(tmp_path, extra=("--json",))
    payload = json.loads(out)
    assert payload["github_write_performed"] is True
    assert payload["comment_id"] == writer.comment_id


def test_the_json_result_reports_an_escalation_without_taking_it(tmp_path):
    _, out, _ = invoke(
        tmp_path,
        documents=chain(tmp_path, reviewer_output=one_escalated()),
        extra=("--json",),
    )
    payload = json.loads(out)
    assert payload["next_action"] == "HUMAN_ESCALATION"
    assert payload["escalated_original_finding_ids"] == ["F1"]
    assert payload["human_decision_options"] == [
        "merge",
        "do not merge",
        "request another fix",
        "escalate",
    ]


# -- dry run ------------------------------------------------------------------


def test_a_dry_run_prints_the_brief_and_writes_nothing(tmp_path):
    code, out, writer = invoke(tmp_path, extra=("--dry-run",))

    assert code == 0
    assert writer.posted == []
    assert "GitHub write performed: No" in out
    assert "--- brief that would be recorded ---" in out
    assert "## Merge Decision Brief" in out


def test_a_dry_run_never_constructs_a_writer(tmp_path):
    """Structural, not conventional: nothing is threaded through that could write."""
    review, push, rereview = chain(tmp_path)
    code = main(
        [
            "merge-brief",
            "--review-json",
            write(tmp_path, "r.json", review),
            "--push-json",
            write(tmp_path, "p.json", push),
            "--rereview-json",
            write(tmp_path, "rr.json", rereview),
            "--dry-run",
        ],
        client=green_client(),
        reader=FakeCommentReader(),
        writer=None,
        expected_author=AUTOMATION_LOGIN,
        stream=io.StringIO(),
    )
    assert code == 0


# -- stale evidence -----------------------------------------------------------


def test_a_moved_head_exits_evidence_not_current_and_writes_nothing(tmp_path):
    moved = FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=LATER_SHA)],
        runs=[
            run_payload(
                run_id=9, path=BASELINE_PATH, head_sha=LATER_SHA, pr_number=PR,
                merge_base=BASE_TIP,
            )
        ],
    )
    code, out, writer = invoke(tmp_path, client=moved)

    assert code == DECISION_EXIT_CODES[DecisionOutcome.EVIDENCE_NOT_CURRENT]
    assert writer.posted == []
    assert "Next action:          EVIDENCE_NOT_CURRENT" in out
    assert "--- diagnostic (not recorded) ---" in out
    assert "GitHub write performed: No" in out


def test_the_diagnostic_is_printed_without_a_dry_run_flag(tmp_path):
    """A refusal has to explain itself even on the path that would have written."""
    code, out, _ = invoke(tmp_path, client=green_client(base_tip=ADVANCED_BASE_TIP))
    assert code == DECISION_EXIT_CODES[DecisionOutcome.EVIDENCE_NOT_CURRENT]
    assert "Why this is not decision-ready:" in out
    assert "No brief was recorded." in out


def test_stale_evidence_json_says_why_and_classifies_nothing_else(tmp_path):
    code, out, _ = invoke(
        tmp_path, client=green_client(base_tip=ADVANCED_BASE_TIP), extra=("--json",)
    )
    payload = json.loads(out)

    assert code == DECISION_EXIT_CODES[DecisionOutcome.EVIDENCE_NOT_CURRENT]
    assert payload["next_action"] == "EVIDENCE_NOT_CURRENT"
    assert payload["evidence_current"] is False
    assert payload["evidence_not_current_reasons"]
    assert payload["github_write_performed"] is False
    assert payload["comment_id"] is None
    # No decision brief is served for a state nobody briefed; the explanation
    # is under its own key so it cannot be read as one.
    assert payload["decision_brief"] is None
    assert "Why this is not decision-ready:" in payload["diagnostic"]


# -- input refusals -----------------------------------------------------------


def test_the_re_review_document_is_required(tmp_path):
    """All three, always. There is no chain without the last link."""
    review, push, _ = chain(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "merge-brief",
                "--review-json",
                write(tmp_path, "r.json", review),
                "--push-json",
                write(tmp_path, "p.json", push),
            ],
            stream=io.StringIO(),
        )
    assert exit_info.value.code == EXIT_USAGE


def test_an_unreadable_document_is_reported_without_touching_github(tmp_path):
    review, push, _ = chain(tmp_path)

    out = io.StringIO()
    client = green_client()
    code = main(
        [
            "merge-brief",
            "--review-json",
            write(tmp_path, "r.json", review),
            "--push-json",
            write(tmp_path, "p.json", push),
            "--rereview-json",
            str(tmp_path / "absent.json"),
        ],
        client=client,
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        expected_author=AUTOMATION_LOGIN,
        stream=out,
    )
    assert code == DECISION_EXIT_CODES[DecisionOutcome.DECISION_INPUT_INVALID]
    assert client.calls == []


def test_a_mispaired_chain_is_refused_before_any_github_request(tmp_path):
    review, push, rereview = chain(tmp_path)
    swapped = edited(
        rereview, lambda p: p["request"].update({"original_finding_ids": ["F1", "F9"]})
    )
    client = green_client()
    code, out, writer = invoke(
        tmp_path, documents=(review, push, swapped), client=client
    )

    assert code == DECISION_EXIT_CODES[DecisionOutcome.DECISION_INPUT_INVALID]
    assert client.calls == []
    assert writer.posted == []
    assert "Outcome:              DECISION_INPUT_INVALID" in out


def test_a_repo_flag_that_disagrees_with_the_documents_is_refused(tmp_path):
    code, out, writer = invoke(tmp_path, extra=("--repo", "someone/else"))
    assert code == DECISION_EXIT_CODES[DecisionOutcome.DECISION_INPUT_INVALID]
    assert writer.posted == []


# -- idempotency --------------------------------------------------------------


def test_a_retry_over_the_same_state_writes_nothing_a_second_time(tmp_path):
    documents = chain(tmp_path)
    first_code, _, first_writer = invoke(tmp_path, documents=documents)
    assert first_code == 0

    reader = FakeCommentReader([first_writer.posted[0][1]])
    code, out, writer = invoke(tmp_path, documents=documents, reader=reader)

    assert code == 0
    assert writer.posted == []
    assert "Outcome:              COMMENT_ALREADY_EXISTS" in out
    assert "GitHub write performed: No" in out
