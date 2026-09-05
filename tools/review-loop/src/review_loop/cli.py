"""Command line front end.

Three commands, one entry point:

* ``review-loop --pr <number> --dry-run`` -- verification only, read-only,
  unchanged from the shape PR #28 shipped.
* ``review-loop review --pr <number> --reviewer-command ...`` -- one
  Independent Review turn, which is the only path that can write to GitHub.
* ``review-loop fix --review-json <file> --agent-command ...`` -- one bounded
  Coding Agent turn routed from that review's validated findings. It makes no
  GitHub request at all, and writes only inside a worktree it prepares.

Subcommands are dispatched by name rather than by an argparse subparser so
that the bare ``--pr`` form keeps working exactly as before, including its
help text and its exit codes.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence, TextIO

from .github_client import GitHubApiError, GitHubClient, detect_repository
from .evaluate import AUTHORITATIVE_EVENT
from .model import EXIT_CODES, EXIT_USAGE, CiEvaluation, TriggerExpectation, Verdict, short_sha
from .runner import verify_pull_request

_EPILOG = """\
exit codes:
  0   READY         the exact head has complete, passing CI; a review may start
  10  PENDING       CI for the exact head is still running
  11  FAILED        CI for the exact head completed with a failing conclusion
  12  AMBIGUOUS     CI state could not be determined safely; do not start a review
  13  STALE_TARGET  the head, or the base its CI was merged onto, moved
  20  API_ERROR     GitHub could not be queried
  2   usage error

Only exit code 0 means a review may be started. This command never writes to
GitHub: it issues read-only GET requests through the authenticated `gh` CLI.

