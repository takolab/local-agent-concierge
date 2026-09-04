"""Decide whether parsed reviewer output is an admissible Structured Verdict.

Everything here fails closed. A rejected verdict costs one wasted reviewer
run; an accepted bad one puts a review record on a pull request that claims
more than the reviewer actually established.

Two rules are worth naming, because they are not obvious:

* **Any ``Reviewed head SHA`` that is not exactly the target SHA is a binding
  failure**, including an abbreviated or malformed one. A short SHA is not a
  vaguer way of saying the same commit -- it is a value this runner refuses
  to resolve, because resolving it is exactly the guess the contract exists
  to forbid.
* **A ``Blocking`` finding requires ``escalate``.** That is this project's
  standing review-automation decision: Blocking findings go to a human, never
  into a bounded fix. A verdict that pairs one with ``changes_requested`` is
  therefore not a verdict this pipeline knows how to act on.
"""

from __future__ import annotations

import re

from .model import FULL_SHA_PATTERN
from .verdict import (
    MAX_FIELD_CHARS,
    MAX_FINDING_ID_CHARS,
    MAX_FINDINGS,
    MAX_LOCATION_CHARS,
    SUPPORTED_ROUND,
    Finding,
    Recommendation,
    ReviewVerdict,
    Severity,
    ShaBindingError,
    VerdictValidationError,
)
from .verdict_parser import RawFinding, RawVerdict

_REQUIRED_FINDING_FIELDS = (
    "Finding ID",
    "Severity",
    "Location",
    "Problem",
    "Evidence",
    "Required outcome",
)

#: A finding id ends up inside a rendered comment and is quoted in later
#: rounds, so it is restricted to a plain token rather than arbitrary text.
_FINDING_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

#: Substrings that would let reviewer text forge or disturb the machine
#: marker the idempotency check reads. Rejected rather than escaped: a
#: reviewer has no legitimate reason to write either one.
_FORBIDDEN_IN_FIELDS = ("<!--", "-->", "local-agent-concierge:independent-review")

_NO_RESOLVED_VALUES = frozenset({"", "-", "none", "(none)", "n/a"})


def _normalise_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _require(raw: dict[str, str], label: str, *, where: str) -> str:
    value = raw.get(label)
    if value is None:
        raise VerdictValidationError(f"{where} is missing the required field {label!r}")
    if not value.strip():
        raise VerdictValidationError(f"{where} has an empty {label!r}")
    return value.strip()


def _check_text(value: str, label: str, *, where: str, limit: int) -> str:
    if len(value) > limit:
        raise VerdictValidationError(
            f"{where}: {label!r} is {len(value)} characters, above the {limit} limit"
        )
    for forbidden in _FORBIDDEN_IN_FIELDS:
        if forbidden in value:
            raise VerdictValidationError(
                f"{where}: {label!r} contains {forbidden!r}, which is not allowed in "
                "reviewer text because it would disturb the record's machine marker"
            )
    return value


def _validate_finding(raw: RawFinding, index: int) -> Finding:
    where = f"finding {index}"
    for label in _REQUIRED_FINDING_FIELDS:
        _require(raw.fields, label, where=where)

    finding_id = raw.fields["Finding ID"].strip()
    if not _FINDING_ID_PATTERN.match(finding_id):
        raise VerdictValidationError(
            f"{where}: {finding_id!r} is not a usable finding id (letters, digits, "
            f"'_', '-' and '.', at most {MAX_FINDING_ID_CHARS} characters)"
        )

    severity_text = raw.fields["Severity"].strip()
    severity = {s.value.lower(): s for s in Severity}.get(severity_text.lower())
    if severity is None:
        raise VerdictValidationError(
            f"{where}: unknown severity {severity_text!r}; the contract admits only "
            + ", ".join(s.value for s in Severity)
        )

    location = _check_text(
        raw.fields["Location"].strip(), "Location", where=where, limit=MAX_LOCATION_CHARS
    )
    texts = {}
    for label in ("Problem", "Evidence", "Required outcome"):
        texts[label] = _check_text(
            raw.fields[label].strip(), label, where=where, limit=MAX_FIELD_CHARS
        )

    scope_boundary = raw.fields.get("Scope boundary", "").strip() or None
    if scope_boundary is not None:
        scope_boundary = _check_text(
            scope_boundary, "Scope boundary", where=where, limit=MAX_FIELD_CHARS
        )

    return Finding(
        finding_id=finding_id,
        severity=severity,
        location=location,
        problem=texts["Problem"],
        evidence=texts["Evidence"],
        required_outcome=texts["Required outcome"],
        scope_boundary=scope_boundary,
    )


