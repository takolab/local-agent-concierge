"""Rebuild the whole evidence chain from the three documents that recorded it.

A merge decision brief is a claim about a chain:

```text
initial review -> review_sha256 -> validated fix -> exact pushed commit
    -> authoritative CI -> fresh re-review -> the pull request right now
```

Every link in it was established by an earlier turn, and every one of those
turns wrote a ``--json`` document. The temptation is to read the last document
and believe the rest, because the last document *says* the rest happened. This
module does not, for the reason the whole pipeline exists: a serialised claim
that a check passed is not the check.

So all three documents are required, and the chain is rebuilt from scratch:

* :func:`review_loop.rereview_input.load_request` re-runs the review-to-push
  pairing in full -- the fix commit's parent is the reviewed head, its diff
  hashes to the candidate patch the fix turn validated, the push wrote one
  ref, and the review it fixed is the review supplied here, compared by a
  ``review_sha256`` **recomputed** from that document rather than read out of
  the push document that recorded it.
* The re-review document is then checked to be the re-review *of that push*,
  and its content is re-validated through
  :func:`review_loop.rereview_validation.validate` -- the same function that
  admitted it in the first place, reached by rebuilding the parser's own
  intermediate form from the JSON. Not a second implementation of the rules:
  the rules.

What that buys is worth stating exactly. It means a brief cannot be produced
from a re-review of a different commit, a re-review whose resolutions do not
answer this review's findings, a re-review that was never valid, or a
re-review that was only ever a dry run. It does not mean the documents are
authentic -- they are operator-controlled files, as every handoff here is, and
someone who can write all three can make them agree. What they cannot do is
make the *pull request* agree: the runner re-reads head, base, merge context,
CI **and the re-review's own record** from GitHub before any of this is
allowed to become a brief.

**Why a document with no re-review payload is refused rather than
rehydrated.** The re-review turn's early duplicate exit reports
``COMMENT_ALREADY_EXISTS`` before a reviewer runs, so its document proves a
record exists without saying what the record says. Recovering the missing
model by parsing the rendered ``## Independent AI Re-Review`` comment back
into a :class:`~review_loop.rereview.ReReview` would be the one thing
:mod:`review_loop.routing` refuses in its opening paragraph: deriving a
verdict a second time from a source that was never a verdict, using rules
that could drift from the ones that validated it. It would also make a
comment *rendering* -- lossy by design, and free to change for human
readability -- into a machine contract that no longer could.

The goal behind that idea is still met, and by a stronger route. What
``COMMENT_ALREADY_EXISTS`` ought to identify is *the exact validated evidence
downstream relies on*, and identity is precisely what this pipeline already
computes: :func:`review_loop.comment_format.rereview_identity_for` names one
re-review by pull request, pushed head, merge base, round and role. So the
runner looks that record up on the pull request and confirms it, while the
document supplies the model. The label is trusted for nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .decision import DecisionInputError
from .model import FULL_SHA_PATTERN
from .rereview import (
    RE_REVIEW_ROUND,
    ReReview,
    ReReviewParseError,
    ReReviewValidationError,
)
from .rereview_input import ReReviewInputError, ReReviewRequest
from .rereview_input import load_request as load_rereview_request
from .rereview_parser import RawBlock, RawReReview
from .rereview_validation import validate as validate_rereview
from .review_identity import review_sha256 as compute_review_sha256
from .review_target import ReviewTarget
from .routing import RoutingInputError
from .routing import load_handoff as load_review_handoff
from .verdict import Finding, Recommendation

#: The re-review outcomes whose document *can* carry a validated re-review of
#: a comment that is on the pull request.
#:
#: "Can" is the operative word, and it is why the outcome is a filter here
#: rather than the evidence. A label describes what one past run believed;
#: this stage needs two facts, and takes neither from it:
#:
#: 1. **the re-review model**, which must be in the document and must
#:    re-validate -- checked in :func:`_rereview` below, and
#: 2. **that it is recorded**, which is a fact about the pull request right
#:    now, and is therefore read back from GitHub by
#:    :mod:`review_loop.decision_runner` rather than inferred from any of
#:    these strings.
#:
#: ``GITHUB_WRITE_FAILED`` is in the set for exactly that reason. It is the
#: lost-response document: the ``POST`` was issued, the response did not come
#: back, and that run could not tell a rejected request from a comment it
#: really created. It carries the full validated re-review, so whether its
#: write landed is a question this stage can simply go and answer -- and if it
#: did not, the record check refuses the brief, which is the same fail-closed
#: answer by a route that does not depend on guessing.
#:
#: Two ``COMMENT_ALREADY_EXISTS`` paths exist in the re-review runner and they
#: are not equivalent. The pre-write duplicate check has a validated
#: re-review in hand and reports it; the early exit, which runs *before* the
#: reviewer to avoid paying for a result it may not post, has nothing but the
#: comment id. The second is refused below, by name.
BRIEFABLE_RE_REVIEW_OUTCOMES = frozenset(
    {"RE_REVIEW_VALID", "COMMENT_ALREADY_EXISTS", "GITHUB_WRITE_FAILED"}
)


@dataclass(frozen=True)
class DecisionRequest:
    """One merge-brief turn's inputs, every link of the chain re-established.

    ``recorded_target`` is the state the *re-review* verified, and it is what
    the current pull request is checked against. It is deliberately not the
    push turn's target: the base branch may advance between the push and the
    re-review, CI may re-run green against the new merge, and in that case the
    commit a fresh reviewer read was integrated against a different base than
    the one the push recorded. The re-review is the later and more specific
    evidence, so it is the one a brief is bound to.
    """

    #: The review-and-push chain, re-validated in full.
    chain: ReReviewRequest
    #: The identity of the review the whole chain answers, recomputed.
    source_review_sha256: str
    #: What the round-1 reviewer recommended. Always ``changes_requested``
    #: -- the re-review input admits no other -- and carried so the brief can
    #: state it rather than assert it.
    original_recommendation: Recommendation
    #: The merge context the re-review turn verified for the pushed fix.
    recorded_target: ReviewTarget
    #: The validated re-review itself, re-admitted through its own validator.
    rereview: ReReview
    #: Which re-review outcome the document reported.
    rereview_outcome: str
    #: The comment the re-review was recorded as, when the document names one.
    rereview_comment_id: int | None = None

    @property
    def original_findings(self) -> tuple[Finding, ...]:
        return self.chain.original_findings

    @property
    def original_head_sha(self) -> str:
        return self.chain.original_head_sha

    @property
    def original_round(self) -> int:
        return self.chain.original_round

    @property
    def pushed_fix_sha(self) -> str:
        return self.chain.pushed_fix_sha


def _require(payload: dict, key: str, *, where: str):
    if key not in payload or payload[key] is None:
        raise DecisionInputError(f"{where} is missing the required field {key!r}")
    return payload[key]


def _object(payload: dict, key: str, *, where: str) -> dict:
    value = _require(payload, key, where=where)
    if not isinstance(value, dict):
        raise DecisionInputError(f"{where}'s {key!r} is not an object")
    return value


def _sha(value: object, key: str, *, where: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_PATTERN.match(value):
        raise DecisionInputError(
            f"{where}: {key!r} must be an exact 40-character lowercase hex SHA, "
            f"got {value!r}"
        )
    return value


def _string(payload: dict, key: str, *, where: str) -> str:
    value = _require(payload, key, where=where)
    if not isinstance(value, str) or not value.strip():
        raise DecisionInputError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, key: str, *, where: str) -> str | None:
    """A field the contract allows to be absent, but not to be nonsense."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecisionInputError(f"{where}: {key!r} is not a string")
    return value


