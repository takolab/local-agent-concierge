"""One fresh Independent Re-Review turn, bound to one pushed fix commit.

The shape is the review turn's, because the argument is the same one: a
review is evidence about the exact commit it read, so the target is verified
before the reviewer starts, captured, and verified again after -- and a
result about a commit the pull request has since moved off is discarded
rather than recorded.

What is new is the precondition. A review turn may start wherever CI is
READY. A re-review may start only at a *specific* commit: the fix the push
turn put on the branch. So ``PUSH_READY`` is revalidated rather than
remembered, and it is revalidated from GitHub, not from the document that
claimed it:

* the pull request still exists and is still the one the inputs describe,
* its head is still exactly the pushed fix SHA,
* authoritative CI for that head is READY *now*, and
* the merge context that CI tested is still the base branch tip, so a base
  that advanced after the push cannot leave a green record standing in for
  evidence about an integration nobody tested.

Any of those failing means no reviewer runs. That is deliberate and it is the
expensive-looking choice: a re-review is the most costly turn in the loop,
and the one whose wrong answer is most likely to be acted on.

This turn does **not** decide anything. It records what a fresh reviewer
found -- which original findings are resolved, and what is wrong with the
pull request now -- as two separate facts. It does not route a second fix, it
does not re-run the Coding Agent, and it does not merge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import comment_format
from .github_client import GitHubApiError
from .model import CiEvaluation, Verdict
from .rereview import (
    RE_REVIEW_EXIT_CODES,
    RE_REVIEW_ROUND,
    ReReview,
    ReReviewOutcome,
    ReReviewParseError,
    ReReviewShaBindingError,
    ReReviewValidationError,
)
from .rereview_input import ReReviewRequest
from .rereview_parser import parse
from .rereview_prompt import build_prompt
from .rereview_validation import validate
from .review_target import ReviewTarget, TargetNotVerified, drift_reasons, from_evaluation
from .reviewer_workspace import WorkspaceError
from .runner import verify_pull_request
from .verdict import MAX_COMMENT_CHARS


@dataclass(frozen=True)
class ReReviewResult:
    """Everything one re-review turn established, and what it did about it.

    ``rereview`` holds both collections, separately, exactly as the reviewer
    reported them. Nothing on this result combines them: there is no "is it
    done?" field, because this stage is not the component that answers that.
    """

    outcome: ReReviewOutcome
    reasons: tuple[str, ...] = ()
    request: ReReviewRequest | None = None
    pre_evaluation: CiEvaluation | None = None
    post_evaluation: CiEvaluation | None = None
    #: The freshly verified target the reviewer was actually pointed at.
    target: ReviewTarget | None = None
    rereview: ReReview | None = None
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
        caller keeps the PENDING / FAILED / AMBIGUOUS / STALE_TARGET
        vocabulary rather than a second one that means the same thing.
        """
        if self.outcome is ReReviewOutcome.TARGET_NOT_READY:
            return (
                self.pre_evaluation.exit_code
                if self.pre_evaluation is not None
                else RE_REVIEW_EXIT_CODES[ReReviewOutcome.API_ERROR]
            )
        return RE_REVIEW_EXIT_CODES[self.outcome]


