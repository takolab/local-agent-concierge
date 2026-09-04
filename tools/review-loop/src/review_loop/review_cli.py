"""``review-loop review`` -- run one Independent Review turn.

Kept beside the verification front end rather than inside it, so that
``review-loop --pr N --dry-run`` keeps meaning exactly what PR #28 shipped: a
read-only question with a read-only answer. The review subcommand is the only
place a write can happen, and it happens only on the path that ends in a
validated verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence, TextIO

from .github_client import GitHubApiError, GitHubClient, detect_repository
from .github_comments import (
    IssueCommentReader,
    IssueCommentWriter,
    resolve_comment_author,
)
from .model import EXIT_CODES, EXIT_USAGE, Verdict, short_sha
from .review_runner import ReviewResult, run_review
from .reviewer_process import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_TIMEOUT_SECONDS,
    ReviewerCommandError,
    SubprocessReviewer,
    build_env,
    split_command,
)
from .verdict import REVIEW_EXIT_CODES, ReviewOutcome, Severity

_EPILOG = f"""\
exit codes:
  0   REVIEW_VALID            a validated review was recorded (or, with
                              --dry-run, would be)
  0   COMMENT_ALREADY_EXISTS  this exact review is already recorded; nothing
                              was written
  20  API_ERROR               GitHub could not be queried
  30  REVIEWER_FAILED         the reviewer process failed, timed out, or
                              produced nothing
  31  REVIEW_MALFORMED        the reviewer's output is not a valid verdict
  32  REVIEW_SHA_MISMATCH     the verdict describes another commit
  33  TARGET_STALE            the pull request moved while the reviewer ran
  34  GITHUB_WRITE_FAILED     the verdict was valid but the comment failed
  2   usage error

If verification does not report READY, no reviewer is started and the exit
code is that verification verdict's own ({EXIT_CODES[Verdict.PENDING]} PENDING,
{EXIT_CODES[Verdict.FAILED]} FAILED, {EXIT_CODES[Verdict.AMBIGUOUS]} AMBIGUOUS,
{EXIT_CODES[Verdict.STALE_TARGET]} STALE_TARGET).

The reviewer command is run with no shell: it is tokenised into an argument
vector, receives the review prompt on stdin, and answers on stdout. Its
environment is an allowlist ({', '.join(DEFAULT_ENV_ALLOWLIST)}) plus anything
named with --reviewer-env.