def _block(payload: object, labels: dict[str, str], *, where: str) -> RawBlock:
    """Rebuild one parsed block from its serialised form.

    ``labels`` maps the JSON key to the contract label the validator reads, so
    the re-review's own rules can be applied to a document without a second
    transcription of what those rules require. A key the contract treats as
    optional is simply absent when the document omits it, which is exactly
    what the parser would have produced from output that did not write it.
    """
    if not isinstance(payload, dict):
        raise DecisionInputError(f"{where} is not an object")
    fields: dict[str, str] = {}
    for key, label in labels.items():
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise DecisionInputError(f"{where}: {key!r} is not a string")
        fields[label] = value
    return RawBlock(fields=fields)


_RESOLUTION_LABELS = {
    "finding_id": "Finding ID",
    "resolution": "Resolution",
    "evidence": "Evidence",
    "reason": "Reason",
}

_FRESH_LABELS = {
    "finding_id": "Fresh finding ID",
    "severity": "Severity",
    "location": "Location",
    "problem": "Problem",
    "evidence": "Evidence",
    "required_outcome": "Required outcome",
    "scope_boundary": "Scope boundary",
}


def _list(payload: dict, key: str, *, where: str) -> list:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise DecisionInputError(f"{where}'s {key!r} is not a list")
    return value


