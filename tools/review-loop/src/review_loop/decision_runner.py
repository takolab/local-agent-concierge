"""One merge-brief turn: revalidate the pull request, classify, record, stop.

The shape is the review and re-review turns' -- verify, act, record -- with
one turn's worth of work removed and one rule added.

Removed: there is no reviewer. This turn starts no subprocess, reads no
working tree and forms no opinion. Everything it says was established by an
earlier turn, and its only job is to say it in one place, currently, with the
next workflow action derived from it mechanically.

Added: **a stale chain is not classified, it is refused.** The three
documents describe a re-review of one exact commit merged onto one exact
base. If the pull request has moved off that state, the documents are still
true and no longer relevant, and the difference between those two things is
the whole reason this stage exists. So the pull request is re-read from
GitHub first, and only a chain that still describes it becomes a brief.

What this turn cannot do is worth listing, because the classification names
actions and a reader could reasonably wonder whether it takes them. It does
not merge, approve, close, label, push, commit, invoke a Coding Agent, start
another review, or route a finding anywhere. Its entire write surface is the
one it inherits: a single issue comment, through the same writer the review
turns use, on the one path that ends in a current brief.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import comment_format
from .decision import (
    DECISION_EXIT_CODES,
    Classification,
    DecisionFacts,
    DecisionOutcome,
    NextAction,
    classify,
    gather_facts,
)
from .decision_input import DecisionRequest
from .github_client import GitHubApiError
from .model import CiEvaluation, Verdict
from .review_target import ReviewTarget, TargetNotVerified, drift_reasons, from_evaluation
from .runner import verify_pull_request
from .verdict import MAX_COMMENT_CHARS


@dataclass(frozen=True)
class DecisionResult:
    """What one merge-brief turn established, and what it did about it.

    ``facts`` and ``classification`` are separate fields for the same reason
    they are separate types: the facts are what the evidence says, the
    classification is one function of them, and a consumer that wants to apply
    a different policy should be able to read the first without inheriting the
    second.
    """

    outcome: DecisionOutcome
    reasons: tuple[str, ...] = ()
    request: DecisionRequest | None = None
    evaluation: CiEvaluation | None = None
    #: The pull request state as re-verified by *this* turn. ``None`` when the
    #: pull request could not be verified, or has moved off the re-reviewed
    #: commit, in which case there is no current target to describe.
    current_target: ReviewTarget | None = None
    facts: DecisionFacts | None = None
    classification: Classification | None = None
    #: The rendered artifact: a Merge Decision Brief when the evidence is
    #: current, and the stale-chain diagnostic when it is not. Only the first
    #: is ever written to GitHub.
    brief: str | None = None
    comment_id: int | None = None
    existing_comment_id: int | None = None
    github_write_performed: bool = False
    dry_run: bool = False

    @property
    def next_action(self) -> NextAction | None:
        return None if self.classification is None else self.classification.next_action

    @property
    def exit_code(self) -> int:
        return DECISION_EXIT_CODES[self.outcome]


def _currency_reasons(
    request: DecisionRequest, evaluation: CiEvaluation
) -> tuple[tuple[str, ...], ReviewTarget | None]:
    """Why the re-review no longer describes the pull request, if it does not.

    Ordered so the first reason is the one an operator should act on. "The
    head moved" and "CI is not green" are different problems with different
    fixes, and reporting the second when the first is true sends them to look
    at the wrong thing -- the same ordering the re-review turn established.
    """
    recorded = request.recorded_target

    observed_head = evaluation.target.head_sha if evaluation.target is not None else None
    if observed_head != recorded.head_sha:
        return (
            (
                f"the pull request head is {observed_head or 'unresolved'}, not the "
                f"re-reviewed fix {recorded.head_sha}; the re-review is evidence "
                "about a commit this pull request has moved off",
            ),
            None,
        )

    if evaluation.verdict is not Verdict.READY:
        return (
            (
                f"authoritative CI for {recorded.head_sha} verifies as "
                f"{evaluation.verdict.value}, not READY, so the green CI the "
                "re-review rested on is not the current state",
            )
            + evaluation.reasons,
            None,
        )

    try:
        current = from_evaluation(recorded.repo, evaluation)
    except TargetNotVerified as exc:  # pragma: no cover - READY implies both
        return ((str(exc),), None)

    reasons = list(drift_reasons(recorded, current))

    # Same head is not enough. Two different things can go wrong with the
    # base, and they surface at different checks:
    #
    # * The base advances and the old CI evidence is left behind -- caught by
    #   verification itself, which reports STALE_TARGET rather than READY, so
    #   the branch above has already returned by the time this runs.
    # * The base advances, CI re-runs green against the new merge, and
    #   verification reports READY again -- for an integration state nobody
    #   re-reviewed. That is `drift_reasons` above, comparing the re-reviewed
    #   merge base against the current one.
    #
    # What is left for this check is the case neither covers: a READY
    # evaluation that never established a base tip at all. Belt and braces,
    # exactly as the re-review turn carries it, because the alternative is a
    # brief citing CI evidence whose currency was never established.
    if (
        evaluation.base_tip_at_verification is None
        or current.ci_merge_base_sha != evaluation.base_tip_at_verification
    ):
        reasons.append(
            f"authoritative CI for {current.head_sha} tested it merged onto "
            f"{current.ci_merge_base_sha}, which is not the current "
            f"{current.base_ref} tip "
            f"({evaluation.base_tip_at_verification or 'unknown'}); the CI evidence "
            "the brief would cite is stale"
        )

    return tuple(reasons), current


def run_decision(
    *,
    client,
    reader,
    request: DecisionRequest,
    expected_author: str,
    writer=None,
    dry_run: bool = False,
) -> DecisionResult:
    """Produce one Merge Decision Brief for the chain in ``request``."""
    if not dry_run and writer is None:
        raise ValueError("a writer is required unless the brief is a dry run")

    def result(outcome: DecisionOutcome, reasons, **kwargs) -> DecisionResult:
        kwargs.setdefault("dry_run", dry_run)
        return DecisionResult(
            outcome=outcome, reasons=tuple(reasons), request=request, **kwargs
        )

    recorded = request.recorded_target

    # 1. Re-read the pull request. Nothing the documents claim about it is
    #    taken on trust, including the facts an earlier turn verified: they
    #    were current when that turn ran, which is not the question here.
    evaluation = verify_pull_request(client, recorded.number)
    if evaluation.verdict is Verdict.API_ERROR:
        return result(
            DecisionOutcome.API_ERROR,
            ("the pull request could not be verified",) + evaluation.reasons,
            evaluation=evaluation,
        )

    not_current, current = _currency_reasons(request, evaluation)
    if current is None and not not_current:
        # Unreachable: every branch above that withholds a current target also
        # gives a reason. Normalised rather than asserted, so that a future
        # branch which forgets one fails closed instead of classifying a state
        # this turn never established.
        not_current = (
            "the pull request's current verified state could not be established",
        )

    # 2. Gather the facts and classify. Both happen even when the evidence is
    #    stale: the classification is then EVIDENCE_NOT_CURRENT by the first
    #    rule in `classify`, and the finding facts are still worth reporting
    #    as what the re-review said -- clearly labelled as historical.
    facts = gather_facts(
        request.original_findings,
        request.rereview,
        evidence_not_current_reasons=not_current,
    )
    classification = classify(facts)

    if classification.next_action is NextAction.EVIDENCE_NOT_CURRENT:
        return result(
            DecisionOutcome.EVIDENCE_NOT_CURRENT,
            (
                "the re-review is no longer authoritative for this pull request's "
                "current state; no merge decision brief was produced",
            )
            + not_current,
            evaluation=evaluation,
            facts=facts,
            classification=classification,
            brief=comment_format.render_stale_brief(request, facts),
        )

    # Past this point the evidence is current, which means `_currency_reasons`
    # gave no reason and therefore returned a freshly verified target.
    body = comment_format.render_merge_brief(current, request, facts, classification)
    if len(body) > MAX_COMMENT_CHARS:
        # Not a classification failure: the evidence is fine and the artifact
        # is unpostable. Reported as a write failure so the exit code says a
        # brief exists that GitHub will not take, rather than implying the
        # pull request is in some state it is not.
        return result(
            DecisionOutcome.GITHUB_WRITE_FAILED,
            (
                f"the rendered brief is {len(body)} characters, above GitHub's "
                f"{MAX_COMMENT_CHARS}-character comment limit",
            ),
            evaluation=evaluation,
            current_target=current,
            facts=facts,
            classification=classification,
            brief=body,
        )

    identity = comment_format.merge_brief_identity_for(
        current, request.rereview.round
    )

    # 3. The duplicate check. One check, immediately before the write, unlike
    #    the review turns' two: they run an expensive reviewer in between and
    #    check early to avoid paying for a result they may not post, while
    #    nothing here happens between this read and the POST. It is therefore
    #    the check that matters -- the one catching a retry whose earlier POST
    #    succeeded and whose response was lost.
    try:
        already = comment_format.find_record(
            reader.list_comments(current.number),
            identity,
            expected_author=expected_author,
        )
    except GitHubApiError as exc:
        return result(
            DecisionOutcome.API_ERROR,
            (f"could not read existing comments: {exc}",),
            evaluation=evaluation,
            current_target=current,
            facts=facts,
            classification=classification,
            brief=body,
        )
    if already is not None:
        return result(
            DecisionOutcome.COMMENT_ALREADY_EXISTS,
            (
                f"comment {already} already records a merge decision brief for "
                f"{current.head_sha} merged onto {current.ci_merge_base_sha}; "
                "nothing was written",
            ),
            evaluation=evaluation,
            current_target=current,
            facts=facts,
            classification=classification,
            brief=body,
            existing_comment_id=already,
        )

    if dry_run:
        return result(
            DecisionOutcome.BRIEF_RECORDED,
            ("dry run: the brief is current and would be recorded",),
            evaluation=evaluation,
            current_target=current,
            facts=facts,
            classification=classification,
            brief=body,
        )

    try:
        comment_id = writer.create_comment(current.number, body)
    except GitHubApiError as exc:
        return result(
            DecisionOutcome.GITHUB_WRITE_FAILED,
            (
                f"{exc}. If this was a lost response rather than a rejected request, "
                "re-running finds the record by its marker and will not duplicate it.",
            ),
            evaluation=evaluation,
            current_target=current,
            facts=facts,
            classification=classification,
            brief=body,
        )

    return result(
        DecisionOutcome.BRIEF_RECORDED,
        (f"recorded as comment {comment_id}",),
        evaluation=evaluation,
        current_target=current,
        facts=facts,
        classification=classification,
        brief=body,
        comment_id=comment_id,
        github_write_performed=True,
    )
