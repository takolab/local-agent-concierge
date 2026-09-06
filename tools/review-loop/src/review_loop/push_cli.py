"""``review-loop push`` -- commit one validated candidate patch, and push it.

The fourth command, and **the first that can change this repository.** Three
things about its shape follow from that.

**It takes the fix turn's own JSON, and a patch file, and requires them to
agree.** Not a pull request number, not a branch, not a commit range. The
handoff says which commit the patch is against and what the patch's SHA-256
is; the file has to hash to that. Neither half is sufficient: a handoff with
the wrong patch, or a patch with no handoff, is not a fix anyone validated.

**Its write authority is one ref.** The branch is derived from GitHub's own
pull request object, never from the handoff, the review text, the agent's
output or a flag. There is no ``--branch``, and adding one would be the
change that undoes this design -- see :mod:`review_loop.push_branch`.

**It uses the reader you already have.** The GitHub side is the read-only
``gh api --method GET`` client from PR #28: this command adds no GitHub write
of any kind, opens no new endpoint, and posts no comment. The one write it
performs is a ``git push``, which travels over your existing git credential
for the remote -- not over a token this tool introduces.

What it does not do, deliberately: no merge, no re-review, no second fix
round, no force push, no branch creation, and no automatic repair of a run
that ended badly. Those all stay with the human.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence, TextIO

from .fix_handoff import FixHandoff, FixHandoffError, load_handoff
from .github_client import GitHubApiError, GitHubClient
from .model import EXIT_USAGE, short_sha
from .push_response import (
    BOUNDARY_CLEAN,
    BOUNDARY_EXCEEDED,
    BOUNDARY_UNKNOWN,
    DEFAULT_CI_POLL_SECONDS,
    DEFAULT_CI_TIMEOUT_SECONDS,
    PUSH_EXIT_CODES,
    PushOutcome,
)
from .push_runner import PushResult, run_push
from .reviewer_workspace import DEFAULT_REMOTE, ExistingWorkspace, PreparedWorkspace

PUSH_ROLE = "fix commit"

_EPILOG = """\
exit codes:
  0   PUSH_READY              the fix commit is on the pull request branch and
                              authoritative CI for that exact commit is READY
                              against the current merge context
  0   PUSH_PREPARED           --dry-run: the candidate patch verified and
                              applied; nothing was committed or pushed
  60  PUSH_INPUT_INVALID      the push input is not a validated candidate patch
  61  PUSH_BRANCH_REFUSED     the destination could not be established safely:
                              a fork head, a closed PR, the default branch, an
                              unusable branch name, or a --git-remote that
                              names a different repository
  62  PUSH_TARGET_STALE       the pull request moved, or its branch is
                              somewhere this runner cannot account for
  63  PATCH_IDENTITY_MISMATCH the patch is not the validated candidate patch,
                              does not apply, or produced something else
  64  COMMIT_REFUSED          the workspace was not a clean reviewed head, or
                              the commit is not exactly the candidate patch
  65  PUSH_FAILED             the remote's own --porcelain report REFUSED the
                              ref (a recognised rejection, not merely a
                              failure), so nothing was written
  66  PUSH_NOT_VERIFIED       the remote gave no answer, or one that settles
                              nothing ([remote failure], a timeout, a local
                              hook), and the branch does not read back as the
                              created commit; an absent commit proves nothing,
                              because a commit can land and then be erased.
                              REMOTE STATE IS NOT KNOWN
  67  CI_FAILED               authoritative CI for the pushed commit failed
  68  CI_PENDING              CI had not finished within --ci-timeout
  69  CI_STALE_TARGET         the pull request moved off the pushed commit, its
                              CI no longer describes the current merge, or a
                              lost response hid a push that did land
  70  CI_AMBIGUOUS            CI state for the pushed commit is undecidable
  71  PUSH_WORKSPACE_INVALID  the workspace could not be prepared or verified
  72  PUSH_API_ERROR          GitHub could not be queried before the push
  73  CI_API_ERROR            GitHub could not be queried while waiting for CI
  74  PUSH_WROTE_UNEXPECTED_REFS  the remote reported updating a ref this run
                              did not ask for; something was written, and more
                              than the boundary permits. Inspect the remote
  75  PUSH_BOUNDARY_NOT_VERIFIED  the remote reported trying a ref this run did
                              not ask for, with an answer that settles nothing;
                              whether the boundary was exceeded is unknown.
                              What the authorised branch holds is still
                              reported. Inspect the remote
  2   usage error

