"""``review-loop merge-brief`` -- turn the evidence chain into one human artifact.

Its three inputs are the three earlier turns' own ``--json`` documents. None
of them is trusted as authority: the chain they describe is rebuilt through
the invariants that produced it, and every fact they claim about the pull
request is re-established against GitHub before anything is classified.

The command is named for what it produces, not for what it decides, because
it decides nothing. It answers seven questions in one place -- what exact
state this is, what became of every original finding, what a fresh review
found, whether authoritative CI is current, whether the merge context is
current, whether anything is escalated, and what the next workflow action
mechanically is -- and then stops. Merging is not among the things it can do.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence, TextIO

from .decision import (
    DECISION_EXIT_CODES,
    DecisionInputError,
    DecisionOutcome,
    NextAction,
)
from .decision_input import load_request
from .decision_runner import DecisionResult, run_decision
from .github_client import GitHubApiError, GitHubClient
from .github_comments import (
    IssueCommentReader,
    IssueCommentWriter,
    resolve_comment_author,
)
from .model import short_sha

_EPILOG = """\
exit codes:
  0   BRIEF_RECORDED          a current merge decision brief was recorded (or,
                              with --dry-run, would be)
  0   COMMENT_ALREADY_EXISTS  this exact brief is already recorded; nothing
                              was written
  90  DECISION_INPUT_INVALID  the inputs are not a validated review, the
                              PUSH_READY push of its fix, and the validated
                              re-review of that push
  91  EVIDENCE_NOT_CURRENT    the pull request has moved out from under the
                              re-review; a diagnostic is printed and nothing
                              is recorded
  92  GITHUB_WRITE_FAILED     the brief was current but the comment failed
  93  API_ERROR               GitHub could not be queried
  2   usage error

Exit code 0 means *a current merge decision brief exists for this exact pull
request state*. It does not mean the pull request may merge, and it does not
mean the classification was READY_FOR_HUMAN_MERGE_DECISION: FIX_REQUIRED and
HUMAN_ESCALATION are equally successful runs that recorded one artifact. What
the evidence concluded is in the brief, never in the exit status.