The only write this command itself performs is creating one pull request
comment. The reviewer you configure is a trusted, read-only wrapper of your
choosing: it runs as an ordinary child process with your filesystem access,
so nothing here can stop a reviewer command that decides to write.
"""


def build_review_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-loop review",
        description=(
            "Verify a pull request's exact head, run an independent read-only "
            "reviewer against it, validate the verdict it returns, re-verify the "
            "target, and record the result as one '## Independent AI Review' "
            "comment."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument(
        "--repo",
        default=None,
        help="target repository as owner/name (default: detected from the git remote)",
    )
    parser.add_argument(
        "--reviewer-command",
        default=None,
        help=(
            "the reviewer to run, as a command line tokenised with shell quoting "
            "rules but never executed by a shell"
        ),
    )
    parser.add_argument(
        "--reviewer-timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds before the reviewer is abandoned (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--reviewer-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "additionally pass this environment variable to the reviewer; repeatable. "
            "Without it the reviewer sees only the default allowlist."
        ),
    )
    parser.add_argument(
        "--reviewer-cwd",
        default=None,
        help="working directory for the reviewer (default: the current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run everything including the reviewer and the post-review "
            "re-verification, print the comment that would be recorded, and write "
            "nothing to GitHub"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    parser.add_argument(
        "--print-raw-output",
        action="store_true",
        help=(
            "on a malformed verdict, print the reviewer's raw output to stderr for "
            "debugging. Off by default: raw output is untrusted and may contain "
            "anything the reviewer read."
        ),
    )
    return parser


def _verdict_summary(result: ReviewResult) -> str:
    verdict = result.verdict
    if verdict is None:
        return "(no valid verdict)"
    return (
        f"round={verdict.round} recommendation={verdict.recommendation.value} "
        f"open={len(verdict.open_findings)} "
        f"(Blocking {verdict.count(Severity.BLOCKING)} / "
        f"Major {verdict.count(Severity.MAJOR)} / "
        f"Minor {verdict.count(Severity.MINOR)})"
    )


def render_text(result: ReviewResult, stream: TextIO, *, reviewer_label: str) -> None:
    target = result.target
    # A run that stops before the target is captured -- an unready pull
    # request, most often -- still knows which commit it was looking at, and
    # that is the first thing a reader wants to see.
    verified = result.pre_evaluation.target if result.pre_evaluation else None

    if target is not None:
        print(f"PR:                   #{target.number} (base {target.base_ref})", file=stream)
        print(
            f"Head SHA:             {target.head_sha}  [{short_sha(target.head_sha)}]",
            file=stream,
        )
        print(f"CI merge base:        {target.ci_merge_base_sha}", file=stream)
    elif verified is not None:
        print(
            f"PR:                   #{verified.number} "
            f"({verified.head_ref} -> {verified.base_ref})",
            file=stream,
        )
        print(
            f"Head SHA:             {verified.head_sha}  [{short_sha(verified.head_sha)}]",
            file=stream,
        )
    else:
        print("PR:                   (not resolved)", file=stream)

    pre = result.pre_evaluation
    print(
        "CI verification:      "
        + (pre.verdict.value if pre is not None else "(not performed)"),
        file=stream,
    )
    print(f"Reviewer:             {reviewer_label}", file=stream)
    print(
        f"Reviewer invoked:     {'Yes' if result.reviewer_invoked else 'No'}", file=stream
    )
    print(f"Verdict:              {_verdict_summary(result)}", file=stream)
    if result.verdict is not None:
        print(
            f"Reviewed head SHA:    {result.verdict.reviewed_head_sha} (matches target)",
            file=stream,
        )

    post = result.post_evaluation
    print(
        "Revalidation:         "
        + (
            "not reached"
            if post is None
            else f"{post.verdict.value}"
            + (
                ", target unchanged"
                if result.outcome
                not in {ReviewOutcome.TARGET_STALE, ReviewOutcome.API_ERROR}
                else ", target changed"
            )
        ),
        file=stream,
    )
    print(f"Outcome:              {result.outcome.value}", file=stream)
    print("Reason:", file=stream)
    for reason in result.reasons or ("(none recorded)",):
        print(f"  - {reason}", file=stream)

    if result.dry_run and result.comment_body:
        print("", file=stream)
        print("--- comment that would be recorded ---", file=stream)
        print(result.comment_body.rstrip("\n"), file=stream)
        print("--- end of comment ---", file=stream)
        print("", file=stream)

    written = (
        f"Yes (comment {result.comment_id})" if result.github_write_performed else "No"
    )
    print(f"GitHub write performed: {written}", file=stream)


def render_json(result: ReviewResult, stream: TextIO) -> None:
    verdict = result.verdict
    target = result.target
    payload = {
        "outcome": result.outcome.value,
        "exit_code": result.exit_code,
        "dry_run": result.dry_run,
        "reasons": list(result.reasons),
        "reviewer_invoked": result.reviewer_invoked,
        "github_write_performed": result.github_write_performed,
        "comment_id": result.comment_id,
        "existing_comment_id": result.existing_comment_id,
        "ci_verification": None
        if result.pre_evaluation is None
        else result.pre_evaluation.verdict.value,
        "ci_reverification": None
        if result.post_evaluation is None
        else result.post_evaluation.verdict.value,
        "target": None
        if target is None
        else {
            "repo": target.repo,
            "number": target.number,
            "head_sha": target.head_sha,
            "base_ref": target.base_ref,
            "ci_merge_base_sha": target.ci_merge_base_sha,
            "ci_evidence": [
                {"workflow_path": path, "run_id": run_id, "conclusion": conclusion}
                for path, run_id, conclusion in target.ci_evidence
            ],
        },
        "verdict": None
        if verdict is None
        else {
            "round": verdict.round,
            "reviewed_head_sha": verdict.reviewed_head_sha,
            "recommendation": verdict.recommendation.value,
            "blocking": verdict.count(Severity.BLOCKING),
            "major": verdict.count(Severity.MAJOR),
            "minor": verdict.count(Severity.MINOR),
            "escalation_reason": verdict.escalation_reason,
            "open_findings": [
                {
                    "finding_id": f.finding_id,
                    "severity": f.severity.value,
                    "location": f.location,
                    "problem": f.problem,
                    "evidence": f.evidence,
                    "required_outcome": f.required_outcome,
                    "scope_boundary": f.scope_boundary,
                }
                for f in verdict.open_findings
            ],
        },
        "comment_body": result.comment_body,
    }
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _failure(reason: str, out: TextIO, as_json: bool) -> int:
    result = ReviewResult(outcome=ReviewOutcome.API_ERROR, reasons=(reason,))
    render_json(result, out) if as_json else render_text(
        result, out, reviewer_label="(not started)"
    )
    return REVIEW_EXIT_CODES[ReviewOutcome.API_ERROR]


def review_main(
    argv: Sequence[str],
    *,
    client=None,
    reader=None,
    writer=None,
    reviewer=None,
    expected_author: str | None = None,
    stream: TextIO | None = None,
) -> int:
    parser = build_review_parser()
    args = parser.parse_args(list(argv))
    out = stream if stream is not None else sys.stdout

    if args.pr <= 0:
        print(f"error: --pr must be a positive integer, got {args.pr}", file=out)
        return EXIT_USAGE

    repo = args.repo
    if repo is None and (client is None or reader is None):
        try:
            repo = detect_repository()
        except (GitHubApiError, ValueError) as exc:
            return _failure(str(exc), out, args.json)
    repo = repo or "(unknown)/(unknown)"

    reviewer_label = "(injected)"
    if reviewer is None:
        if not args.reviewer_command:
            print(
                "error: --reviewer-command is required; there is no default reviewer",
                file=out,
            )
            return EXIT_USAGE
        try:
            argv_command = split_command(args.reviewer_command)
        except ReviewerCommandError as exc:
            print(f"error: {exc}", file=out)
            return EXIT_USAGE
        reviewer_label = " ".join(argv_command)
        reviewer = SubprocessReviewer(
            argv_command,
            timeout=args.reviewer_timeout,
            env=build_env(dict(os.environ), tuple(args.reviewer_env)),
            cwd=args.reviewer_cwd,
        )

    try:
        client = client if client is not None else GitHubClient(repo)
        reader = reader if reader is not None else IssueCommentReader(repo)
        # A dry run never constructs a writer, so there is nothing that could
        # write even if a later change got the branching wrong.
        if not args.dry_run and writer is None:
            writer = IssueCommentWriter(repo)
        # Resolved even for a dry run: without it the duplicate check cannot
        # tell this automation's own record from a marker anyone copied, and a
        # dry run that reported the wrong answer would be worse than useless.
        if expected_author is None:
            expected_author = resolve_comment_author()
    except (GitHubApiError, ValueError) as exc:
        return _failure(str(exc), out, args.json)

    result = run_review(
        client=client,
        reader=reader,
        writer=None if args.dry_run else writer,
        reviewer=reviewer,
        repo=repo,
        number=args.pr,
        expected_author=expected_author,
        dry_run=args.dry_run,
    )

    if args.print_raw_output and result.reviewer_stdout:
        print(
            "--- reviewer raw stdout (untrusted, never recorded) ---",
            file=sys.stderr,
        )
        print(result.reviewer_stdout, file=sys.stderr)

    if args.json:
        render_json(result, out)
    else:
        render_text(result, out, reviewer_label=reviewer_label)
    return result.exit_code
