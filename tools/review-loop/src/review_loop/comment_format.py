"""Render a validated verdict as the comment that gets recorded, and identify
the records this automation has already written.

Two separate jobs, kept together because they are two halves of one format.

**Rendering** reads only validated fields. The reviewer's raw output never
reaches GitHub: whatever prose, reasoning or instruction-shaped text it wrote
around its verdict block is dropped, so the recorded comment contains exactly
the fields the contract admits and nothing a reviewer could smuggle past it.

**Identity** is the machine marker plus the author, and never the heading.
``## Independent AI Review`` is a convention this repository's humans already
use by hand -- PR #26, #27 and #28 all carry one written by a person -- so
treating the heading as proof of an automation record would let a human
comment suppress a real review.

The marker alone is not proof either. It is a public, deterministic string
that anyone who can comment is able to reproduce; it identifies *which*
review a record would be, not *who* wrote it. The runner therefore accepts a
marker as a record only from the account it would post as -- see
:func:`find_record` below. That is a provenance
check, not a signature: it does not defend against the account itself, which
is the same-identity residual risk this project accepts deliberately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import require_full_sha
from .verdict import Recommendation, ReviewVerdict, Severity
from .review_target import ReviewTarget

HEADING = "## Independent AI Review"
RE_REVIEW_HEADING = "## Independent AI Re-Review"

_MARKER_PREFIX = "local-agent-concierge:independent-review"
_MARKER_VERSION = "v1"

#: The role this record was written in. It exists so a later re-review round,
#: or a Bounded Fix Response, is a different identity rather than an
#: overwrite of this one.
REVIEWER_ROLE = "independent-reviewer"

#: The role a fresh Independent Re-Review record is written in. Together with
#: the round it makes a re-review a different identity from the review it
#: follows, even though both describe the same pull request -- and it is what
#: keeps a re-review comment from being mistaken for a second initial review.
RE_REVIEWER_ROLE = "independent-re-reviewer"

_MARKER_PATTERN = re.compile(
    r"<!--\s*"
    + re.escape(f"{_MARKER_PREFIX}:{_MARKER_VERSION}")
    + r"\s+repo=(?P<repo>\S+)"
    r"\s+pr=(?P<pr>\d+)"
    r"\s+head=(?P<head>[0-9a-f]{40})"
    r"\s+base=(?P<base>[0-9a-f]{40})"
    r"\s+round=(?P<round>\d+)"
    r"\s+role=(?P<role>[a-z-]+)"
    r"\s*-->"
)


@dataclass(frozen=True)
class RecordIdentity:
    """What makes one recorded review distinct from every other.

    Deliberately not "one review per pull request": the same pull request with
    a new head, or a later round, is a different record. Re-review has to be
    able to add evidence without erasing what came before.

    ``base_sha`` is here for the same reason the rest of this package treats
    the review target as a merge context rather than a commit. Without it, a
    review of ``H`` merged onto ``B1`` would suppress a review of the very
    same ``H`` merged onto ``B2`` -- and the second is a different integration
    state, verified by different CI, that no reviewer has looked at. The
    post-review revalidation already refuses to conflate the two; identity has
    to agree with it, or the duplicate check quietly reintroduces exactly the
    stale-evidence case revalidation exists to prevent.
    """

    repo: str
    number: int
    head_sha: str
    base_sha: str
    round: int
    role: str = REVIEWER_ROLE

    def __post_init__(self) -> None:
        require_full_sha(self.head_sha, label="record identity head sha")
        require_full_sha(self.base_sha, label="record identity base sha")


def identity_for(target: ReviewTarget, verdict: ReviewVerdict) -> RecordIdentity:
    return RecordIdentity(
        repo=target.repo,
        number=target.number,
        head_sha=verdict.reviewed_head_sha,
        base_sha=target.ci_merge_base_sha,
        round=verdict.round,
    )


def marker(identity: RecordIdentity) -> str:
    """The hidden line that identifies one recorded review.

    It carries identity only -- no secret, no reviewer prompt, and no copy of
    the verdict, which is visible in the comment body anyway.
    """
    return (
        f"<!-- {_MARKER_PREFIX}:{_MARKER_VERSION} repo={identity.repo} "
        f"pr={identity.number} head={identity.head_sha} "
        f"base={identity.base_sha} round={identity.round} "
        f"role={identity.role} -->"
    )


def parse_markers(body: str) -> tuple[RecordIdentity, ...]:
    """Return every automation identity found in a comment body."""
    found: list[RecordIdentity] = []
    for match in _MARKER_PATTERN.finditer(body or ""):
        found.append(
            RecordIdentity(
                repo=match.group("repo"),
                number=int(match.group("pr")),
                head_sha=match.group("head"),
                base_sha=match.group("base"),
                round=int(match.group("round")),
                role=match.group("role"),
            )
        )
    return tuple(found)


def body_records(body: str, identity: RecordIdentity) -> bool:
    """Whether a comment body is already a record of this exact identity."""
    return identity in parse_markers(body)


def find_record(comments, identity: RecordIdentity, *, expected_author: str) -> int | None:
    """Return the id of a comment already recording this identity, if any.

    A record is a matching marker **written by the account the runner would
    post as**. Neither half is sufficient on its own.

    The heading is not identity: ``## Independent AI Review`` is how every
    review in this repository has been written by hand, so a human comment
    must not suppress a real review.

    The marker is not provenance either. Its format is public and
    deterministic, so anyone who can comment on the pull request can reproduce
    it -- and a marker copied into someone else's comment would otherwise make
    a runner report a validated review that was never produced, without even
    starting a reviewer. Checking the author is not a signature and does not
    defend against the account itself; it distinguishes this automation's own
    record from everyone else's text, which is the distinction the duplicate
    check actually needs.

    Shared by the review and re-review turns rather than written twice: the
    two records differ by round and role inside ``identity``, and a second
    copy of this rule is a second place for it to drift.
    """
    for comment in comments:
        if not body_records(comment.body, identity):
            continue
        if comment.author.casefold() != expected_author.casefold():
            continue
        return comment.comment_id
    return None


def _render_finding(finding) -> list[str]:
    lines = [
        "",
        f"### {finding.severity.value} — {finding.finding_id}",
        "",
        f"Finding ID: {finding.finding_id}",
        f"Severity: {finding.severity.value}",
        f"Location: {finding.location}",
        f"Problem: {finding.problem}",
        f"Evidence: {finding.evidence}",
        f"Required outcome: {finding.required_outcome}",
    ]
    if finding.scope_boundary:
        lines.append(f"Scope boundary: {finding.scope_boundary}")
    return lines


def render(target: ReviewTarget, verdict: ReviewVerdict) -> str:
    """Render the comment body for one validated review of one exact target."""
    evidence = (
        ", ".join(
            f"{path} (run {run_id}: {conclusion})"
            for path, run_id, conclusion in target.ci_evidence
        )
        or "(none recorded)"
    )

    lines = [
        HEADING,
        "",
        f"Round: {verdict.round}",
        f"Reviewed head SHA: {verdict.reviewed_head_sha}",
        f"CI integration base: {target.base_ref} at {target.ci_merge_base_sha}",
        f"CI verification: READY — {evidence}",
        f"Recommendation: {verdict.recommendation.value}",
        "",
        f"Blocking: {verdict.count(Severity.BLOCKING)}",
        f"Major: {verdict.count(Severity.MAJOR)}",
        f"Minor: {verdict.count(Severity.MINOR)}",
        f"Open findings: {len(verdict.open_findings)}",
    ]

    if verdict.escalation_reason:
        lines += ["", f"Escalation reason: {verdict.escalation_reason}"]

    if not verdict.open_findings:
        note = {
            Recommendation.APPROVED: (
                "The reviewer found nothing to change at this exact commit."
            ),
            Recommendation.ESCALATE: (
                "The reviewer could not complete a review of this commit; see the "
                "escalation reason above."
            ),
        }.get(verdict.recommendation)
        if note:
            lines += ["", note]
    else:
        lines.append("")
        lines.append("Findings:")
        for finding in verdict.open_findings:
            lines += _render_finding(finding)

    lines += [
        "",
        "---",
        "",
        "Recorded automatically by `review-loop review`. The verdict above was "
        "produced by an independent reviewer, validated against this exact head "
        "SHA, and re-checked against the pull request's current CI and merge "
        "context immediately before this comment was written.",
        "",
        marker(identity_for(target, verdict)),
    ]
    return "\n".join(lines) + "\n"


def rereview_identity_for(target: ReviewTarget, rereview) -> RecordIdentity:
    """The identity of one recorded Independent AI Re-Review.

    Same shape as :func:`identity_for`, with two fields carrying the whole
    difference: the head is the *pushed fix* commit and the role is
    :data:`RE_REVIEWER_ROLE`. So a re-review can never overwrite or be
    mistaken for the round-1 review of the commit it followed, and -- because
    ``base_sha`` is still the merge base CI verified -- a re-review of the
    same pushed commit against a *different* merge context is a different
    record, which is the case a duplicate check must not suppress.
    """
    return RecordIdentity(
        repo=target.repo,
        number=target.number,
        head_sha=rereview.reviewed_head_sha,
        base_sha=target.ci_merge_base_sha,
        round=rereview.round,
        role=RE_REVIEWER_ROLE,
    )


def _render_resolution(resolution) -> list[str]:
    lines = [
        "",
        f"### {resolution.resolution.value} — {resolution.finding_id}",
        "",
        f"Finding ID: {resolution.finding_id}",
        f"Resolution: {resolution.resolution.value}",
        f"Evidence: {resolution.evidence}",
    ]
    if resolution.reason:
        lines.append(f"Reason: {resolution.reason}")
    return lines


def _id_list(ids) -> str:
    return ", ".join(ids) or "(none)"


def render_rereview(target: ReviewTarget, request, rereview) -> str:
    """Render the comment body for one validated re-review of one pushed fix.

    The layout is deliberately two sections that never share a number. The
    counts above them are counts *of one collection each* -- resolutions by
    resolution, fresh findings by severity -- and there is no combined total
    anywhere, because there is no fact a combined total would state. A reader
    who wants "is this pull request done?" has to read both, which is the
    correct amount of work for that question.
    """
    from .rereview import Resolution

    evidence = (
        ", ".join(
            f"{path} (run {run_id}: {conclusion})"
            for path, run_id, conclusion in target.ci_evidence
        )
        or "(none recorded)"
    )
    resolved = rereview.resolutions_with(Resolution.RESOLVED)

    lines = [
        RE_REVIEW_HEADING,
        "",
        f"Round: {rereview.round}",
        f"Reviewed head SHA: {rereview.reviewed_head_sha}",
        f"Fix for: round {request.original_round} review of "
        f"{request.original_head_sha}",
        f"CI integration base: {target.base_ref} at {target.ci_merge_base_sha}",
        f"CI verification: READY — {evidence}",
        f"Recommendation: {rereview.recommendation.value}",
        "",
        "These are two independent facts. Resolution describes what became of "
        "the round "
        f"{request.original_round} findings; the fresh review describes this "
        "pull request as it now stands. Neither implies the other.",
        "",
        f"Original findings: {len(rereview.resolutions)}",
        f"RESOLVED: {_id_list(r.finding_id for r in resolved)}",
        f"UNRESOLVED: {_id_list(rereview.unresolved_finding_ids)}",
        "ESCALATE: "
        + _id_list(
            r.finding_id for r in rereview.resolutions_with(Resolution.ESCALATE)
        ),
        "",
        f"Fresh Blocking: {rereview.count(Severity.BLOCKING)}",
        f"Fresh Major: {rereview.count(Severity.MAJOR)}",
        f"Fresh Minor: {rereview.count(Severity.MINOR)}",
        f"Fresh findings: {len(rereview.fresh_findings)}",
    ]

    if rereview.escalation_reason:
        lines += ["", f"Escalation reason: {rereview.escalation_reason}"]

    lines += ["", f"Original finding resolutions (round {request.original_round}):"]
    for resolution in rereview.resolutions:
        lines += _render_resolution(resolution)

    lines += ["", "Fresh findings:"]
    if not rereview.fresh_findings:
        lines += [
            "",
            f"The re-reviewer found nothing new at {rereview.reviewed_head_sha}.",
        ]
    else:
        for finding in rereview.fresh_findings:
            lines += _render_finding(finding)

    lines += [
        "",
        "---",
        "",
        "Recorded automatically by `review-loop re-review`. The re-review above "
        "was produced by a fresh independent reviewer with no access to the "
        "Coding Agent's context, validated against this exact pushed fix SHA, "
        "and re-checked against the pull request's current CI and merge context "
        "immediately before this comment was written. It is evidence, not an "
        "approval: whether an unresolved finding gets another fix, whether a "
        "fresh finding is accepted, and whether anything merges all remain a "
        "human's decision.",
        "",
        marker(rereview_identity_for(target, rereview)),
    ]
    return "\n".join(lines) + "\n"


MERGE_BRIEF_HEADING = "## Merge Decision Brief"

#: The role a Merge Decision Brief is recorded in. A third role rather than a
#: second round: the brief describes the same commit and the same round as the
#: re-review it is derived from, and is a different artifact about it.
DECISION_ROLE = "merge-decision-brief"

#: How much of a validated finding's prose the brief reproduces. The brief is
#: a decision surface, not a second copy of the evidence: the full text is in
#: the review and re-review comments this one names, and a human who needs it
#: goes there. Every string truncated here has already been through
#: :func:`review_loop.verdict_validation.check_text`, so it cannot contain the
#: marker substrings whatever the reviewer wrote.
MAX_BRIEF_EXCERPT_CHARS = 200


def merge_brief_identity_for(target: ReviewTarget, round_number: int) -> RecordIdentity:
    """The identity of one recorded Merge Decision Brief.

    Head and base are the pull request's **current** verified state rather
    than anything a document claimed, because that is what the brief asserts
    about. Keeping ``base_sha`` in the identity is what makes the regression
    this stage most needs to avoid impossible: the same head against an
    advanced base is a different merge context, verified by different CI, and
    a brief about the old one must not suppress a brief about the new one.
    """
    return RecordIdentity(
        repo=target.repo,
        number=target.number,
        head_sha=target.head_sha,
        base_sha=target.ci_merge_base_sha,
        round=round_number,
        role=DECISION_ROLE,
    )


def _excerpt(text: str | None) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_BRIEF_EXCERPT_CHARS:
        return collapsed
    return collapsed[: MAX_BRIEF_EXCERPT_CHARS - 1].rstrip() + "…"


def _brief_identity_lines(
    target: ReviewTarget, request, rereview_comment_id: int | None
) -> list[str]:
    """The pull request state and the evidence chain, as the brief states them."""
    evidence = (
        ", ".join(
            f"{path} (run {run_id}: {conclusion})"
            for path, run_id, conclusion in target.ci_evidence
        )
        or "(none recorded)"
    )
    return [
        f"Repository: {target.repo}",
        f"Pull request: #{target.number}",
        f"Head SHA: {target.head_sha}",
        f"Base: {target.base_ref} at {target.ci_merge_base_sha}",
        f"Authoritative CI: READY — {evidence}",
        "",
        "Evidence chain:",
        "",
        f"Original review: round {request.original_round} of "
        f"{request.original_head_sha}",
        f"Review identity: {request.source_review_sha256}",
        f"Original recommendation: {request.original_recommendation.value}",
        f"Fix commit: {request.pushed_fix_sha} (parent {request.original_head_sha})",
        f"Re-review: round {request.rereview.round} of "
        f"{request.rereview.reviewed_head_sha}",
        f"Re-review recommendation: {request.rereview.recommendation.value}",
        # The comment this brief rests on, as found on the pull request by the
        # turn that wrote this line -- not as claimed by the document it read.
        # A brief whose record could not be confirmed is never produced, so
        # this is always a comment a reader can open.
        "Re-review record: comment "
        + (str(rereview_comment_id) if rereview_comment_id is not None else "(unconfirmed)"),
    ]


def _resolution_summary_lines(request) -> list[str]:
    """One line per original finding: which, how severe, and what became of it.

    Severity comes from the round-1 record and the resolution from round 2,
    joined only for display. The line never states a severity for a finding
    the re-review resolved, because "RESOLVED" is not a severity and a reader
    scanning a column of them must not be able to read one as the other.
    """
    from .rereview import Resolution

    severities = {f.finding_id: f.severity.value for f in request.original_findings}
    lines: list[str] = []
    for resolution in request.rereview.resolutions:
        severity = severities.get(resolution.finding_id, "(unknown severity)")
        line = f"{resolution.finding_id} — {severity} — {resolution.resolution.value}"
        if resolution.resolution is not Resolution.RESOLVED:
            excerpt = _excerpt(resolution.reason)
            if excerpt:
                line += f" — {excerpt}"
        lines.append(line)
    return lines or ["(none)"]


def _fresh_summary_lines(request) -> list[str]:
    return [
        f"{finding.finding_id} — {finding.severity.value} — {_excerpt(finding.problem)}"
        for finding in request.rereview.fresh_findings
    ] or ["(none)"]


def render_merge_brief(
    target: ReviewTarget,
    request,
    facts,
    classification,
    *,
    rereview_comment_id: int | None = None,
) -> str:
    """Render one Merge Decision Brief for a verified, current pull request state.

    Only ever called with evidence the runner has just re-established against
    GitHub. The stale case has a rendering of its own,
    :func:`render_stale_brief`, and it carries no marker -- so there is no code
    path on which a brief that is not current can be recorded as one that is.

    Original and fresh findings are two sections that never share a count.
    ``F1 — Major — RESOLVED`` above ``R2.F1 — Major — …`` says two true things
    at once: the fix worked, and the pull request still needs one. A single
    "Major findings: 1" would say neither.
    """
    lines = [
        MERGE_BRIEF_HEADING,
        "",
        f"Round: {request.rereview.round}",
        *_brief_identity_lines(target, request, rereview_comment_id),
        "",
        f"Merge context: current — authoritative CI tested this head merged onto "
        f"{target.ci_merge_base_sha}, which is still the {target.base_ref} tip",
        "",
        f"Original finding resolutions (round {request.original_round}):",
        "",
        *_resolution_summary_lines(request),
        "",
        f"Fresh findings (round {request.rereview.round}):",
        "",
        *_fresh_summary_lines(request),
        "",
        f"Unresolved original findings: {_id_list(facts.unresolved_original_finding_ids)}",
        f"Escalated original findings: {_id_list(facts.escalated_original_finding_ids)}",
        f"Fresh Blocking: {_id_list(facts.fresh_blocking_finding_ids)}",
        f"Fresh Major: {_id_list(facts.fresh_major_finding_ids)}",
        f"Fresh Minor: {_id_list(facts.fresh_minor_finding_ids)}",
        "Escalation: "
        + (
            facts.escalation_reason
            if facts.escalation_reason
            else ("requested" if facts.escalated_original_finding_ids
                  or facts.fresh_blocking_finding_ids else "none")
        ),
        "",
        f"Next action: {classification.next_action.value}",
        "",
    ]
    lines += [f"- {reason}" for reason in classification.reasons]
    lines += [
        "",
        "Human decision required: merge / do not merge / request another fix / "
        "escalate",
        "",
        "---",
        "",
        "Recorded automatically by `review-loop merge-brief`. Every fact above was "
        "re-derived from the review, fix, push and re-review artifacts and "
        "re-verified against this pull request's current head, base, merge context "
        "and authoritative CI immediately before this comment was written. The "
        "next action is a mechanical classification of that evidence, **not an "
        "approval and not a merge**: nothing here merges anything, starts another "
        "fix, or invokes a Coding Agent. Merging, declining, requesting another "
        "fix, accepting or deferring a Minor finding, and escalating all remain a "
        "human's decision.",
        "",
        marker(merge_brief_identity_for(target, request.rereview.round)),
    ]
    return "\n".join(lines) + "\n"


def render_stale_brief(request, facts) -> str:
    """Render the diagnostic a stale chain gets instead of a decision brief.

    Deliberately not a Merge Decision Brief with a bad classification inside
    it. It states what the evidence *was* about and why that is no longer the
    pull request, and it carries no marker, so it can be printed for an
    operator without ever becoming a record that a later run would find and
    treat as this state's decision.
    """
    target = request.recorded_target
    lines = [
        f"{MERGE_BRIEF_HEADING} — not produced: evidence is not current",
        "",
        f"Repository: {target.repo}",
        f"Pull request: #{target.number}",
        f"Re-reviewed head SHA: {target.head_sha}",
        f"Re-reviewed base: {target.base_ref} at {target.ci_merge_base_sha}",
        f"Review identity: {request.source_review_sha256}",
        "",
        "Why this is not decision-ready:",
        "",
    ]
    lines += [f"- {reason}" for reason in facts.evidence_not_current_reasons]
    lines += [
        "",
        "No brief was recorded. The re-review above is still true about the commit "
        "and merge context it read; it is no longer evidence about the pull "
        "request's present state, and a decision made from it would be a decision "
        "about something nobody is looking at. Re-run the loop from the stage the "
        "change invalidated.",
    ]
    return "\n".join(lines) + "\n"
