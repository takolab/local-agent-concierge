"""Render a validated verdict as the comment that gets recorded, and identify
the records this automation has already written.

Two separate jobs, kept together because they are two halves of one format.

**Rendering** reads only validated fields. The reviewer's raw output never
reaches GitHub: whatever prose, reasoning or instruction-shaped text it wrote
around its verdict block is dropped, so the recorded comment contains exactly
the fields the contract admits and nothing a reviewer could smuggle past it.

**Identity** is the machine marker, not the heading. ``## Independent AI
Review`` is a convention this repository's humans already use by hand -- PR
#26, #27 and #28 all carry one written by a person. Treating the heading as
proof of an automation record would let a human comment suppress a real
review; treating the marker as proof cannot, because nothing else writes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import require_full_sha
from .verdict import Recommendation, ReviewVerdict, Severity
from .review_target import ReviewTarget

HEADING = "## Independent AI Review"

_MARKER_PREFIX = "local-agent-concierge:independent-review"
_MARKER_VERSION = "v1"

#: The role this record was written in. It exists so a later re-review round,
#: or a Bounded Fix Response, is a different identity rather than an
#: overwrite of this one.
REVIEWER_ROLE = "independent-reviewer"

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