To run the review itself, see `review-loop review --help`. To route its
findings to a bounded Coding Agent turn, see `review-loop fix --help`.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-loop",
        description=(
            "Resolve a pull request's exact 40-character head SHA and verify "
            "whether that exact commit's GitHub Actions CI allows an "
            "Independent Review to start."
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
        "--dry-run",
        action="store_true",
        help=(
            "read-only verification and report only. This is currently the only "
            "supported mode; the flag exists so the read-only path keeps a stable "
            "name once the runner grows write-capable modes."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def _expectation_labels(evaluation: CiEvaluation) -> dict[str, str]:
    return {d.path: d.expectation.value for d in evaluation.definitions}


def render_text(evaluation: CiEvaluation, stream: TextIO) -> None:
    target = evaluation.target
    expectations = _expectation_labels(evaluation)

    if target is None:
        print("PR:                   (not resolved)", file=stream)
    else:
        print(f"PR:                   #{target.number} ({target.head_ref} -> {target.base_ref})", file=stream)
        print(f"Head SHA:             {target.head_sha}  [{short_sha(target.head_sha)}]", file=stream)

    stable = (
        target is not None and evaluation.head_sha_at_verification == target.head_sha
    )
    print(f"Head stable:          {'Yes' if stable else 'No'}", file=stream)

    # A pull_request run tests the head merged onto the base, so the base is
    # part of what was verified even though it is not what gets reviewed.
    merge_base = evaluation.ci_merge_base_sha
    base_tip = evaluation.base_tip_at_verification
    print(
        f"CI merge base:        {merge_base or '(not established)'}",
        file=stream,
    )
    base_stable = merge_base is not None and base_tip is not None and merge_base == base_tip
    print(
        f"Merge base current:   {'Yes' if base_stable else 'No'}"
        + (f" (base is now {short_sha(base_tip)})" if base_tip and not base_stable else ""),
        file=stream,
    )

    baseline = sorted(
        path
        for path, expectation in expectations.items()
        if expectation == TriggerExpectation.REQUIRED.value
    )
    print(f"Baseline workflows:   {', '.join(baseline) if baseline else '(none identified)'}", file=stream)

    print("Observed workflows:", file=stream)
    if not evaluation.outcomes:
        print("  (no workflow runs for this exact commit)", file=stream)
    for outcome in evaluation.outcomes:
        run = outcome.run
        conclusion = run.conclusion or "-"
        superseded = (
            f" supersedes={list(outcome.superseded_run_ids)}"
            if outcome.superseded_run_ids
            else ""
        )
        evidence = "" if run.event == AUTHORITATIVE_EVENT else " (not evidence: tested the head tree)"
        print(
            f"  {outcome.workflow_path} [{expectations.get(outcome.workflow_path, 'UNCONFIGURED')}]"
            f" name={outcome.workflow_name!r} run={run.run_id} attempt={run.run_attempt}"
            f" event={run.event} status={run.status} conclusion={conclusion}"
            f"{superseded}{evidence}",
            file=stream,
        )

    print(f"CI verdict:           {evaluation.verdict.value}", file=stream)
    print("Reason:", file=stream)
    for reason in evaluation.reasons or ("(none recorded)",):
        print(f"  - {reason}", file=stream)
    print("GitHub write performed: No", file=stream)


def render_json(evaluation: CiEvaluation, stream: TextIO) -> None:
    target = evaluation.target
    expectations = _expectation_labels(evaluation)
    payload = {
        "verdict": evaluation.verdict.value,
        "exit_code": evaluation.exit_code,
        "reasons": list(evaluation.reasons),
        "github_write_performed": False,
        "pull_request": None
        if target is None
        else {
            "number": target.number,
            "head_sha": target.head_sha,
            "base_ref": target.base_ref,
            "head_ref": target.head_ref,
            "state": target.state,
            "changed_files": list(target.changed_files),
        },
        "head_sha_at_verification": evaluation.head_sha_at_verification,
        "ci_merge_base_sha": evaluation.ci_merge_base_sha,
        "base_tip_at_verification": evaluation.base_tip_at_verification,
        "workflow_definitions": [
            {"path": d.path, "name": d.name, "expectation": d.expectation.value}
            for d in evaluation.definitions
        ],
        "observed_workflows": [
            {
                "workflow_path": o.workflow_path,
                "workflow_name": o.workflow_name,
                "expectation": expectations.get(o.workflow_path, "UNCONFIGURED"),
                "run_id": o.run.run_id,
                "workflow_id": o.run.workflow_id,
                "run_attempt": o.run.run_attempt,
                "event": o.run.event,
                "status": o.run.status,
                "conclusion": o.run.conclusion,
                "head_sha": o.run.head_sha,
                "superseded_run_ids": list(o.superseded_run_ids),
                "is_evidence": o.run.event == AUTHORITATIVE_EVENT,
                "pull_request_numbers": list(o.run.pull_request_numbers),
                "merge_base_shas": list(o.run.merge_base_shas),
            }
            for o in evaluation.outcomes
        ],
    }
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


#: Subcommand names, dispatched before argparse sees the arguments.
REVIEW_COMMAND = "review"
FIX_COMMAND = "fix"


def main(
    argv: Sequence[str] | None = None,
    *,
    client: GitHubClient | None = None,
    stream: TextIO | None = None,
    reader=None,
    writer=None,
    reviewer=None,
    agent=None,
    workspace=None,
    expected_author: str | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == FIX_COMMAND:
        from .fix_cli import fix_main

        # No client, reader or writer is threaded through: the fix turn holds
        # no GitHub credential and issues no GitHub request.
        return fix_main(arguments[1:], agent=agent, workspace=workspace, stream=stream)
    if arguments and arguments[0] == REVIEW_COMMAND:
        from .review_cli import review_main

        return review_main(
            arguments[1:],
            client=client,
            reader=reader,
            writer=writer,
            reviewer=reviewer,
            expected_author=expected_author,
            stream=stream,
        )
    return verify_main(arguments, client=client, stream=stream)


def verify_main(
    argv: Sequence[str] | None = None,
    *,
    client: GitHubClient | None = None,
    stream: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = stream if stream is not None else sys.stdout

    if args.pr <= 0:
        print(f"error: --pr must be a positive integer, got {args.pr}", file=out)
        return EXIT_USAGE

    if client is None:
        try:
            client = GitHubClient(args.repo or detect_repository())
        except (GitHubApiError, ValueError) as exc:
            evaluation = CiEvaluation(verdict=Verdict.API_ERROR, reasons=(str(exc),))
            render_json(evaluation, out) if args.json else render_text(evaluation, out)
            return EXIT_CODES[Verdict.API_ERROR]

    evaluation = verify_pull_request(client, args.pr)
    if args.json:
        render_json(evaluation, out)
    else:
        render_text(evaluation, out)
    return evaluation.exit_code