def _rereview(payload: dict, *, pushed_fix_sha: str, original_ids: tuple[str, ...]):
    """Re-admit the serialised re-review through its own validator."""
    if payload.get("rereview") is None:
        # The re-review runner's early duplicate exit reaches this: it returns
        # COMMENT_ALREADY_EXISTS before starting a reviewer, so it never holds
        # a re-review model to serialise. The outcome is honest and the
        # document is simply not evidence about what the re-review *said*.
        #
        # Named specifically rather than falling through to "missing field
        # 'rereview'", because the operator's next step depends on which
        # document they are holding, and the generic message points at
        # neither. Rehydrating it by parsing the recorded comment back into a
        # verdict is deliberately not done -- see this module's note on why.
        raise DecisionInputError(
            f"the re-review input reports {payload.get('outcome')!r} with no "
            "'re-review' payload, so it establishes only that a record exists and "
            "not what that record says. This is the re-review turn's early "
            "duplicate exit, which returns before a reviewer runs. Brief from the "
            "document of the run that produced the re-review: its outcome is "
            "RE_REVIEW_VALID, or GITHUB_WRITE_FAILED if its write response was "
            "lost, and either carries the validated re-review this needs"
        )
    block = _object(payload, "rereview", where="the re-review input")

    envelope: dict[str, str] = {
        "Round": str(_require(block, "round", where="the re-review input's rereview")),
        "Reviewed head SHA": _string(
            block, "reviewed_head_sha", where="the re-review input's rereview"
        ),
        "Recommendation": _string(
            block, "recommendation", where="the re-review input's rereview"
        ),
    }
    escalation_reason = _optional_text(
        block.get("escalation_reason"),
        "escalation_reason",
        where="the re-review input's rereview",
    )
    if escalation_reason is not None:
        envelope["Escalation reason"] = escalation_reason

    raw = RawReReview(
        envelope=envelope,
        resolutions=[
            _block(
                entry,
                _RESOLUTION_LABELS,
                where=f"the re-review input's resolution {index}",
            )
            for index, entry in enumerate(
                _list(block, "resolutions", where="the re-review input's rereview"),
                start=1,
            )
        ],
        fresh=[
            _block(
                entry,
                _FRESH_LABELS,
                where=f"the re-review input's fresh finding {index}",
            )
            for index, entry in enumerate(
                _list(block, "fresh_findings", where="the re-review input's rereview"),
                start=1,
            )
        ],
    )

    try:
        return validate_rereview(
            raw,
            target_head_sha=pushed_fix_sha,
            original_finding_ids=original_ids,
        )
    except (ReReviewParseError, ReReviewValidationError) as exc:
        # Re-raised in this stage's error type so a caller catching input
        # errors catches all of them, exactly as the re-review validator
        # translates the verdict contract's errors into its own.
        raise DecisionInputError(f"the re-review input is not admissible: {exc}") from exc


def _recorded_target(
    payload: dict, *, chain: ReReviewRequest, pushed_fix_sha: str
) -> ReviewTarget:
    """The merge context the re-review turn itself verified and re-verified."""
    where = "the re-review input's target"
    target = _object(payload, "target", where="the re-review input")

    repo = _string(target, "repo", where=where)
    if repo != chain.target.repo:
        raise DecisionInputError(
            f"{where} names {repo}, but the review and push it is paired with "
            f"describe {chain.target.repo}"
        )
    number = target.get("number")
    if number != chain.target.number:
        raise DecisionInputError(
            f"{where} names pull request #{number!r}, but the review and push it is "
            f"paired with describe #{chain.target.number}"
        )

    head = _sha(_require(target, "head_sha", where=where), "head_sha", where=where)
    if head != pushed_fix_sha:
        raise DecisionInputError(
            f"{where} was verified at {head}, not the pushed fix {pushed_fix_sha}"
        )

    base_ref = _string(target, "base_ref", where=where)
    if base_ref != chain.target.base_ref:
        raise DecisionInputError(
            f"{where} names base branch {base_ref!r}, but the push it is paired with "
            f"pushed against {chain.target.base_ref!r}"
        )

    evidence: list[tuple[str, int, str]] = []
    for index, entry in enumerate(_list(target, "ci_evidence", where=where), start=1):
        if not isinstance(entry, dict):
            raise DecisionInputError(f"{where}: CI evidence {index} is not an object")
        path = entry.get("workflow_path")
        run_id = entry.get("run_id")
        conclusion = entry.get("conclusion")
        if (
            not isinstance(path, str)
            or not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or not isinstance(conclusion, str)
        ):
            raise DecisionInputError(
                f"{where}: CI evidence {index} does not name a workflow path, run id "
                "and conclusion"
            )
        evidence.append((path, run_id, conclusion))

    return ReviewTarget(
        repo=repo,
        number=int(number),
        head_sha=head,
        base_ref=base_ref,
        ci_merge_base_sha=_sha(
            _require(target, "ci_merge_base_sha", where=where),
            "ci_merge_base_sha",
            where=where,
        ),
        ci_evidence=tuple(evidence),
    )


