"""``review-loop re-review`` -- run one fresh Independent Re-Review turn.

Its two inputs are the two earlier turns' own ``--json`` documents: the
review that raised the findings, and the ``PUSH_READY`` push that put their
fix on the branch. Neither is trusted as authority -- both are re-read
through the invariants that produced them, and every fact they claim about
the pull request is re-established against GitHub before a reviewer starts.

Like ``review``, the only write this command performs is creating one pull
request comment, and only on the path that ends in a validated re-review.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence, TextIO

from .github_client import GitHubApiError, GitHubClient
from .github_comments import (
    IssueCommentReader,
    IssueCommentWriter,
    resolve_comment_author,
)
from .model import EXIT_CODES, EXIT_USAGE, Verdict, short_sha
from .rereview import (
    RE_REVIEW_EXIT_CODES,
    RE_REVIEW_ROUND,
    ReReviewOutcome,
    Resolution,
)
from .rereview_input import ReReviewInputError, load_request
from .rereview_runner import ReReviewResult, run_rereview
from .reviewer_process import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_TIMEOUT_SECONDS,
    ReviewerCommandError,
    SubprocessReviewer,
    build_env,
    split_command,
)
from .reviewer_workspace import (
    DEFAULT_REMOTE,
    ExistingWorkspace,
    PreparedWorkspace,
    WorkspaceBoundReviewer,
)
from .verdict import Severity

_EPILOG = f"""\
exit codes:
  0   RE_REVIEW_VALID         a validated re-review was recorded (or, with
                              --dry-run, would be)
  0   COMMENT_ALREADY_EXISTS  this exact re-review is already recorded;
                              nothing was written
  80  RE_REVIEW_INPUT_INVALID the inputs are not a validated review plus the
                              PUSH_READY push of its fix
  81  TARGET_NOT_AT_FIX       the pull request is no longer at the pushed fix,
                              or its CI no longer describes the current merge
                              context; no reviewer was started
  82  REVIEWER_WORKSPACE_INVALID  the reviewer's working directory is not a
                              clean checkout of the pushed fix
  83  REVIEWER_FAILED         the reviewer process failed, timed out, or
                              produced nothing
  84  RE_REVIEW_MALFORMED     the reviewer's output is not a valid re-review
  85  RE_REVIEW_SHA_MISMATCH  the re-review describes another commit
  86  TARGET_STALE            the pull request moved while the reviewer ran
  87  GITHUB_WRITE_FAILED     the re-review was valid but the comment failed
  88  API_ERROR               GitHub could not be queried
  2   usage error

If verification does not report READY for the pushed fix, no reviewer is
started and the exit code is that verification verdict's own
({EXIT_CODES[Verdict.PENDING]} PENDING, {EXIT_CODES[Verdict.FAILED]} FAILED,
{EXIT_CODES[Verdict.AMBIGUOUS]} AMBIGUOUS, {EXIT_CODES[Verdict.STALE_TARGET]}
STALE_TARGET).

Exit code 0 means *a validated re-review exists for this exact pushed fix*.
It does not mean the findings were resolved and it does not mean the pull
request may merge: what the re-review established is in the recorded comment,
as two separate facts -- which original findings are resolved, and what a
fresh review of the current state found.