def _validate_recommendation(
    recommendation: Recommendation,
    findings: tuple[Finding, ...],
    escalation_reason: str | None,
) -> None:
    """Reject a recommendation that its own finding set does not support."""
    blocking = [f for f in findings if f.severity is Severity.BLOCKING]

    if recommendation is Recommendation.APPROVED and findings:
        raise VerdictValidationError(
            f"the verdict recommends 'approved' while reporting {len(findings)} open "
            "finding(s); an approval with findings is not a state this pipeline records"
        )
    if recommendation is Recommendation.CHANGES_REQUESTED and not findings:
        raise VerdictValidationError(
            "the verdict recommends 'changes_requested' but reports no open finding, "
            "so there is nothing to change"
        )
    if recommendation is Recommendation.CHANGES_REQUESTED and blocking:
        raise VerdictValidationError(
            "the verdict recommends 'changes_requested' but reports a Blocking "
            f"finding ({blocking[0].finding_id}); a Blocking finding always escalates"
        )
    if recommendation is Recommendation.ESCALATE and not findings and not escalation_reason:
        raise VerdictValidationError(
            "the verdict recommends 'escalate' but gives neither an open finding nor "
            "an 'Escalation reason', so what is being escalated is unstated"
        )


def validate(raw: RawVerdict, *, target_head_sha: str) -> ReviewVerdict:
    """Validate a parsed verdict against the exact commit that was reviewed."""
    reviewed = raw.envelope.get("Reviewed head SHA")
    if reviewed is None:
        raise VerdictValidationError(
            "the verdict is missing the required field 'Reviewed head SHA'"
        )
    reviewed = reviewed.strip()
    if not FULL_SHA_PATTERN.match(reviewed) or reviewed != target_head_sha:
        raise ShaBindingError(
            f"the verdict reports reviewing {reviewed!r}, which is not the exact "
            f"40-character head SHA under review ({target_head_sha})"
        )

    round_text = _require(raw.envelope, "Round", where="the verdict")
    try:
        round_number = int(round_text)
    except ValueError:
        raise VerdictValidationError(
            f"the verdict's Round is {round_text!r}, which is not a number"
        ) from None
    if round_number != SUPPORTED_ROUND:
        raise VerdictValidationError(
            f"the verdict reports round {round_number}; this runner implements only "
            f"the initial review (round {SUPPORTED_ROUND})"
        )

    recommendation_text = _require(raw.envelope, "Recommendation", where="the verdict")
    recommendation = {r.value: r for r in Recommendation}.get(
        _normalise_token(recommendation_text)
    )
    if recommendation is None:
        raise VerdictValidationError(
            f"unknown recommendation {recommendation_text!r}; the contract admits only "
            + ", ".join(r.value for r in Recommendation)
        )

    resolved = raw.envelope.get("Resolved", "").strip()
    if resolved.lower() not in _NO_RESOLVED_VALUES:
        raise VerdictValidationError(
            f"the verdict reports resolved findings ({resolved!r}) in round "
            f"{SUPPORTED_ROUND}, which has no earlier round to resolve anything from"
        )

    if len(raw.findings) > MAX_FINDINGS:
        raise VerdictValidationError(
            f"the verdict reports {len(raw.findings)} findings, above the "
            f"{MAX_FINDINGS} this runner will record in one comment"
        )

    findings = tuple(
        _validate_finding(entry, index) for index, entry in enumerate(raw.findings, start=1)
    )

    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            raise VerdictValidationError(
                f"finding id {finding.finding_id!r} appears more than once; ids must be "
                "unique within one verdict"
            )
        seen.add(finding.finding_id)

    escalation_reason = raw.envelope.get("Escalation reason", "").strip() or None
    if escalation_reason is not None:
        escalation_reason = _check_text(
            escalation_reason, "Escalation reason", where="the verdict", limit=MAX_FIELD_CHARS
        )

    _validate_recommendation(recommendation, findings, escalation_reason)

    return ReviewVerdict(
        round=round_number,
        reviewed_head_sha=reviewed,
        recommendation=recommendation,
        open_findings=findings,
        resolved_finding_ids=(),
        escalation_reason=escalation_reason,
    )