This command performs no merge, no push, no commit, and starts no Coding
Agent and no reviewer. Its only write is one pull request comment.
"""


def build_decision_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-loop merge-brief",
        description=(
            "Rebuild the review -> fix -> push -> CI -> re-review evidence chain "
            "from the three documents that recorded it, re-verify that it still "
            "describes the pull request's current head, base, merge context and "
            "authoritative CI, derive the next workflow action from the resulting "
            "facts, and record it as one '## Merge Decision Brief' comment for a "
            "human to decide on."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--review-json",
        required=True,
        help="the 'review-loop review --json' document that raised the findings",
    )
    parser.add_argument(
        "--push-json",
        required=True,
        help="the 'review-loop push --json' document reporting PUSH_READY for the fix",
    )
    parser.add_argument(
        "--rereview-json",
        required=True,
        help=(
            "the 'review-loop re-review --json' document recording the fresh "
            "re-review of that pushed fix"
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="target repository as owner/name (default: read from the documents)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "re-verify everything and print the brief that would be recorded, "
            "writing nothing to GitHub"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def _read_document(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _id_list(values) -> str:
    return ", ".join(values) or "(none)"


def render_text(result: DecisionResult, stream: TextIO) -> None:
    request = result.request
    target = result.current_target or (
        request.recorded_target if request is not None else None
    )

    if target is None:
        print("PR:                   (not resolved)", file=stream)
    else:
        print(
            f"PR:                   #{target.number} (base {target.base_ref})",
            file=stream,
        )
        print(
            f"Head SHA:             {target.head_sha}  [{short_sha(target.head_sha)}]",
            file=stream,
        )
        print(f"CI merge base:        {target.ci_merge_base_sha}", file=stream)

    print(
        "CI verification:      "
        + (
            result.evaluation.verdict.value
            if result.evaluation is not None
            else "(not performed)"
        ),
        file=stream,
    )

    if request is not None:
        print(
            f"Original review:      round {request.original_round} of "
            f"{request.original_head_sha}",
            file=stream,
        )
        print(f"Review identity:      {request.source_review_sha256}", file=stream)
        print(
            f"Re-review:            round {request.rereview.round} of "
            f"{request.rereview.reviewed_head_sha} "
            f"(recommendation={request.rereview.recommendation.value})",
            file=stream,
        )
        print(
            "Re-review record:     "
            + (
                f"comment {result.rereview_record_id}"
                if result.rereview_record_id is not None
                else "(not confirmed on the pull request)"
            ),
            file=stream,
        )

    facts = result.facts
    if facts is None:
        print("Evidence:             (not gathered)", file=stream)
    else:
        # Printed as two labelled groups, never as one score: an original
        # finding's resolution and a fresh finding's severity are different
        # facts about different rounds.
        print(
            "Original findings:    RESOLVED "
            f"{_id_list(facts.resolved_original_finding_ids)}",
            file=stream,
        )
        print(
            "                      UNRESOLVED "
            f"{_id_list(facts.unresolved_original_finding_ids)}",
            file=stream,
        )
        print(
            "                      ESCALATE "
            f"{_id_list(facts.escalated_original_finding_ids)}",
            file=stream,
        )
        print(
            "Fresh findings:       Blocking "
            f"{_id_list(facts.fresh_blocking_finding_ids)}",
            file=stream,
        )
        print(
            "                      Major "
            f"{_id_list(facts.fresh_major_finding_ids)}",
            file=stream,
        )
        print(
            "                      Minor "
            f"{_id_list(facts.fresh_minor_finding_ids)}",
            file=stream,
        )
        print(
            f"Evidence current:     {'Yes' if facts.evidence_current else 'No'}",
            file=stream,
        )

    print(
        "Next action:          "
        + (
            result.next_action.value
            if result.next_action is not None
            else "(not classified)"
        ),
        file=stream,
    )
    if result.classification is not None:
        print("Because:", file=stream)
        for reason in result.classification.reasons:
            print(f"  - {reason}", file=stream)

    print(f"Outcome:              {result.outcome.value}", file=stream)
    print("Reason:", file=stream)
    for reason in result.reasons or ("(none recorded)",):
        print(f"  - {reason}", file=stream)

    if result.brief and (result.dry_run or result.outcome is DecisionOutcome.EVIDENCE_NOT_CURRENT):
        label = (
            "brief that would be recorded"
            if result.next_action is not NextAction.EVIDENCE_NOT_CURRENT
            else "diagnostic (not recorded)"
        )
        print("", file=stream)
        print(f"--- {label} ---", file=stream)
        print(result.brief.rstrip("\n"), file=stream)
        print("--- end ---", file=stream)
        print("", file=stream)

    print(
        "Human decision required: merge / do not merge / request another fix / "
        "escalate",
        file=stream,
    )
    written = (
        f"Yes (comment {result.comment_id})" if result.github_write_performed else "No"
    )
    print(f"GitHub write performed: {written}", file=stream)


def render_json(result: DecisionResult, stream: TextIO) -> None:
    request = result.request
    facts = result.facts
    target = result.current_target or (
        request.recorded_target if request is not None else None
    )
    stale = result.next_action is NextAction.EVIDENCE_NOT_CURRENT
    payload = {
        "outcome": result.outcome.value,
        "exit_code": result.exit_code,
        "dry_run": result.dry_run,
        "reasons": list(result.reasons),
        # The classification and the facts it was derived from, side by side,
        # so a consumer applying a different policy can read the facts without
        # inheriting this runner's routing.
        "next_action": None if result.next_action is None else result.next_action.value,
        "classification_reasons": []
        if result.classification is None
        else list(result.classification.reasons),
        "evidence_current": None if facts is None else facts.evidence_current,
        "evidence_not_current_reasons": []
        if facts is None
        else list(facts.evidence_not_current_reasons),
        "repository": None if target is None else target.repo,
        "pr_number": None if target is None else target.number,
        "head_sha": None if target is None else target.head_sha,
        "base_ref": None if target is None else target.base_ref,
        "ci_merge_base_sha": None if target is None else target.ci_merge_base_sha,
        "ci_verification": None
        if result.evaluation is None
        else result.evaluation.verdict.value,
        "base_tip_at_verification": None
        if result.evaluation is None
        else result.evaluation.base_tip_at_verification,
        "ci_evidence": []
        if target is None
        else [
            {"workflow_path": path, "run_id": run_id, "conclusion": conclusion}
            for path, run_id, conclusion in target.ci_evidence
        ],
        "source_review_sha256": None
        if request is None
        else request.source_review_sha256,
        "original_review": None
        if request is None
        else {
            "round": request.original_round,
            "reviewed_head_sha": request.original_head_sha,
            "recommendation": request.original_recommendation.value,
            "finding_ids": list(request.chain.original_finding_ids),
        },
        "rereview": None
        if request is None
        else {
            "round": request.rereview.round,
            "reviewed_head_sha": request.rereview.reviewed_head_sha,
            "recommendation": request.rereview.recommendation.value,
            "escalation_reason": request.rereview.escalation_reason,
            "outcome": request.rereview_outcome,
            # What the document said, and what this turn actually found. The
            # second is the one a brief rests on; the first is kept so a
            # disagreement is visible rather than silently resolved.
            "comment_id": request.rereview_comment_id,
            "confirmed_comment_id": result.rereview_record_id,
        },
        # Six explicit lists rather than any boolean. Each names the
        # collection it reads and what it reports, so no field can be read as
        # saying something about the other collection.
        "resolved_original_finding_ids": []
        if facts is None
        else list(facts.resolved_original_finding_ids),
        "unresolved_original_finding_ids": []
        if facts is None
        else list(facts.unresolved_original_finding_ids),
        "escalated_original_finding_ids": []
        if facts is None
        else list(facts.escalated_original_finding_ids),
        "fresh_blocking_finding_ids": []
        if facts is None
        else list(facts.fresh_blocking_finding_ids),
        "fresh_major_finding_ids": []
        if facts is None
        else list(facts.fresh_major_finding_ids),
        "fresh_minor_finding_ids": []
        if facts is None
        else list(facts.fresh_minor_finding_ids),
        "human_decision_required": True,
        "human_decision_options": [
            "merge",
            "do not merge",
            "request another fix",
            "escalate",
        ],
        "github_write_performed": result.github_write_performed,
        "comment_id": result.comment_id,
        "existing_comment_id": result.existing_comment_id,
        # Two keys, and never both. Stale evidence produces a diagnostic
        # explaining why no decision can be made, and it is deliberately not
        # served under the key a consumer reads a decision from: a brief that
        # is not current must not be reachable as one that is.
        "decision_brief": None if stale else result.brief,
        "diagnostic": result.brief if stale else None,
    }
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _failure(outcome: DecisionOutcome, reason: str, out: TextIO, as_json: bool) -> int:
    result = DecisionResult(outcome=outcome, reasons=(reason,))
    if as_json:
        render_json(result, out)
    else:
        render_text(result, out)
    return DECISION_EXIT_CODES[outcome]


def decision_main(
    argv: Sequence[str],
    *,
    client=None,
    reader=None,
    writer=None,
    expected_author: str | None = None,
    stream: TextIO | None = None,
) -> int:
    parser = build_decision_parser()
    args = parser.parse_args(list(argv))
    out = stream if stream is not None else sys.stdout

    try:
        documents = [
            _read_document(path)
            for path in (args.review_json, args.push_json, args.rereview_json)
        ]
    except OSError as exc:
        return _failure(
            DecisionOutcome.DECISION_INPUT_INVALID, str(exc), out, args.json
        )

    try:
        request = load_request(*documents, expected_repo=args.repo)
    except DecisionInputError as exc:
        return _failure(
            DecisionOutcome.DECISION_INPUT_INVALID, str(exc), out, args.json
        )

    repo = request.recorded_target.repo
    try:
        client = client if client is not None else GitHubClient(repo)
        reader = reader if reader is not None else IssueCommentReader(repo)
        # A dry run never constructs a writer, so there is nothing that could
        # write even if a later change got the branching wrong.
        if not args.dry_run and writer is None:
            writer = IssueCommentWriter(repo)
        if expected_author is None:
            expected_author = resolve_comment_author()
    except (GitHubApiError, ValueError) as exc:
        return _failure(DecisionOutcome.API_ERROR, str(exc), out, args.json)

    result = run_decision(
        client=client,
        reader=reader,
        writer=None if args.dry_run else writer,
        request=request,
        expected_author=expected_author,
        dry_run=args.dry_run,
    )

    if args.json:
        render_json(result, out)
    else:
        render_text(result, out)
    return result.exit_code