The reviewer command is run with no shell, in a working directory that is the
pushed fix commit, exactly as `review-loop review` runs one. It is a fresh
process with no access to the Coding Agent's context: the only thing it is
told about the fix is which commit it is and which findings preceded it.
Its environment is an allowlist ({', '.join(DEFAULT_ENV_ALLOWLIST)}) plus
anything named with --reviewer-env.
"""


def build_rereview_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-loop re-review",
        description=(
            "Re-verify that a pushed fix is still the pull request's head with "
            "authoritative CI green against the current merge context, run a fresh "
            "independent read-only reviewer against that exact commit, validate the "
            "resolution of every original finding and any fresh findings it reports, "
            "re-verify the target, and record the result as one "
            "'## Independent AI Re-Review' comment."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--review-json",
        required=True,
        help=(
            "the 'review-loop review --json' document whose validated findings are "
            "being re-evaluated"
        ),
    )
    parser.add_argument(
        "--push-json",
        required=True,
        help=(
            "the 'review-loop push --json' document reporting PUSH_READY for the fix "
            "of that review"
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "assert that the documents describe this repository, as "
            "owner/name. It is a check, not a selector: the repository this "
            "command reads from GitHub and records against always comes from "
            "the validated documents."
        ),
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
        help=(
            "seconds before the reviewer is abandoned "
            f"(default: {DEFAULT_TIMEOUT_SECONDS:g})"
        ),
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
        help=(
            "run the reviewer in this directory instead of a worktree the runner "
            "prepares. It must be a clean checkout of the exact pushed fix commit, "
            "and is verified before the reviewer starts"
        ),
    )
    parser.add_argument(
        "--git-remote",
        default=DEFAULT_REMOTE,
        help=(
            "remote to fetch the pull request's head ref from when preparing the "
            f"reviewer's worktree (default: {DEFAULT_REMOTE})"
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "repository the reviewer's worktree is prepared from "
            "(default: the current directory)"
        ),
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
            "on a malformed re-review, print the reviewer's raw output to stderr for "
            "debugging. Off by default: raw output is untrusted and may contain "
            "anything the reviewer read."
        ),
    )
    return parser


def _read_document(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _workspace(args, number: int):
    """Choose how the reviewer's working directory is bound to the pushed fix."""
    if args.reviewer_cwd is not None:
        return ExistingWorkspace(args.reviewer_cwd)
    return PreparedWorkspace(
        args.repo_root or os.getcwd(), number, remote=args.git_remote
    )


def _resolution_line(result: ReReviewResult, resolution: Resolution) -> str:
    rereview = result.rereview
    if rereview is None:
        return "(none)"
    ids = [r.finding_id for r in rereview.resolutions_with(resolution)]
    return ", ".join(ids) or "(none)"