def _check_request_block(payload: dict, *, chain: ReReviewRequest) -> None:
    """Require the re-review to have been run for *this* review and push.

    The re-review document records which inputs it was given, and those are
    re-derived here rather than believed: a document whose ``request`` block
    names other findings, another original head or another round is the
    re-review of a different chain, and a brief built from it would report one
    fix's evidence as though it answered another's findings.
    """
    where = "the re-review input's request"
    request = _object(payload, "request", where="the re-review input")

    if request.get("repo") != chain.target.repo or request.get("number") != chain.target.number:
        raise DecisionInputError(
            f"{where} names {request.get('repo')!r} #{request.get('number')!r}, not "
            f"the {chain.target.repo} #{chain.target.number} the review and push "
            "describe"
        )

    pushed = _sha(
        _require(request, "pushed_fix_sha", where=where), "pushed_fix_sha", where=where
    )
    if pushed != chain.pushed_fix_sha:
        raise DecisionInputError(
            f"{where} re-reviewed the fix {pushed}, but the push it is paired with "
            f"pushed {chain.pushed_fix_sha}"
        )

    original_head = _sha(
        _require(request, "original_head_sha", where=where),
        "original_head_sha",
        where=where,
    )
    if original_head != chain.original_head_sha:
        raise DecisionInputError(
            f"{where} re-reviewed a fix for a review of {original_head}, not of "
            f"{chain.original_head_sha}"
        )

    if request.get("original_round") != chain.original_round:
        raise DecisionInputError(
            f"{where} names original round {request.get('original_round')!r}, but the "
            f"review it is paired with is round {chain.original_round}"
        )

    ids = request.get("original_finding_ids")
    if not isinstance(ids, list) or not all(isinstance(entry, str) for entry in ids):
        raise DecisionInputError(f"{where}'s 'original_finding_ids' is not a list of ids")
    if tuple(ids) != chain.original_finding_ids:
        raise DecisionInputError(
            f"{where} re-reviewed the findings "
            + (", ".join(ids) or "(none)")
            + ", but the review it is paired with raised "
            + (", ".join(chain.original_finding_ids) or "(none)")
        )


def load_request(
    review_document: str,
    push_document: str,
    rereview_document: str,
    *,
    expected_repo: str | None = None,
) -> DecisionRequest:
    """Read the three turn documents back as one re-established evidence chain."""
    try:
        chain = load_rereview_request(
            review_document, push_document, expected_repo=expected_repo
        )
    except ReReviewInputError as exc:
        raise DecisionInputError(
            f"the review and push inputs are not usable: {exc}"
        ) from exc

    # The same loader the chain used, for the two facts the chain does not
    # carry forward. Reading it twice rather than widening ReReviewRequest
    # keeps one implementation of "is this a validated review?".
    try:
        review = load_review_handoff(review_document, expected_repo=expected_repo)
    except RoutingInputError as exc:  # pragma: no cover - load_request got here first
        raise DecisionInputError(f"the review input is not usable: {exc}") from exc
    source_review_sha256 = compute_review_sha256(review.target, review.verdict)

    try:
        payload = json.loads(rereview_document)
    except json.JSONDecodeError as exc:
        raise DecisionInputError(f"the re-review input is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DecisionInputError("the re-review input is not a JSON object")

    outcome = _require(payload, "outcome", where="the re-review input")
    if outcome not in BRIEFABLE_RE_REVIEW_OUTCOMES:
        raise DecisionInputError(
            f"the re-review input reports outcome {outcome!r}; only "
            + " and ".join(sorted(BRIEFABLE_RE_REVIEW_OUTCOMES))
            + " mean a validated re-review of this pushed fix exists"
        )
    if payload.get("dry_run", False) is not False:
        raise DecisionInputError(
            "the re-review input is a dry run, so the re-review it describes was "
            "never recorded on the pull request; a brief citing it would point at "
            "evidence a human cannot read"
        )
    if payload.get("round") != RE_REVIEW_ROUND:
        raise DecisionInputError(
            f"the re-review input reports round {payload.get('round')!r}; this runner "
            f"briefs only the first re-review (round {RE_REVIEW_ROUND})"
        )

    _check_request_block(payload, chain=chain)
    recorded_target = _recorded_target(
        payload, chain=chain, pushed_fix_sha=chain.pushed_fix_sha
    )
    rereview = _rereview(
        payload,
        pushed_fix_sha=chain.pushed_fix_sha,
        original_ids=chain.original_finding_ids,
    )

    comment_id = payload.get("comment_id")
    if comment_id is None:
        comment_id = payload.get("existing_comment_id")
    if comment_id is not None and (
        not isinstance(comment_id, int) or isinstance(comment_id, bool)
    ):
        raise DecisionInputError(
            f"the re-review input's comment id {comment_id!r} is not an integer"
        )

    return DecisionRequest(
        chain=chain,
        source_review_sha256=source_review_sha256,
        original_recommendation=review.verdict.recommendation,
        recorded_target=recorded_target,
        rereview=rereview,
        rereview_outcome=outcome,
        rereview_comment_id=comment_id,
    )
