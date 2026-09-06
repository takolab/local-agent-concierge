"""Decide whether parsed re-reviewer output is an admissible Bounded Re-Review.

Everything here fails closed, for the reason the initial review's validator
gives: a rejected re-review costs one wasted reviewer run, an accepted bad
one puts a record on a pull request claiming more than the reviewer
established -- and here the claim is specifically *"the thing a human asked
for got fixed"*, which is the one an operator is most likely to act on
without re-checking.

Four rules carry the weight:

* **The resolutions must be a bijection onto the original finding ids.**
  Every original id exactly once, no unknown id, none omitted. A re-review
  that silently drops F2 is not a re-review with a gap; it is a document that
  would let F2 disappear from the record entirely.
* **A fresh finding id cannot be an original finding id.** Enforced twice --
  by requiring this round's namespace prefix, and by checking the actual
  original ids -- because the prefix constrains only this round's reviewer,
  and a round-1 reviewer was never told to avoid the namespace.
* **``RESOLVED`` needs evidence; ``UNRESOLVED`` and ``ESCALATE`` need a
  reason as well.** "Fixed" without the code that shows it is an assertion,
  and "not fixed" without a reason gives the human deciding what to do next
  nothing to decide with.
* **The recommendation must be supported by both collections at once.** It
  is the one field that legitimately reads across the two, and it is
  therefore the one place they could be wrongly collapsed -- so the coherence
  rules name which fact each requirement comes from, and the collections
  themselves stay untouched.
"""

from __future__ import annotations

from .model import FULL_SHA_PATTERN
from .rereview import (
    FRESH_FINDING_PREFIX,
    RE_REVIEW_ROUND,
    FindingResolution,
    ReReview,
    ReReviewShaBindingError,
    ReReviewValidationError,
    Resolution,
)
from .rereview_parser import RawBlock, RawReReview
from .verdict import (
    MAX_FIELD_CHARS,
    MAX_FINDINGS,
    Finding,
    Recommendation,
    Severity,
    VerdictValidationError,
)
from .verdict_parser import RawFinding
from .verdict_validation import check_text, validate_finding


def _normalise_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _require(fields: dict[str, str], label: str, *, where: str) -> str:
    value = fields.get(label)
    if value is None:
        raise ReReviewValidationError(
            f"{where} is missing the required field {label!r}"
        )
    if not value.strip():
        raise ReReviewValidationError(f"{where} has an empty {label!r}")
    return value.strip()


def _bounded(value: str, label: str, *, where: str) -> str:
    """Apply the review contract's field limits, in this contract's error type.

    :func:`review_loop.verdict_validation.check_text` is the single
    implementation of "is this field text admissible?" -- the length bound and
    the refusal to let reviewer text contain the record marker's own
    substrings. It raises the *verdict* contract's error, so it is translated
    here: a caller catching this module's errors must catch all of them, and
    one that escaped would surface as an unhandled exception rather than as
    RE_REVIEW_MALFORMED.
    """
    try:
        return check_text(value, label, where=where, limit=MAX_FIELD_CHARS)
    except VerdictValidationError as exc:
        raise ReReviewValidationError(str(exc)) from exc


def _text(fields: dict[str, str], label: str, *, where: str) -> str:
    return _bounded(_require(fields, label, where=where), label, where=where)


def _validate_resolution(raw: RawBlock, index: int) -> FindingResolution:
    where = f"resolution {index}"

    finding_id = _require(raw.fields, "Finding ID", where=where)
    resolution_text = _require(raw.fields, "Resolution", where=where)
    resolution = {r.value: r for r in Resolution}.get(resolution_text.strip().upper())
    if resolution is None:
        raise ReReviewValidationError(
            f"{where}: unknown resolution {resolution_text!r}; the contract admits "
            "only " + ", ".join(r.value for r in Resolution)
        )

    evidence = _text(raw.fields, "Evidence", where=where)

    reason = raw.fields.get("Reason", "").strip() or None
    if reason is not None:
        reason = _bounded(reason, "Reason", where=where)
    if resolution is not Resolution.RESOLVED and reason is None:
        raise ReReviewValidationError(
            f"{where} reports {resolution.value} for {finding_id} without a "
            "'Reason'; an unresolved or escalated finding has to say what is still "
            "wrong or what the human is being asked"
        )

    return FindingResolution(
        finding_id=finding_id,
        resolution=resolution,
        evidence=evidence,
        reason=reason,
    )