Exit codes differ in what changed. 60-65, 71 and 72 mean this run performed
no repository write -- which is not the same claim as the branch being where
it was, since another actor can move it at any time. 0 (PUSH_READY) and 67-70,
73 mean the fix commit IS on the branch. 66 means remote state is not known -- read the branch before
doing anything else, and do not re-run blind. 74 means something WAS written
and more than one ref moved; 75 means whether more than one ref moved could
not be established. Both report what the authorised branch holds either way --
the boundary and the branch are two facts, and this command does not let
uncertainty about one erase the other.

WRITE AUTHORITY. This command performs exactly one repository write: a
fast-forward `git push` of one commit to refs/heads/<the pull request's head
branch>, over your existing git credential for the remote, conditional on that
ref still being exactly the reviewed head. The condition is carried as
--force-with-lease=<that ref>:<reviewed head>, which despite the flag's name
authorises no rewrite: the commit's parent is already proven to be that value,
so the update is an ordinary fast-forward and the lease only makes it atomic
with the read that authorised it. The push also passes --no-follow-tags and
--recurse-submodules=no, because an explicit refspec bounds what this runner
asks for but not what push.followTags or push.recurseSubmodules would add to
the request; and the remote's own report is checked for refs nobody asked for.
It never forces,
never writes a tag, never creates a branch that does not exist, never pushes
to a default branch or a fork, and never merges. The branch name comes from
GitHub's pull request object alone; there is no flag that can change it. The
destination repository is checked too: EVERY URL git reports for --git-remote
-- fetch and push, read with --all, because a push writes to every configured
push URL -- must name the repository the fix handoff describes, so a mirror
holding the same branch at the same commit is refused rather than pushed to.

The GitHub side has the same single authority: the client is scoped from the
validated handoff, never from the current directory, and the pull request must
be a pull request in that repository at both its head and its base. --repo is
an assertion about the handoff, not a selector.

That guarantee is about the git argument vectors this command constructs. Your
`pre-commit`, `prepare-commit-msg` and `pre-push` hooks still run -- they are
deliberately not bypassed -- and a hook is an arbitrary program. Your hooks are
trusted; the runner's own argv is bounded. A hook that edits files is still
caught, because the commit is re-hashed against the candidate patch.

GitHub itself is read-only here: the same `gh api --method GET` client the
verification command uses. No comment, label, review or merge is written.

