"""``review-loop fix`` -- route validated findings to one bounded fix turn.

The third command, and the first that starts a *writable* agent. Two things
about its shape are deliberate.

**It takes the review's own JSON, not a pull request number.** Running the
review again to recover its findings would pay for a second reviewer and
could produce a different verdict from the one a human read. The handoff is
:mod:`review_loop.routing`, which re-validates every field rather than
trusting the file.

**It talks to GitHub not at all.** No client, no reader, no writer, no token,
no `gh`. The one fact a fix turn needs from outside -- that the commit it is
fixing is still this pull request's head -- comes from git resolving
``refs/pull/N/head``, which is the same fact obtained without a credential.
So this command cannot comment, cannot push and cannot merge, and that is a
property of what it imports rather than a promise about how it behaves.

The end of a successful run is a patch and a structured result, not a commit.
Whether the fix is right, and whether it is used, remain the human's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence, TextIO

from .agent_process import (
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    DEFAULT_ENV_ALLOWLIST,
    AgentCommandError,
    SubprocessAgent,
    build_env,
    split_command,
)
from .fix_request import ScopeError
from .fix_response import (
    DEFAULT_MAX_ROUTED_FINDINGS,
    FIX_EXIT_CODES,
    FixRunOutcome,
)
from .fix_runner import FixResult, run_fix
from .model import EXIT_USAGE, short_sha
from .routing import RoutingInputError, load_handoff
from .reviewer_workspace import DEFAULT_REMOTE, ExistingWorkspace, PreparedWorkspace

AGENT_ROLE = "coding agent"

_EPILOG = f"""\
exit codes:
  0   FIX_APPLIED             every routed finding came back fixed, and the
                              working tree agrees
  0   NO_ACTIONABLE_FINDINGS  the review asks for nothing; no agent ran
  0   ROUTING_PREPARED        --dry-run: the request was built and shown
  40  REVIEW_REQUIRES_HUMAN   a Blocking finding, an escalated review, too
                              many findings, or a finding whose scope cannot
                              be bounded; no agent ran
  41  ROUTING_INPUT_INVALID   the routing input is not a validated review
  42  CODING_AGENT_WORKSPACE_INVALID  the agent's working directory is not a
                              clean checkout of the target; no agent ran
  43  CODING_AGENT_FAILED     the agent process failed, timed out, or
                              produced nothing
  44  FIX_RESPONSE_MALFORMED  the output is not a valid fix response
  45  FIX_TARGET_MISMATCH     a response describes another commit
  46  FIX_FINDING_MISMATCH    the responses do not match the routed findings
  47  FIX_SCOPE_VIOLATION     the working tree disagrees with the response,
                              or holds a change the scope did not permit
  48  FIX_NOT_APPLIED         the agent could not fix a routed finding
  49  FIX_ESCALATED           the agent escalated a routed finding
  50  PATCH_WRITE_FAILED      the fix was valid but --write-patch failed
  51  PATCH_TOO_LARGE         the fix was valid but its diff was too large to
                              capture, so no patch survives the run
  2   usage error

Exit code 0 means there is nothing left for this step to do: either a
validated fix exists, or the review gave it nothing to act on.

This command makes no GitHub request at all -- no read and no write. It needs
no token. That the commit being fixed is still this pull request's head is
established by git: the worktree is prepared from refs/pull/N/head, and a ref
resolving anywhere else stops the run.

The outer limit on what the coding agent may edit is what this pull request
itself changed, also taken from git -- the diff against the point this branch
diverged from its base. A finding's Location selects within that limit and
cannot reach beyond it; only --allow-path can. Establishing it needs the base
branch locally, or fetchable from --git-remote.

The coding agent is run with no shell: it is tokenised into an argument
vector, receives the task contract on stdin, and answers on stdout. Its
environment is an allowlist ({', '.join(DEFAULT_ENV_ALLOWLIST)}) plus anything
named with --agent-env. It runs in a dedicated detached worktree at the exact
reviewed commit, which is removed afterwards on every path -- so use
--write-patch to keep the diff.