def _validate_fresh(raw: RawBlock, index: int, original_ids: frozenset[str]) -> Finding:
    """Validate one fresh finding through the review contract's own rules.

    The block is handed to :func:`review_loop.verdict_validation.validate_finding`
    under the label it uses, so a fresh finding is admissible exactly when the
    same finding would have been admissible in a round-1 verdict -- same
    severity vocabulary, same field limits, same marker-forgery refusal, same
    id pattern.
    """
    fields = dict(raw.fields)
    finding_id = fields.pop("Fresh finding ID", "").strip()
    if not finding_id:
        raise ReReviewValidationError(
            f"fresh finding {index} is missing the required field 'Fresh finding ID'"
        )
    fields["Finding ID"] = finding_id

    try:
        finding = validate_finding(RawFinding(fields=fields), index)
    except VerdictValidationError as exc:
        # Reported as "fresh finding N: ..." so a reader can tell which of the
        # two collections the refusal came from, and re-raised in this
        # contract's error type for the reason :func:`_bounded` gives.
        raise ReReviewValidationError(f"fresh {exc}") from exc

    if not finding_id.startswith(FRESH_FINDING_PREFIX):
        raise ReReviewValidationError(
            f"fresh finding {index}: {finding_id!r} does not begin with "
            f"{FRESH_FINDING_PREFIX!r}; a finding raised in round {RE_REVIEW_ROUND} is "
            "namespaced by its round so it cannot be read as one of the originals"
        )
    if finding_id == FRESH_FINDING_PREFIX:
        raise ReReviewValidationError(
            f"fresh finding {index}: {finding_id!r} is the namespace prefix and "
            "nothing else, so it does not identify a finding"
        )
    if finding_id in original_ids:
        raise ReReviewValidationError(
            f"fresh finding {index}: {finding_id!r} is also an original finding id; a "
            "fresh finding must be distinguishable from the findings whose "
            "resolution this re-review reports"
        )
    return finding


def _validate_recommendation(
    recommendation: Recommendation,
    resolutions: tuple[FindingResolution, ...],
    fresh: tuple[Finding, ...],
    escalation_reason: str | None,
) -> None:
    """Reject a recommendation the two collections do not support.

    Each rule names which collection it reads, because this is the one field
    that legitimately depends on both and therefore the one place the
    evidence separation could quietly be lost.
    """
    outstanding = tuple(r for r in resolutions if r.resolution is not Resolution.RESOLVED)
    escalated = tuple(r for r in resolutions if r.resolution is Resolution.ESCALATE)
    blocking = tuple(f for f in fresh if f.severity is Severity.BLOCKING)

    if recommendation is Recommendation.APPROVED and outstanding:
        raise ReReviewValidationError(
            "the re-review recommends 'approved' while reporting "
            f"{len(outstanding)} original finding(s) that are not RESOLVED "
            f"({', '.join(r.finding_id for r in outstanding)})"
        )
    if recommendation is Recommendation.APPROVED and fresh:
        raise ReReviewValidationError(
            f"the re-review recommends 'approved' while reporting {len(fresh)} fresh "
            "finding(s); an approval with findings is not a state this pipeline records"
        )
    if recommendation is Recommendation.CHANGES_REQUESTED and not outstanding and not fresh:
        raise ReReviewValidationError(
            "the re-review recommends 'changes_requested' but every original finding "
            "is RESOLVED and it reports no fresh finding, so there is nothing to change"
        )
    if recommendation is Recommendation.CHANGES_REQUESTED and blocking:
        raise ReReviewValidationError(
            "the re-review recommends 'changes_requested' but reports a fresh "
            f"Blocking finding ({blocking[0].finding_id}); a Blocking finding always "
            "escalates"
        )
    if recommendation is Recommendation.CHANGES_REQUESTED and escalated:
        raise ReReviewValidationError(
            f"the re-review recommends 'changes_requested' but escalates "
            f"{escalated[0].finding_id}; an escalated finding is a question for a "
            "human, not a change to request"
        )
    if (
        recommendation is Recommendation.ESCALATE
        and not escalated
        and not blocking
        and not escalation_reason
    ):
        raise ReReviewValidationError(
            "the re-review recommends 'escalate' but gives no ESCALATE resolution, no "
            "fresh Blocking finding and no 'Escalation reason', so what is being "
            "escalated is unstated"
        )