IDEMPOTENCY. A retry re-derives what happened from git and GitHub rather than
from any record. If the pull request branch already holds a commit whose
parent is the reviewed head and whose diff is exactly this candidate patch,
that IS this fix: nothing is committed or pushed, and the run resumes at the
CI wait. Local commits are never authoritative and are never reused.
"""


def build_push_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-loop push",
        description=(
            "Commit one validated candidate patch as an exact fix commit on the "
            "reviewed head, push it to the pull request's own branch, verify the "
            "pushed commit by reading the remote ref back, and wait for "
            "authoritative CI on that exact commit."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fix-json",
        required=True,
        help=(
            "the 'review-loop fix --json' document describing the candidate patch, "
            "or '-' to read it from stdin"
        ),
    )
    parser.add_argument(
        "--patch",
        default=None,
        help=(
            "the candidate patch file. Defaults to the 'patch_path' the fix "
            "document records. Whichever is used, its bytes must hash to the "
            "digest the fix turn recorded."
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "assert that the push input describes this repository, as "
            "owner/name. It is a check, not a selector: the repository this "
            "command reads from GitHub and pushes to always comes from the "
            "validated fix handoff."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help=(
            "local clone the fix commit is built in and pushed from "
            "(default: the current directory)"
        ),
    )
    parser.add_argument(
        "--commit-cwd",
        default=None,
        help=(
            "build the fix commit in this directory instead of a worktree the "
            "runner prepares. It must be a clean checkout of the exact reviewed "
            "commit, and is verified before anything is applied. Its contents "
            "will be modified and committed."
        ),
    )
    parser.add_argument(
        "--git-remote",
        default=DEFAULT_REMOTE,
        help=(
            "remote the pull request branch is read from and pushed to "
            f"(default: {DEFAULT_REMOTE})"
        ),
    )
    parser.add_argument(
        "--ci-timeout",
        type=float,
        default=DEFAULT_CI_TIMEOUT_SECONDS,
        help=(
            "seconds to wait for authoritative CI on the pushed commit "
            f"(default: {DEFAULT_CI_TIMEOUT_SECONDS:g}). The wait is bounded: on "
            "expiry the run reports CI_PENDING, with the push already made."
        ),
    )
    parser.add_argument(
        "--ci-poll",
        type=float,
        default=DEFAULT_CI_POLL_SECONDS,
        help=(
            "seconds between CI observations "
            f"(default: {DEFAULT_CI_POLL_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "verify the candidate patch and apply it in a throwaway worktree, then "
            "stop. Nothing is committed and nothing is pushed."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    return parser


def _workspace(args, number: int):
    """Choose where the fix commit is built.

    Preparing a worktree is the default and the recommended path: it is a
    fresh detached checkout of the reviewed head, so "no unrelated workspace
    change was committed" is true by construction as well as by the check.
    ``--commit-cwd`` exists for an operator who deliberately manages their
    own checkout, and it is verified, not trusted.
    """
    if args.commit_cwd is not None:
        return ExistingWorkspace(args.commit_cwd, role=PUSH_ROLE)
    return PreparedWorkspace(
        args.repo_root or os.getcwd(),
        number,
        remote=args.git_remote,
        role=PUSH_ROLE,
    )


def _read_document(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _mutation_line(result: PushResult) -> str:
    """What changed, in one line an operator can act on.

    Every claim here is about *this run's own write*. It is deliberately not a
    claim that the branch is where it was: another actor can move it at any
    moment, and after a refused push the branch very often is not where the
    pre-flight found it. What this runner can establish is that it did not
    write, which is the narrower and true statement.
    """
    mutated = result.repository_mutated
    if result.outcome is PushOutcome.PUSH_WROTE_UNEXPECTED_REFS:
        # Written, and beyond authority: neither an ordinary push nor an
        # unknown. Whatever the branch read-back established is still stated,
        # because the boundary being wrong is not a reason to forget it.
        return (
            "Yes -- the remote reported an unexpected ref update. "
            + (
                f"{result.pushed_sha} is on the branch"
                if result.pushed_sha
                else "the authorised branch does not hold this run's commit"
            )
            + ". Inspect the remote"
        )
    if result.outcome is PushOutcome.PUSH_BOUNDARY_NOT_VERIFIED:
        return (
            (
                f"Yes -- {result.pushed_sha} is on the branch"
                if result.pushed_sha
                else "UNKNOWN -- the authorised branch does not hold this run's commit"
            )
            + ", and the remote's answer about a ref this run did not touch does "
            "not establish whether it was written. Inspect the remote"
        )
    if mutated is None:
        return (
            "UNKNOWN -- the push ran and the branch did not read back as the "
            "created commit"
        )
    if not mutated:
        return "No -- this run performed no repository write"
    if result.already_pushed:
        # Not "by an earlier run": this runner cannot tell an earlier run of
        # its own from another actor that put the same commit there, and the
        # honest claim is about the branch's state rather than about who is
        # responsible for it.
        return (
            f"Yes -- the branch already held this exact fix ({result.pushed_sha}); "
            "this run did not move the ref"
        )
    return f"Yes -- {result.pushed_sha} was pushed by this run"


def render_text(result: PushResult, stream: TextIO, *, workspace_label: str | None = None) -> None:
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

    push_target = result.push_target
    print(
        "Push target ref:      "
        + (push_target.ref if push_target is not None else "(not established)"),
        file=stream,
    )
    if push_target is not None:
        print(
            f"Default branch:       {push_target.default_branch} (never pushed to)",
            file=stream,
        )
    if workspace_label is not None:
        print(f"Commit workspace:     {workspace_label}", file=stream)
    print(
        "Candidate patch:      " + (result.patch_path or "(not resolved)"),
        file=stream,
    )

    commit = result.commit
    print(
        "Fix commit:           "
        + (
            f"{commit.sha} (parent {commit.parent_sha})"
            if commit is not None
            else "(none created)"
        ),
        file=stream,
    )
    if commit is not None:
        print(f"Committed paths:      {', '.join(commit.changed_paths)}", file=stream)
        print(f"Candidate patch id:   {commit.patch_sha256}", file=stream)
    print(
        "Pushed commit SHA:    " + (result.pushed_sha or "(none)"),
        file=stream,
    )
    print(f"Repository mutated:   {_mutation_line(result)}", file=stream)
    print(
        "Write boundary:       "
        + {
            BOUNDARY_CLEAN: "clean -- only the authorised ref was reported",
            BOUNDARY_EXCEEDED: "EXCEEDED -- the remote wrote a ref nobody asked for",
            BOUNDARY_UNKNOWN: "UNKNOWN -- a ref nobody asked for was reported "
            "with an answer that settles nothing",
        }[result.boundary_status],
        file=stream,
    )

    evaluation = result.ci_evaluation
    print(
        "Authoritative CI:     "
        + (
            f"{evaluation.verdict.value} after {result.ci_polls} observation(s)"
            if evaluation is not None
            else "(not observed)"
        ),
        file=stream,
    )
    if evaluation is not None and evaluation.target is not None:
        print(
            f"CI bound to:          {evaluation.target.head_sha}"
            + (
                "  (== the pushed commit)"
                if evaluation.target.head_sha == result.pushed_sha
                else "  (NOT the pushed commit)"
            ),
            file=stream,
        )
        print(
            f"CI merge base:        {evaluation.ci_merge_base_sha or '(not established)'}",
            file=stream,
        )

    print(f"Outcome:              {result.outcome.value}", file=stream)
    print("Reason:", file=stream)
    for reason in result.reasons or ("(none recorded)",):
        print(f"  - {reason}", file=stream)
    print("GitHub write performed: No", file=stream)


def render_json(result: PushResult, stream: TextIO) -> None:
    target = result.target
    push_target = result.push_target
    commit = result.commit
    evaluation = result.ci_evaluation
    verified = result.verified_target
    provenance = result.fix_provenance
    payload = {
        "outcome": result.outcome.value,
        "exit_code": result.exit_code,
        "dry_run": result.dry_run,
        "reasons": list(result.reasons),
        "repository_mutated": result.repository_mutated,
        # Reported beside the mutation rather than folded into it: whether the
        # push stayed inside its one-ref boundary is a different question from
        # whether the authorised branch moved.
        "boundary_status": result.boundary_status,
        "push_performed": result.push_performed,
        "already_pushed": result.already_pushed,
        "commit_created": result.commit_created,
        "pushed_sha": result.pushed_sha,
        "patch_path": result.patch_path,
        "github_write_performed": False,
        "target": None
        if target is None
        else {
            "repo": target.repo,
            "number": target.number,
            "head_sha": target.head_sha,
            "base_ref": target.base_ref,
            "ci_merge_base_sha": target.ci_merge_base_sha,
        },
        "push_target": None
        if push_target is None
        else {
            "branch": push_target.branch,
            "ref": push_target.ref,
            "base_ref": push_target.base_ref,
            "default_branch": push_target.default_branch,
            "head_sha_at_resolution": push_target.head_sha,
        },
        "commit": None
        if commit is None
        else {
            "sha": commit.sha,
            "parent_sha": commit.parent_sha,
            "tree_sha": commit.tree_sha,
            "patch_sha256": commit.patch_sha256,
            "changed_paths": list(commit.changed_paths),
        },
        "ci": None
        if evaluation is None
        else {
            "verdict": evaluation.verdict.value,
            "polls": result.ci_polls,
            "head_sha": None if evaluation.target is None else evaluation.target.head_sha,
            "bound_to_pushed_commit": (
                evaluation.target is not None
                and evaluation.target.head_sha == result.pushed_sha
            ),
            "ci_merge_base_sha": evaluation.ci_merge_base_sha,
            "base_tip_at_verification": evaluation.base_tip_at_verification,
            "reasons": list(evaluation.reasons),
        },
        # Which validated review caused this fix, and what git said the fix
        # is. Present whenever the branch is known to hold the fix, on the
        # created-commit path and the already-pushed path alike -- the next
        # stage needs the link between a review and a fix, and it is the only
        # stage that can no longer derive it from git itself.
        "fix_provenance": None
        if provenance is None
        else {
            "source_review_sha256": provenance.source_review_sha256,
            "source_round": provenance.source_round,
            "source_reviewed_head_sha": provenance.source_reviewed_head_sha,
            "source_ci_merge_base_sha": provenance.source_ci_merge_base_sha,
            "source_finding_ids": list(provenance.source_finding_ids),
            "source_patch_sha256": provenance.source_patch_sha256,
            "fix_sha": provenance.fix_sha,
            "fix_parent_sha": provenance.fix_parent_sha,
            "fix_patch_sha256": provenance.fix_patch_sha256,
        },
        # The verified merge context the next stage would re-review, present
        # only when this run established one.
        "verified_target": None
        if verified is None
        else {
            "repo": verified.repo,
            "number": verified.number,
            "head_sha": verified.head_sha,
            "base_ref": verified.base_ref,
            "ci_merge_base_sha": verified.ci_merge_base_sha,
        },
    }
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")


def _failure(outcome: PushOutcome, reason: str, out: TextIO, as_json: bool) -> int:
    result = PushResult(outcome=outcome, reasons=(reason,))
    if as_json:
        render_json(result, out)
    else:
        render_text(result, out)
    return PUSH_EXIT_CODES[outcome]


def _patch_path(args, handoff: FixHandoff) -> str | None:
    return args.patch or handoff.patch_path


def push_main(
    argv: Sequence[str],
    *,
    client=None,
    workspace=None,
    stream: TextIO | None = None,
    clock=None,
    sleep=None,
) -> int:
    parser = build_push_parser()
    args = parser.parse_args(list(argv))
    out = stream if stream is not None else sys.stdout

    if args.ci_timeout < 0:
        print(f"error: --ci-timeout must not be negative, got {args.ci_timeout}", file=out)
        return EXIT_USAGE
    if args.ci_poll <= 0:
        print(f"error: --ci-poll must be positive, got {args.ci_poll}", file=out)
        return EXIT_USAGE

    try:
        document = _read_document(args.fix_json)
    except OSError as exc:
        return _failure(
            PushOutcome.PUSH_INPUT_INVALID,
            f"the push input could not be read: {exc}",
            out,
            args.json,
        )

    try:
        handoff = load_handoff(document, expected_repo=args.repo)
    except FixHandoffError as exc:
        return _failure(PushOutcome.PUSH_INPUT_INVALID, str(exc), out, args.json)

    patch_path = _patch_path(args, handoff)
    if not patch_path:
        return _failure(
            PushOutcome.PUSH_INPUT_INVALID,
            "no candidate patch file: the fix document records no 'patch_path', so "
            "--patch is required. Re-run the fix turn with --write-patch, or point "
            "at the patch it wrote",
            out,
            args.json,
        )

    repo_root = args.repo_root or os.getcwd()

    if client is None:
        # Scoped from the **validated handoff**, never from the current
        # directory. `detect_repository()` would read the repository out of
        # whatever clone the operator happens to be standing in, which would
        # give the GitHub side of this command a different authority from the
        # git side -- and the whole point of the slice is that they are one.
        # `--repo` remains an assertion about the handoff (`load_handoff`
        # above refuses a document that describes anything else), not a
        # competing source of truth.
        try:
            client = GitHubClient(handoff.target.repo)
        except (GitHubApiError, ValueError) as exc:
            return _failure(PushOutcome.PUSH_API_ERROR, str(exc), out, args.json)

    if workspace is None:
        workspace = _workspace(args, handoff.target.number)

    result = run_push(
        client=client,
        workspace=workspace,
        repo_root=repo_root,
        handoff=handoff,
        patch_path=patch_path,
        git_remote=args.git_remote,
        ci_timeout=args.ci_timeout,
        ci_poll_seconds=args.ci_poll,
        clock=clock,
        sleep=sleep,
        dry_run=args.dry_run,
    )

    if args.json:
        render_json(result, out)
    else:
        render_text(
            result,
            out,
            workspace_label=(
                workspace.describe() if hasattr(workspace, "describe") else None
            ),
        )
    return result.exit_code