def render_text(
    result: ReReviewResult,
    stream: TextIO,
    *,
    reviewer_label: str,
    workspace_label: str | None = None,
) -> None:
    request = result.request
    target = result.target

    if request is not None:
        print(
            f"PR:                   #{request.target.number} "
            f"(base {request.target.base_ref})",
            file=stream,
        )
        print(
            f"Pushed fix SHA:       {request.pushed_fix_sha}  "
            f"[{short_sha(request.pushed_fix_sha)}]",
            file=stream,
        )
        print(
            f"Original review:      round {request.original_round} of "
            f"{request.original_head_sha}",
            file=stream,
        )
        print(
            "Original findings:    "
            + (", ".join(request.original_finding_ids) or "(none)"),
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
    if target is not None:
        print(f"CI merge base:        {target.ci_merge_base_sha}", file=stream)
    print(f"Reviewer:             {reviewer_label}", file=stream)
    if workspace_label is not None:
        print(f"Reviewer workspace:   {workspace_label}", file=stream)
    print(
        f"Reviewer invoked:     {'Yes' if result.reviewer_invoked else 'No'}",
        file=stream,
    )

    rereview = result.rereview
    if rereview is None:
        print("Re-review:            (no valid re-review)", file=stream)
    else:
        print(
            f"Reviewed head SHA:    {rereview.reviewed_head_sha} (matches pushed fix)",
            file=stream,
        )
        print(
            f"Re-review:            round={rereview.round} "
            f"recommendation={rereview.recommendation.value}",
            file=stream,
        )
        # The two facts are printed as two blocks, never as one score. A
        # reader who wants "is this done?" reads both.
        print(
            f"Original resolutions: RESOLVED "
            f"{_resolution_line(result, Resolution.RESOLVED)}",
            file=stream,
        )
        print(
            f"                      UNRESOLVED "
            f"{_resolution_line(result, Resolution.UNRESOLVED)}",
            file=stream,
        )
        print(
            f"                      ESCALATE "
            f"{_resolution_line(result, Resolution.ESCALATE)}",
            file=stream,
        )
        print(
            f"Fresh findings:       {len(rereview.fresh_findings)} "
            f"(Blocking {rereview.count(Severity.BLOCKING)} / "
            f"Major {rereview.count(Severity.MAJOR)} / "
            f"Minor {rereview.count(Severity.MINOR)})",
            file=stream,
        )

    post = result.post_evaluation
    print(
        "Revalidation:         "
        + (
            "not reached"
            if post is None
            else post.verdict.value
            + (
                ", target unchanged"
                if result.outcome
                not in {ReReviewOutcome.TARGET_STALE, ReReviewOutcome.API_ERROR}
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


def render_json(result: ReReviewResult, stream: TextIO) -> None:
    request = result.request
    target = result.target
    rereview = result.rereview
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
        "round": RE_REVIEW_ROUND,
        "request": None
        if request is None
        else {
            "repo": request.target.repo,
            "number": request.target.number,
            "pushed_fix_sha": request.pushed_fix_sha,
            "original_head_sha": request.original_head_sha,
            "original_round": request.original_round,
            "original_finding_ids": list(request.original_finding_ids),
        },
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
        # Two keys, never one. 'resolutions' is history about the round-1
        # findings; 'fresh_findings' is this turn's own review of the current
        # state. Nothing here combines them into a status.
        "rereview": None
        if rereview is None
        else {
            "round": rereview.round,
            "reviewed_head_sha": rereview.reviewed_head_sha,
            "recommendation": rereview.recommendation.value,
            "escalation_reason": rereview.escalation_reason,
            "resolutions": [
                {
                    "finding_id": r.finding_id,
                    "resolution": r.resolution.value,
                    "evidence": r.evidence,
                    "reason": r.reason,
                }
                for r in rereview.resolutions
            ],
            "unresolved_finding_ids": list(rereview.unresolved_finding_ids),
            "fresh_findings": [
                {
                    "finding_id": f.finding_id,
                    "severity": f.severity.value,
                    "location": f.location,
                    "problem": f.problem,
                    "evidence": f.evidence,
                    "required_outcome": f.required_outcome,
                    "scope_boundary": f.scope_boundary,
                }
                for f in rereview.fresh_findings
            ],
            # Every severity key here is fresh-only, and says so in its name.
            # A key called '*_findings_remain' would read across both
            # collections while counting one of them, which is the combined
            # status this contract refuses -- and would report `false` for a
            # pull request with an UNRESOLVED original Major finding.
            # 'unresolved_finding_ids' above is the other half; a consumer
            # that wants "is anything outstanding?" reads both.
            "fresh_blocking": rereview.count(Severity.BLOCKING),
            "fresh_major": rereview.count(Severity.MAJOR),
            "fresh_minor": rereview.count(Severity.MINOR),
            "fresh_blocking_findings_present": rereview.fresh_blocking_findings_present,
            "fresh_major_findings_present": rereview.fresh_major_findings_present,
        },
        "comment_body": result.comment_body,
    }
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _failure(
    outcome: ReReviewOutcome, reason: str, out: TextIO, as_json: bool
) -> int:
    result = ReReviewResult(outcome=outcome, reasons=(reason,))
    if as_json:
        render_json(result, out)
    else:
        render_text(result, out, reviewer_label="(not started)")
    return RE_REVIEW_EXIT_CODES[outcome]


def rereview_main(
    argv: Sequence[str],
    *,
    client=None,
    reader=None,
    writer=None,
    reviewer=None,
    expected_author: str | None = None,
    stream: TextIO | None = None,
) -> int:
    parser = build_rereview_parser()
    args = parser.parse_args(list(argv))
    out = stream if stream is not None else sys.stdout

    try:
        review_document = _read_document(args.review_json)
        push_document = _read_document(args.push_json)
    except OSError as exc:
        return _failure(
            ReReviewOutcome.RE_REVIEW_INPUT_INVALID, str(exc), out, args.json
        )

    try:
        request = load_request(
            review_document, push_document, expected_repo=args.repo
        )
    except ReReviewInputError as exc:
        return _failure(
            ReReviewOutcome.RE_REVIEW_INPUT_INVALID, str(exc), out, args.json
        )

    repo = request.target.repo
    number = request.target.number

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
        reviewer = WorkspaceBoundReviewer(
            SubprocessReviewer(
                argv_command,
                timeout=args.reviewer_timeout,
                env=build_env(dict(os.environ), tuple(args.reviewer_env)),
            ),
            _workspace(args, number),
        )

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
        return _failure(ReReviewOutcome.API_ERROR, str(exc), out, args.json)

    result = run_rereview(
        client=client,
        reader=reader,
        writer=None if args.dry_run else writer,
        reviewer=reviewer,
        request=request,
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
        render_text(
            result,
            out,
            reviewer_label=reviewer_label,
            workspace_label=(
                reviewer.describe_workspace()
                if hasattr(reviewer, "describe_workspace")
                else None
            ),
        )
    return result.exit_code