Nothing here commits, pushes, or touches the pull request. The agent is told
not to, and the working tree is inspected afterwards to check that it did
not. It is not a sandbox: the agent runs as an ordinary child process with
your permissions and could reach the rest of your filesystem.
"""


def build_fix_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-loop fix",
        description=(
            "Route a validated Independent Review's open findings to one bounded "
            "Coding Agent turn in a worktree at the reviewed commit, validate the "
            "Structured Fix Response it returns, and check the working tree "
            "against it. Nothing is committed, pushed, or written to GitHub."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--review-json",
        required=True,
        help=(
            "the 'review-loop review --json' document to route, or '-' to read it "
            "from stdin"
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "expected repository as owner/name; the routing input must describe it"
        ),
    )
    parser.add_argument(
        "--agent-command",
        default=None,
        help=(
            "the coding agent to run, as a command line tokenised with shell "
            "quoting rules but never executed by a shell"
        ),
    )
    parser.add_argument(
        "--agent-timeout",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT_SECONDS,
        help=(
            "seconds before the coding agent is abandoned "
            f"(default: {DEFAULT_AGENT_TIMEOUT_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--agent-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "additionally pass this environment variable to the coding agent; "
            "repeatable. Without it the agent sees only the default allowlist, "
            "which holds no credential."
        ),
    )
    parser.add_argument(
        "--agent-cwd",
        default=None,
        help=(
            "run the coding agent in this directory instead of a worktree the "
            "runner prepares. It must be a clean checkout of the exact reviewed "
            "commit, and is verified before the agent starts. Its contents will "
            "be modified."
        ),
    )
    parser.add_argument(
        "--git-remote",
        default=DEFAULT_REMOTE,
        help=(
            "remote to fetch the pull request's head ref from when preparing the "
            "agent's worktree, and to resolve the base branch from when this "
            f"pull request's change set cannot be read locally (default: {DEFAULT_REMOTE})"
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "local clone the agent's worktree is prepared from. Cross-repository "
            "routing needs a clone of the repository under review here "
            "(default: the current directory)"
        ),
    )
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "widen the allowed scope by this repository-relative path or "
            "directory; repeatable. This is the only input that may reach "
            "outside what the pull request itself changed, so use it "
            "deliberately, after reading the finding."
        ),
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=DEFAULT_MAX_ROUTED_FINDINGS,
        help=(
            "most open findings to carry in one bounded turn "
            f"(default: {DEFAULT_MAX_ROUTED_FINDINGS}); above it the review goes "
            "to a human instead"
        ),
    )
    parser.add_argument(
        "--write-patch",
        default=None,
        metavar="FILE",
        help=(
            "write the fix as a unified diff here. The worktree is removed when "
            "the run ends, so without this the change is not kept anywhere."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate the routing input and report what would be routed. No "
            "worktree is created and no coding agent is invoked."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    parser.add_argument(
        "--print-raw-output",
        action="store_true",
        help=(
            "on a malformed response, print the agent's raw output to stderr for "
            "debugging. Off by default: raw output is untrusted and may contain "
            "anything the agent read."
        ),
    )
    return parser


def _workspace(args):
    """Choose how the agent's working directory is bound to the target.

    Preparing one is the default, for the reason PR #32 gave and one more:
    this workspace gets *written to*, so reusing a directory the operator
    cares about would put a coding agent's edits in a tree they did not
    dedicate to it.
    """
    if args.agent_cwd is not None:
        return ExistingWorkspace(args.agent_cwd, role=AGENT_ROLE)
    return PreparedWorkspace(
        args.repo_root or os.getcwd(),
        args.pr,
        remote=args.git_remote,
        role=AGENT_ROLE,
    )


def _read_document(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def render_text(result: FixResult, stream: TextIO, *, agent_label: str,
                workspace_label: str | None = None) -> None:
    target = result.target
    if target is not None:
        print(f"PR:                   #{target.number} (base {target.base_ref})", file=stream)
        print(
            f"Reviewed head SHA:    {target.head_sha}  [{short_sha(target.head_sha)}]",
            file=stream,
        )
        print(f"Repository:           {target.repo}", file=stream)
    else:
        print("PR:                   (not resolved)", file=stream)

    request = result.request
    print(f"Coding agent:         {agent_label}", file=stream)
    if workspace_label is not None:
        print(f"Agent workspace:      {workspace_label}", file=stream)
    print(
        f"Agent invoked:        {'Yes' if result.agent_invoked else 'No'}", file=stream
    )
    if request is not None:
        print(
            "Routed findings:      "
            + ", ".join(
                f"{r.finding.finding_id} ({r.finding.severity.value})"
                for r in request.findings
            ),
            file=stream,
        )
        print(
            "Change-set boundary:  "
            + (
                ", ".join(entry.display() for entry in request.change_set_boundary)
                or "(not established)"
            ),
            file=stream,
        )
        print(
            "Allowed scope:        "
            + ", ".join(entry.display() for entry in request.allowed_paths),
            file=stream,
        )
        outside = sorted(
            {
                path
                for routed in request.findings
                for path in routed.out_of_boundary_paths
            }
        )
        if outside:
            print(
                "Cited but out of PR:  "
                + ", ".join(outside)
                + "  (not granted; pass --allow-path to include)",
                file=stream,
            )

    inspection = result.inspection
    if inspection is not None:
        print(
            "Working tree:         "
            + (
                ", ".join(inspection.changed_paths)
                if inspection.changed_paths
                else "(no change)"
            ),
            file=stream,
        )
        print(f"HEAD after the run:   {inspection.head_sha}", file=stream)
        if inspection.residue_paths:
            print(
                f"Build/test residue:   {len(inspection.residue_paths)} ignored "
                "path(s), tolerated",
                file=stream,
            )
        if inspection.unexpected_ignored:
            print(
                "Unexpected ignored:   "
                + ", ".join(inspection.unexpected_ignored[:5]),
                file=stream,
            )

    validated = result.validated
    if validated is not None:
        print("Fix responses:", file=stream)
        for response in validated.responses:
            print(
                f"  {response.finding_id}: {response.outcome.value}"
                + (
                    f" ({', '.join(response.files_changed)})"
                    if response.files_changed
                    else ""
                ),
                file=stream,
            )

    print(f"Outcome:              {result.outcome.value}", file=stream)
    print("Reason:", file=stream)
    for reason in result.reasons or ("(none recorded)",):
        print(f"  - {reason}", file=stream)

    print(
        "Patch:                "
        + (
            result.patch_path
            if result.patch_path
            else (
                f"{len(result.patch.splitlines())} diff line(s), not written "
                "(use --write-patch)"
                if result.patch
                else "(none)"
            )
        ),
        file=stream,
    )
    print("GitHub write performed: No", file=stream)
    print("Commit or push performed: No", file=stream)


def render_json(result: FixResult, stream: TextIO) -> None:
    target = result.target
    request = result.request
    inspection = result.inspection
    validated = result.validated
    payload = {
        "outcome": result.outcome.value,
        "exit_code": result.exit_code,
        "dry_run": result.dry_run,
        "reasons": list(result.reasons),
        "agent_invoked": result.agent_invoked,
        "workspace_created": result.workspace_created,
        "github_write_performed": False,
        "github_requests_performed": 0,
        "commit_or_push_performed": False,
        "patch_path": result.patch_path,
        "target": None
        if target is None
        else {
            "repo": target.repo,
            "number": target.number,
            "head_sha": target.head_sha,
            "base_ref": target.base_ref,
            "ci_merge_base_sha": target.ci_merge_base_sha,
        },
        "request": None
        if request is None
        else {
            "round": request.round,
            "allowed_paths": [entry.display() for entry in request.allowed_paths],
            "change_set_boundary": [
                entry.display() for entry in request.change_set_boundary
            ],
            "findings": [
                {
                    "finding_id": r.finding.finding_id,
                    "severity": r.finding.severity.value,
                    "location": r.finding.location,
                    "cited_paths": list(r.cited_paths),
                    "allowed_paths": [e.display() for e in r.allowed_paths],
                    "out_of_boundary_paths": list(r.out_of_boundary_paths),
                }
                for r in request.findings
            ],
        },
        "workspace": None
        if inspection is None
        else {
            "head_sha": inspection.head_sha,
            "changed_paths": list(inspection.changed_paths),
            "residue_paths": list(inspection.residue_paths),
            "unexpected_ignored": list(inspection.unexpected_ignored),
            "patch_refused": inspection.patch_refused,
        },
        "responses": None
        if validated is None
        else [
            {
                "finding_id": r.finding_id,
                "target_head_sha": r.target_head_sha,
                "outcome": r.outcome.value,
                "files_changed": list(r.files_changed),
                "summary": r.summary,
                "verification": r.verification,
                "reason": r.reason,
                "scope_notes": r.scope_notes,
            }
            for r in validated.responses
        ],
    }
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _failure(outcome: FixRunOutcome, reason: str, out: TextIO, as_json: bool) -> int:
    result = FixResult(outcome=outcome, reasons=(reason,))
    if as_json:
        render_json(result, out)
    else:
        render_text(result, out, agent_label="(not started)")
    return FIX_EXIT_CODES[outcome]


def fix_main(
    argv: Sequence[str],
    *,
    agent=None,
    workspace=None,
    stream: TextIO | None = None,
) -> int:
    parser = build_fix_parser()
    args = parser.parse_args(list(argv))
    out = stream if stream is not None else sys.stdout

    if args.max_findings <= 0:
        print(
            f"error: --max-findings must be a positive integer, got "
            f"{args.max_findings}",
            file=out,
        )
        return EXIT_USAGE

    try:
        document = _read_document(args.review_json)
    except OSError as exc:
        return _failure(
            FixRunOutcome.ROUTING_INPUT_INVALID,
            f"the routing input could not be read: {exc}",
            out,
            args.json,
        )

    try:
        handoff = load_handoff(document, expected_repo=args.repo)
    except RoutingInputError as exc:
        return _failure(
            FixRunOutcome.ROUTING_INPUT_INVALID, str(exc), out, args.json
        )

    # The pull request number is never taken from the command line: it comes
    # from the validated routing input, together with the commit it belongs
    # to. Supplying them separately would make disagreeing with each other
    # possible.
    args.pr = handoff.target.number

    agent_label = "(injected)"
    if agent is None:
        if not args.agent_command:
            print(
                "error: --agent-command is required; there is no default coding "
                "agent",
                file=out,
            )
            return EXIT_USAGE
        try:
            argv_command = split_command(args.agent_command)
        except AgentCommandError as exc:
            print(f"error: {exc}", file=out)
            return EXIT_USAGE
        agent_label = " ".join(argv_command)
        agent = SubprocessAgent(
            argv_command,
            timeout=args.agent_timeout,
            env=build_env(dict(os.environ), tuple(args.agent_env)),
        )

    if workspace is None:
        workspace = _workspace(args)

    try:
        result = run_fix(
            agent=agent,
            workspace=workspace,
            target=handoff.target,
            verdict=handoff.verdict,
            allow_paths=tuple(args.allow_path),
            max_findings=args.max_findings,
            git_remote=args.git_remote,
            dry_run=args.dry_run,
        )
    except ScopeError as exc:
        return _failure(
            FixRunOutcome.REVIEW_REQUIRES_HUMAN, str(exc), out, args.json
        )

    if args.write_patch and result.patch:
        try:
            with open(args.write_patch, "w", encoding="utf-8") as handle:
                handle.write(result.patch)
        except OSError as exc:
            result = FixResult(
                outcome=FixRunOutcome.PATCH_WRITE_FAILED,
                reasons=result.reasons
                + (f"the fix could not be written to {args.write_patch}: {exc}",),
                target=result.target,
                request=result.request,
                validated=result.validated,
                inspection=result.inspection,
                agent_invoked=result.agent_invoked,
                workspace_created=result.workspace_created,
                agent_stdout=result.agent_stdout,
                agent_stderr=result.agent_stderr,
            )
        else:
            result = FixResult(
                outcome=result.outcome,
                reasons=result.reasons,
                target=result.target,
                request=result.request,
                validated=result.validated,
                inspection=result.inspection,
                agent_invoked=result.agent_invoked,
                workspace_created=result.workspace_created,
                dry_run=result.dry_run,
                patch_path=args.write_patch,
                agent_stdout=result.agent_stdout,
                agent_stderr=result.agent_stderr,
            )

    if args.print_raw_output and result.agent_stdout:
        print(
            "--- coding agent raw stdout (untrusted, never recorded) ---",
            file=sys.stderr,
        )
        print(result.agent_stdout, file=sys.stderr)

    if args.json:
        render_json(result, out)
    else:
        render_text(
            result,
            out,
            agent_label=agent_label,
            workspace_label=(
                workspace.describe() if hasattr(workspace, "describe") else None
            ),
        )
    return result.exit_code
