"""Carry a validated review from the review turn to the fix turn.

The fix turn's input is the review turn's own ``--json`` output. That choice
needs defending, because it looks like the thing this design refuses --
re-parsing text to recover a decision that was already made.

It is not, and the difference is what is being read. Loosely scraping a
GitHub comment, or re-parsing the reviewer's raw output, would mean deriving
a verdict a second time from a source that was never a verdict, using rules
that could drift from the ones that validated it. What happens here is that
a machine-generated serialisation of the *already validated* model is read
back through **the same invariants that produced it**: full 40-character
SHAs, the closed severity and recommendation vocabularies, the finding-id
pattern, the field limits, the round. Nothing is accepted because it looks
plausible; a handoff that would not have been a valid verdict is not a valid
handoff.

The alternative -- having ``review-loop fix`` run its own review turn --
would pay for a second reviewer run and produce a second verdict that might
not be the one a human read. Passing the reviewed verdict forward is both
cheaper and more honest about what is being fixed.

Two consequences are worth stating plainly:

* **A handoff file is operator-controlled input.** Anyone who can write it
  can choose which findings get routed. That is not a new authority: they
  could equally run the fix command with different arguments. What they
  cannot do is route a fix against a commit that is not the pull request's
  head, because :class:`review_loop.reviewer_workspace.PreparedWorkspace`
  resolves ``refs/pull/N/head`` from the remote and refuses anything else.
  Git, not the file, decides which commit gets fixed.
* **The handoff is not persistent state.** It is one file the operator pipes
  from one command into the next, exactly as they would pipe any other CLI
  output. Nothing reads it later, nothing accumulates, and deleting it loses
  nothing that GitHub does not already hold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .model import FULL_SHA_PATTERN
from .review_target import ReviewTarget
from .verdict import (
    MAX_FIELD_CHARS,
    MAX_FINDINGS,
    MAX_LOCATION_CHARS,
    SUPPORTED_ROUND,
    Finding,
    Recommendation,
    ReviewVerdict,
    Severity,
)
from .verdict_validation import _FINDING_ID_PATTERN

#: The review outcome a handoff must report. Every other outcome means the
#: review did not end in a validated verdict recorded against this target,
#: and a fix routed from one would be a fix for a review that did not happen.
ROUTABLE_REVIEW_OUTCOMES = frozenset({"REVIEW_VALID", "COMMENT_ALREADY_EXISTS"})


class RoutingInputError(ValueError):
    """The routing input is not a validated review this runner produced."""


@dataclass(frozen=True)
class ReviewHandoff:
    """One validated review, carried across a process boundary."""

    target: ReviewTarget
    verdict: ReviewVerdict
    review_outcome: str


def _require(payload: dict, key: str, *, where: str):
    if key not in payload or payload[key] is None:
        raise RoutingInputError(f"{where} is missing the required field {key!r}")
    return payload[key]


def _text(value: object, key: str, *, where: str, limit: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RoutingInputError(f"{where}: {key!r} must be a non-empty string")
    if len(value) > limit:
        raise RoutingInputError(
            f"{where}: {key!r} is {len(value)} characters, above the {limit} limit"
        )
    return value.strip()


def _sha(value: object, key: str, *, where: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_PATTERN.match(value):
        raise RoutingInputError(
            f"{where}: {key!r} must be an exact 40-character lowercase hex SHA, "
            f"got {value!r}"
        )
    return value


def _finding(payload: object, index: int) -> Finding:
    where = f"open finding {index}"
    if not isinstance(payload, dict):
        raise RoutingInputError(f"{where} is not an object")

    finding_id = _text(
        _require(payload, "finding_id", where=where), "finding_id", where=where
    )
    if not _FINDING_ID_PATTERN.match(finding_id):
        raise RoutingInputError(
            f"{where}: {finding_id!r} is not a usable finding id"
        )

    severity_text = _require(payload, "severity", where=where)
    severity = {s.value: s for s in Severity}.get(
        severity_text if isinstance(severity_text, str) else ""
    )
    if severity is None:
        raise RoutingInputError(
            f"{where}: unknown severity {severity_text!r}; the contract admits only "
            + ", ".join(s.value for s in Severity)
        )

    scope_boundary = payload.get("scope_boundary")
    if scope_boundary is not None:
        scope_boundary = _text(scope_boundary, "scope_boundary", where=where)

    return Finding(
        finding_id=finding_id,
        severity=severity,
        location=_text(
            _require(payload, "location", where=where),
            "location",
            where=where,
            limit=MAX_LOCATION_CHARS,
        ),
        problem=_text(_require(payload, "problem", where=where), "problem", where=where),
        evidence=_text(
            _require(payload, "evidence", where=where), "evidence", where=where
        ),
        required_outcome=_text(
            _require(payload, "required_outcome", where=where),
            "required_outcome",
            where=where,
        ),
        scope_boundary=scope_boundary,
    )


def load_handoff(document: str, *, expected_repo: str | None = None) -> ReviewHandoff:
    """Read a ``review-loop review --json`` document as a validated review."""
    try:
        payload = json.loads(document)
    except json.JSONDecodeError as exc:
        raise RoutingInputError(f"the routing input is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RoutingInputError("the routing input is not a JSON object")

    outcome = _require(payload, "outcome", where="the routing input")
    if outcome not in ROUTABLE_REVIEW_OUTCOMES:
        raise RoutingInputError(
            f"the routing input reports review outcome {outcome!r}; only "
            + " and ".join(sorted(ROUTABLE_REVIEW_OUTCOMES))
            + " carry a validated verdict to route"
        )

    raw_target = _require(payload, "target", where="the routing input")
    if not isinstance(raw_target, dict):
        raise RoutingInputError("the routing input's 'target' is not an object")
    raw_verdict = _require(payload, "verdict", where="the routing input")
    if not isinstance(raw_verdict, dict):
        raise RoutingInputError("the routing input's 'verdict' is not an object")

    repo = _text(_require(raw_target, "repo", where="the target"), "repo", where="the target")
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise RoutingInputError(
            f"the target's repository {repo!r} is not in owner/name form"
        )
    if expected_repo is not None and repo != expected_repo:
        raise RoutingInputError(
            f"the routing input describes {repo}, but --repo says {expected_repo}"
        )

    number = _require(raw_target, "number", where="the target")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise RoutingInputError(
            f"the target's pull request number {number!r} is not a positive integer"
        )

    target = ReviewTarget(
        repo=repo,
        number=number,
        head_sha=_sha(
            _require(raw_target, "head_sha", where="the target"),
            "head_sha",
            where="the target",
        ),
        base_ref=_text(
            _require(raw_target, "base_ref", where="the target"),
            "base_ref",
            where="the target",
        ),
        ci_merge_base_sha=_sha(
            _require(raw_target, "ci_merge_base_sha", where="the target"),
            "ci_merge_base_sha",
            where="the target",
        ),
    )

    round_number = _require(raw_verdict, "round", where="the verdict")
    if round_number != SUPPORTED_ROUND:
        raise RoutingInputError(
            f"the routing input reports round {round_number!r}; this runner routes "
            f"only the initial review (round {SUPPORTED_ROUND})"
        )

    reviewed = _sha(
        _require(raw_verdict, "reviewed_head_sha", where="the verdict"),
        "reviewed_head_sha",
        where="the verdict",
    )
    if reviewed != target.head_sha:
        raise RoutingInputError(
            f"the routing input's verdict reviewed {reviewed}, but its target is "
            f"{target.head_sha}; a review and the state it describes must be the "
            "same commit"
        )

    recommendation_text = _require(raw_verdict, "recommendation", where="the verdict")
    recommendation = {r.value: r for r in Recommendation}.get(
        recommendation_text if isinstance(recommendation_text, str) else ""
    )
    if recommendation is None:
        raise RoutingInputError(
            f"unknown recommendation {recommendation_text!r}; the contract admits "
            "only " + ", ".join(r.value for r in Recommendation)
        )

    raw_findings = raw_verdict.get("open_findings") or []
    if not isinstance(raw_findings, list):
        raise RoutingInputError("the verdict's 'open_findings' is not a list")
    if len(raw_findings) > MAX_FINDINGS:
        raise RoutingInputError(
            f"the verdict reports {len(raw_findings)} findings, above the "
            f"{MAX_FINDINGS} the review contract admits"
        )

    findings = tuple(
        _finding(entry, index) for index, entry in enumerate(raw_findings, start=1)
    )
    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            raise RoutingInputError(
                f"finding id {finding.finding_id!r} appears more than once; ids "
                "must be unique within one verdict"
            )
        seen.add(finding.finding_id)

    escalation_reason = raw_verdict.get("escalation_reason")
    if escalation_reason is not None:
        escalation_reason = _text(
            escalation_reason, "escalation_reason", where="the verdict"
        )

    # The same coherence rules the verdict validator applies. A handoff that
    # would not have been an admissible verdict is not an admissible handoff,
    # however it came to be written.
    if recommendation is Recommendation.APPROVED and findings:
        raise RoutingInputError(
            f"the routing input recommends 'approved' while reporting "
            f"{len(findings)} open finding(s)"
        )
    if recommendation is Recommendation.CHANGES_REQUESTED and not findings:
        raise RoutingInputError(
            "the routing input recommends 'changes_requested' but reports no open "
            "finding"
        )
    blocking = [f for f in findings if f.severity is Severity.BLOCKING]
    if recommendation is Recommendation.CHANGES_REQUESTED and blocking:
        raise RoutingInputError(
            "the routing input recommends 'changes_requested' but reports a "
            f"Blocking finding ({blocking[0].finding_id}); a Blocking finding "
            "always escalates"
        )

    return ReviewHandoff(
        target=target,
        verdict=ReviewVerdict(
            round=SUPPORTED_ROUND,
            reviewed_head_sha=reviewed,
            recommendation=recommendation,
            open_findings=findings,
            escalation_reason=escalation_reason,
        ),
        review_outcome=outcome,
    )