def run_rereview(
    *,
    client,
    reader,
    reviewer,
    request: ReReviewRequest,
    expected_author: str,
    writer=None,
    dry_run: bool = False,
) -> ReReviewResult:
    """Run one fresh Independent Re-Review of the pushed fix in ``request``."""
    if not dry_run and writer is None:
        raise ValueError("a writer is required unless the re-review is a dry run")

    claimed = request.target
    pushed_sha = request.pushed_fix_sha

    def result(outcome: ReReviewOutcome, reasons, **kwargs) -> ReReviewResult:
        kwargs.setdefault("dry_run", dry_run)
        return ReReviewResult(
            outcome=outcome, reasons=tuple(reasons), request=request, **kwargs
        )

    # 1. Revalidate PUSH_READY against GitHub. The document said the fix was
    #    pushed and green; whether that is still true is a question only the
    #    live pull request can answer, and the answer decides whether a
    #    reviewer runs at all.
    pre = verify_pull_request(client, claimed.number)
    if pre.verdict is Verdict.API_ERROR:
        return result(
            ReReviewOutcome.API_ERROR,
            ("the re-review target could not be verified",) + pre.reasons,
            pre_evaluation=pre,
        )

    observed_head = pre.target.head_sha if pre.target is not None else None
    if observed_head != pushed_sha:
        # Checked before the CI verdict on purpose: "the pull request is not
        # at the fix any more" is a different fact from "its CI is not green",
        # and reporting the second when the first is true would send an
        # operator to look at the wrong thing.
        return result(
            ReReviewOutcome.TARGET_NOT_AT_FIX,
            (
                f"the pull request head is {observed_head or 'unresolved'}, not the "
                f"pushed fix {pushed_sha}; there is no re-review to run against this "
                "target",
            )
            + pre.reasons,
            pre_evaluation=pre,
        )

    if pre.verdict is not Verdict.READY:
        return result(
            ReReviewOutcome.TARGET_NOT_READY,
            (
                f"verification reported {pre.verdict.value} for the pushed fix "
                f"{pushed_sha}",
            )
            + pre.reasons,
            pre_evaluation=pre,
        )

    try:
        target = from_evaluation(claimed.repo, pre)
    except TargetNotVerified as exc:  # pragma: no cover - READY implies both
        return result(
            ReReviewOutcome.TARGET_NOT_READY, (str(exc),), pre_evaluation=pre
        )

    if target.base_ref != claimed.base_ref:
        return result(
            ReReviewOutcome.TARGET_NOT_AT_FIX,
            (
                f"the pull request now targets {target.base_ref!r}, not the "
                f"{claimed.base_ref!r} the fix was pushed against",
            ),
            pre_evaluation=pre,
            target=target,
        )

    # The push turn's own currency requirement, asserted again here because
    # the base branch can advance between the push and this run: CI that
    # tested a merge onto a commit that is no longer the base tip is not
    # evidence about the integration state a re-reviewer would be describing.
    if (
        pre.base_tip_at_verification is None
        or target.ci_merge_base_sha != pre.base_tip_at_verification
    ):
        return result(
            ReReviewOutcome.TARGET_NOT_AT_FIX,
            (
                f"authoritative CI for {pushed_sha} tested it merged onto "
                f"{target.ci_merge_base_sha}, which is not the current "
                f"{target.base_ref} tip "
                f"({pre.base_tip_at_verification or 'unknown'}); the fix's CI "
                "evidence is stale",
            ),
            pre_evaluation=pre,
            target=target,
        )

    identity = comment_format.RecordIdentity(
        repo=target.repo,
        number=target.number,
        head_sha=target.head_sha,
        base_sha=target.ci_merge_base_sha,
        round=RE_REVIEW_ROUND,
        role=comment_format.RE_REVIEWER_ROLE,
    )

    # 2. Cheap early exit: if this exact re-review is already recorded,
    #    running a reviewer would only produce a result we may not post.
    try:
        already = comment_format.find_record(
            reader.list_comments(target.number),
            identity,
            expected_author=expected_author,
        )
    except GitHubApiError as exc:
        return result(
            ReReviewOutcome.API_ERROR,
            (f"could not read existing comments: {exc}",),
            pre_evaluation=pre,
            target=target,
        )
    if already is not None:
        return result(
            ReReviewOutcome.COMMENT_ALREADY_EXISTS,
            (
                f"comment {already} already records round {RE_REVIEW_ROUND} of this "
                f"re-review for {target.head_sha} merged onto "
                f"{target.ci_merge_base_sha}",
            ),
            pre_evaluation=pre,
            target=target,
            existing_comment_id=already,
        )

    # 3. Run the reviewer against the exact pushed fix -- in a working
    #    directory that *is* that commit. The prompt names the SHA; the
    #    workspace is what stops a reviewer echoing it back after reading
    #    something else.
    try:
        run = reviewer.invoke(
            build_prompt(request, target), head_sha=target.head_sha
        )
    except WorkspaceError as exc:
        return result(
            ReReviewOutcome.REVIEWER_WORKSPACE_INVALID,
            (str(exc),),
            pre_evaluation=pre,
            target=target,
        )
    if not run.ok:
        return result(
            ReReviewOutcome.REVIEWER_FAILED,
            (run.failure or "the reviewer failed",),
            pre_evaluation=pre,
            target=target,
            reviewer_invoked=True,
            reviewer_stderr=run.stderr,
        )

    # 4. Parse and validate. Reviewer output is untrusted text: it becomes a
    #    re-review only by satisfying the contract, never by being plausible.
    try:
        rereview = validate(
            parse(run.stdout),
            target_head_sha=target.head_sha,
            original_finding_ids=request.original_finding_ids,
        )
    except ReReviewShaBindingError as exc:
        return result(
            ReReviewOutcome.RE_REVIEW_SHA_MISMATCH,
            (str(exc),),
            pre_evaluation=pre,
            target=target,
            reviewer_invoked=True,
            reviewer_stdout=run.stdout,
            reviewer_stderr=run.stderr,
        )
    except (ReReviewParseError, ReReviewValidationError) as exc:
        return result(
            ReReviewOutcome.RE_REVIEW_MALFORMED,
            (str(exc),),
            pre_evaluation=pre,
            target=target,
            reviewer_invoked=True,
            reviewer_stdout=run.stdout,
            reviewer_stderr=run.stderr,
        )

    body = comment_format.render_rereview(target, request, rereview)
    if len(body) > MAX_COMMENT_CHARS:
        return result(
            ReReviewOutcome.RE_REVIEW_MALFORMED,
            (
                f"the rendered re-review is {len(body)} characters, above GitHub's "
                f"{MAX_COMMENT_CHARS}-character comment limit",
            ),
            pre_evaluation=pre,
            target=target,
            rereview=rereview,
            reviewer_invoked=True,
            reviewer_stdout=run.stdout,
            reviewer_stderr=run.stderr,
        )

    # 5. Re-verify. The reviewer read one merge context; only if that is still
    #    the pull request's current, verified state does the re-review
    #    describe what a reader of the comment would go and look at.
    post = verify_pull_request(client, target.number)
    if post.verdict is Verdict.API_ERROR:
        return result(
            ReReviewOutcome.API_ERROR,
            ("the target could not be re-verified after the re-review",)
            + post.reasons,
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            reviewer_invoked=True,
        )
    if post.verdict is not Verdict.READY:
        return result(
            ReReviewOutcome.TARGET_STALE,
            (
                f"the target no longer verifies as READY ({post.verdict.value}) after "
                "the re-review",
            )
            + post.reasons,
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            reviewer_invoked=True,
        )

    drift = drift_reasons(target, from_evaluation(target.repo, post))
    if drift:
        return result(
            ReReviewOutcome.TARGET_STALE,
            drift,
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            reviewer_invoked=True,
        )

    if dry_run:
        return result(
            ReReviewOutcome.RE_REVIEW_VALID,
            ("dry run: the re-review is valid and would be recorded",),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            reviewer_invoked=True,
        )

    # 6. Last duplicate check before the write. This is the one that catches a
    #    retry whose previous POST succeeded but whose response was lost.
    try:
        already = comment_format.find_record(
            reader.list_comments(target.number),
            identity,
            expected_author=expected_author,
        )
    except GitHubApiError as exc:
        return result(
            ReReviewOutcome.API_ERROR,
            (f"could not re-check existing comments before writing: {exc}",),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            reviewer_invoked=True,
        )
    if already is not None:
        return result(
            ReReviewOutcome.COMMENT_ALREADY_EXISTS,
            (
                f"comment {already} already records this re-review; nothing was "
                "written",
            ),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            existing_comment_id=already,
            reviewer_invoked=True,
        )

    try:
        comment_id = writer.create_comment(target.number, body)
    except GitHubApiError as exc:
        return result(
            ReReviewOutcome.GITHUB_WRITE_FAILED,
            (
                f"{exc}. If this was a lost response rather than a rejected request, "
                "re-running finds the record by its marker and will not duplicate it.",
            ),
            pre_evaluation=pre,
            post_evaluation=post,
            target=target,
            rereview=rereview,
            comment_body=body,
            reviewer_invoked=True,
        )

    return result(
        ReReviewOutcome.RE_REVIEW_VALID,
        (f"recorded as comment {comment_id}",),
        pre_evaluation=pre,
        post_evaluation=post,
        target=target,
        rereview=rereview,
        comment_body=body,
        comment_id=comment_id,
        reviewer_invoked=True,
        github_write_performed=True,
    )
