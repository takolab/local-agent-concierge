"""One Independent Review turn, bound to one verified pull request state.

The order of the steps is the design. Verification decides whether a review
may start at all (PR #28); the target is captured *before* the reviewer runs;
and verification runs again *after* it, because a reviewer takes minutes and a
pull request can move in minutes. A review of a commit is only evidence about
that commit, so if the target moved while the reviewer was reading it, the
result is discarded rather than recorded against a state nobody reviewed.

The duplicate check is deliberately performed twice: once before the reviewer,
to avoid paying for a review that is already recorded, and once immediately
before the write, because that is the only one that protects against a retry
whose earlier POST actually succeeded. Neither makes the write atomic --
GitHub offers no compare-and-set on issue comments -- so a narrow race remains
between the last check and the POST. It is narrowed, named, and documented,
not papered over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import comment_format
from .github_client import GitHubApiError
from .model import CiEvaluation, Verdict
from .review_target import ReviewTarget, TargetNotVerified, drift_reasons, from_evaluation
from .reviewer_prompt import build_prompt
from .runner import verify_pull_request
from .verdict import (
    MAX_COMMENT_CHARS,
    SUPPORTED_ROUND,
    REVIEW_EXIT_CODES,
    ReviewOutcome,
    ReviewVerdict,
    ShaBindingError,
    VerdictParseError,
    VerdictValidationError,
)
from .verdict_parser import parse
from .verdict_validation import validate


@dataclass(frozen=True)
class ReviewResult:
    """Everything one review turn established, and what it did about it."""

    outcome: ReviewOutcome
    reasons: tuple[str, ...] = ()
    pre_evaluation: CiEvaluation | None = None
    post_evaluation: CiEvaluation | None = None
    target: ReviewTarget | None = None
    verdict: ReviewVerdict | None = None
    comment_body: str | None = None
    comment_id: int | None = None
    existing_comment_id: int | None = None
    reviewer_invoked: bool = False
    github_write_performed: bool = False
    dry_run: bool = False
    reviewer_stderr: str = field(default="", repr=False)
    reviewer_stdout: str = field(default="", repr=False)

    @property
    def exit_code(self) -> int:
        """The process exit status for this outcome.

        ``TARGET_NOT_READY`` reports the verification verdict's own code, so a
        caller keeps the PENDING / FAILED / AMBIGUOUS / STALE_TARGET vocabulary
        PR #28 already defined instead of a second one that means the same.
        """
        if self.outcome is ReviewOutcome.TARGET_NOT_READY:
            return (
                self.pre_evaluation.exit_code
                if self.pre_evaluation is not None
                else REVIEW_EXIT_CODES[ReviewOutcome.API_ERROR]
            )
        return REVIEW_EXIT_CODES[self.outcome]


def _existing_record(reader, number: int, identity) -> int | None:
    """Return the id of a comment already recording this identity, if any.

    Identity comes from the machine marker alone. A human comment under the
    same heading -- which is how every review in this repository has been
    written so far -- is not an automation record and must not suppress one.
    """
    for comment in reader.list_comments(number):
        if comment_format.body_records(comment.body, identity):
            return comment.comment_id
    return None


def run_review(
    *,
    client,
    reader,
    reviewer,
    repo: str,
    number: int,
    writer=None,
    dry_run: bool = False,
) -> ReviewResult:
    """Run one review turn against pull request ``number``."""
    if not dry_run and writer is None:
        raise ValueError("a writer is required unless the review is a dry run")

    # 1. May a review start at all? Anything but READY stops here, with the
    #    reviewer never invoked: reviewing a commit whose CI state is unknown
    #    produces a record that claims more than was verified.
    pre = verify_pull_request(client, number)
    if pre.verdict is not Verdict.READY:
        return ReviewResult(
            outcome=ReviewOutcome.TARGET_NOT_READY,
            reasons=(f"verification reported {pre.verdict.value}",) + pre.reasons,
            pre_evaluation=pre,
            dry_run=dry_run,
        )

    try:
        target = from_evaluation(repo, pre)
    except TargetNotVerified as exc:  # pragma: no cover - READY implies both
        return ReviewResult(
            outcome=ReviewOutcome.TARGET_NOT_READY,
            reasons=(str(exc),),
            pre_evaluation=pre,
            dry_run=dry_run,
        )

    identity = comment_format.RecordIdentity(
        repo=repo, number=target.number, head_sha=target.head_sha, round=SUPPORTED_ROUND
    )

    # 2. Cheap early exit: if this exact review is already recorded, running a
    #    reviewer would only produce a result we are not allowed to post.
    try:
        already = _existing_record(reader, number, identity)
    except GitHubApiError as exc:
        return ReviewResult(
            outcome=ReviewOutcome.API_ERROR,
            reasons=(f"could not read existing comments: {exc}",),
            pre_evaluation=pre,
            target=target,
            dry_run=dry_run,
        )
    if already is not None:
        return ReviewResult(
            outcome=ReviewOutcome.COMMENT_ALREADY_EXISTS,
            reasons=(
                f"comment {already} already records round {SUPPORTED_ROUND} of this "
                f"review for {target.head_sha}",
            ),
            pre_evaluation=pre,
            target=target,
            existing_comment_id=already,
            dry_run=dry_run,
        )

    # 3. Run the reviewer against the exact target.
    run = reviewer.invoke(build_prompt(target))
    if not run.ok:
        return ReviewResult(
            outcome=ReviewOutcome.REVIEWER_FAILED,
            reasons=(run.failure or "the reviewer failed",),
            pre_evaluation=pre,
            target=target,
            reviewer_invoked=True,
            reviewer_stderr=run.stderr,
            dry_run=dry_run,
        )

    # 4. Parse and validate. Reviewer output is untrusted text: it becomes a
    #    verdict only by satisfying the contract, never by being plausible.
    try:
        verdict = validate(parse(run.stdout), target_head_sha=target.head_sha)
    except ShaBindingError as exc:
        return ReviewResult(
            outcome=ReviewOutcome.REVIEW_SHA_MISMATCH,
            reasons=(str(exc),),
            pre_evaluation=pre,
            target=target,
            reviewer_invoked=True,
            reviewer_stdout=run.stdout,
            reviewer_stderr=run.stderr,
            dry_run=dry_run,
        )
    except (VerdictParseError, VerdictValidationError) as exc:
        return ReviewResult(
            outcome=ReviewOutcome.REVIEW_MALFORMED,
            reasons=(str(exc),),
            pre_evaluation=pre,
            target=target,
            reviewer_invoked=True,
            reviewer_stdout=run.stdout,
            reviewer_stderr=run.stderr,
            dry_run=dry_run,
        )

    body = comment_format.render(target, verdict)
    if len(body) > MAX_COMMENT_CHARS:
        return ReviewResult(
            outcome=ReviewOutcome.REVIEW_MALFORMED,
            reasons=(
                f"the rendered review is {len(body)} characters, above GitHub's "
                f"{MAX_COMMENT_CHARS}-character comment limit",
            ),
            pre_evaluation=pre,
            target=target,
            verdict=verdict,
            reviewer_invoked=True,
            reviewer_stdout=run.stdout,
            reviewer_stderr=run.stderr,
            dry_run=dry_run,
        )

    # 5. Re-verify. The reviewer read one merge context; only if that is still
    #    the pull request's current, verified state does the review describe
    #    what a reader of the comment would go and look at.
    post = verify_pull_request(client, number)
    if post.verdict is Verdict.API_ERROR:
        return ReviewResult(
            outcome=ReviewOutcome.API_ERROR,
            reasons=("the target could not be re-verified after the review",)
            + post.reasons,
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            reviewer_invoked=True,
            dry_run=dry_run,
        )
    if post.verdict is not Verdict.READY:
        return ReviewResult(
            outcome=ReviewOutcome.TARGET_STALE,
            reasons=(
                f"the target no longer verifies as READY ({post.verdict.value}) after "
                "the review",
            )
            + post.reasons,
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            reviewer_invoked=True,
            dry_run=dry_run,
        )

    drift = drift_reasons(target, from_evaluation(repo, post))
    if drift:
        return ReviewResult(
            outcome=ReviewOutcome.TARGET_STALE,
            reasons=drift,
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            reviewer_invoked=True,
            dry_run=dry_run,
        )

    if dry_run:
        return ReviewResult(
            outcome=ReviewOutcome.REVIEW_VALID,
            reasons=("dry run: the review is valid and would be recorded",),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            reviewer_invoked=True,
            dry_run=True,
        )

    # 6. Last duplicate check before the write. This is the one that catches a
    #    retry whose previous POST succeeded but whose response was lost.
    try:
        already = _existing_record(reader, number, identity)
    except GitHubApiError as exc:
        return ReviewResult(
            outcome=ReviewOutcome.API_ERROR,
            reasons=(f"could not re-check existing comments before writing: {exc}",),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            reviewer_invoked=True,
        )
    if already is not None:
        return ReviewResult(
            outcome=ReviewOutcome.COMMENT_ALREADY_EXISTS,
            reasons=(
                f"comment {already} already records this review; nothing was written",
            ),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            existing_comment_id=already,
            reviewer_invoked=True,
        )

    try:
        comment_id = writer.create_comment(number, body)
    except GitHubApiError as exc:
        return ReviewResult(
            outcome=ReviewOutcome.GITHUB_WRITE_FAILED,
            reasons=(
                f"{exc}. If this was a lost response rather than a rejected request, "
                "re-running finds the record by its marker and will not duplicate it.",
            ),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            verdict=verdict,
            comment_body=body,
            reviewer_invoked=True,
        )

    return ReviewResult(
        outcome=ReviewOutcome.REVIEW_VALID,
        reasons=(f"recorded as comment {comment_id}",),
        pre_evaluation=pre,
        post_evaluation=post,
        target=target,
        verdict=verdict,
        comment_body=body,
        comment_id=comment_id,
        reviewer_invoked=True,
        github_write_performed=True,
    )