def validate(
    raw: RawReReview,
    *,
    target_head_sha: str,
    original_finding_ids: tuple[str, ...],
) -> ReReview:
    """Validate a parsed re-review against the exact pushed fix commit."""
    reviewed = raw.envelope.get("Reviewed head SHA")
    if reviewed is None:
        raise ReReviewValidationError(
            "the re-review is missing the required field 'Reviewed head SHA'"
        )
    reviewed = reviewed.strip()
    if not FULL_SHA_PATTERN.match(reviewed) or reviewed != target_head_sha:
        raise ReReviewShaBindingError(
            f"the re-review reports reviewing {reviewed!r}, which is not the exact "
            f"40-character pushed fix SHA under re-review ({target_head_sha})"
        )

    round_text = _require(raw.envelope, "Round", where="the re-review")
    try:
        round_number = int(round_text)
    except ValueError:
        raise ReReviewValidationError(
            f"the re-review's Round is {round_text!r}, which is not a number"
        ) from None
    if round_number != RE_REVIEW_ROUND:
        raise ReReviewValidationError(
            f"the re-review reports round {round_number}; this runner implements only "
            f"the first re-review (round {RE_REVIEW_ROUND})"
        )

    recommendation_text = _require(raw.envelope, "Recommendation", where="the re-review")
    recommendation = {r.value: r for r in Recommendation}.get(
        _normalise_token(recommendation_text)
    )
    if recommendation is None:
        raise ReReviewValidationError(
            f"unknown recommendation {recommendation_text!r}; the contract admits only "
            + ", ".join(r.value for r in Recommendation)
        )

    escalation_reason = raw.envelope.get("Escalation reason", "").strip() or None
    if escalation_reason is not None:
        escalation_reason = _bounded(
            escalation_reason, "Escalation reason", where="the re-review"
        )

    resolutions = tuple(
        _validate_resolution(entry, index)
        for index, entry in enumerate(raw.resolutions, start=1)
    )

    expected = tuple(original_finding_ids)
    reported: list[str] = []
    for resolution in resolutions:
        if resolution.finding_id in reported:
            raise ReReviewValidationError(
                f"original finding {resolution.finding_id!r} is resolved more than "
                "once; each original finding gets exactly one resolution"
            )
        if resolution.finding_id not in expected:
            raise ReReviewValidationError(
                f"the re-review resolves {resolution.finding_id!r}, which is not one "
                "of the original findings ("
                + ", ".join(expected)
                + "); a finding this turn discovered is a fresh finding, not a "
                "resolution"
            )
        reported.append(resolution.finding_id)

    missing = [finding_id for finding_id in expected if finding_id not in reported]
    if missing:
        raise ReReviewValidationError(
            "the re-review reports no resolution for original finding(s) "
            + ", ".join(missing)
            + "; every original finding is answered or the record would lose it"
        )

    if len(raw.fresh) > MAX_FINDINGS:
        raise ReReviewValidationError(
            f"the re-review reports {len(raw.fresh)} fresh findings, above the "
            f"{MAX_FINDINGS} this runner will record in one comment"
        )

    originals = frozenset(expected)
    fresh = tuple(
        _validate_fresh(entry, index, originals)
        for index, entry in enumerate(raw.fresh, start=1)
    )
    seen: set[str] = set()
    for finding in fresh:
        if finding.finding_id in seen:
            raise ReReviewValidationError(
                f"fresh finding id {finding.finding_id!r} appears more than once; ids "
                "must be unique within one re-review"
            )
        seen.add(finding.finding_id)

    _validate_recommendation(recommendation, resolutions, fresh, escalation_reason)

    return ReReview(
        round=round_number,
        reviewed_head_sha=reviewed,
        recommendation=recommendation,
        resolutions=resolutions,
        fresh_findings=fresh,
        escalation_reason=escalation_reason,
    )
