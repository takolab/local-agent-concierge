"""Bind a re-review turn to the exact pushed fix, from validated inputs only.

A re-review needs two facts that no single earlier stage holds:

* **what the original findings said** -- which lives in the review turn's own
  ``--json`` document, and
* **which commit is the fix** -- which lives in the push turn's.

So this module takes both, and its whole job is to refuse every pair that is
not *one review and the PUSH_READY push of that review's fix*. It follows
:mod:`review_loop.routing` exactly: a machine-generated serialisation of an
already validated model is read back through the same invariants that
produced it, never charitably interpreted. The review document is in fact
read by ``routing`` itself, so there is only one implementation of "is this a
validated review?" in the package.

What the pairing can and cannot establish is worth stating plainly, because
the difference is the honest limit of this stage:

* It **can** establish that the pushed commit sits in this pull request, that
  its parent is exactly the commit this review was written against, that
  authoritative CI verified it against the current merge context, and that
  the push wrote only the one ref it was authorised to.
* It **cannot** establish that the patch inside that commit actually
  addresses those findings. Nothing mechanical can. That is precisely the
  question the fresh Independent Re-Review is being run to answer, and
  pretending the handoff already answered it would make the re-review
  ceremonial.

As with every other handoff here, the documents are operator-controlled
input. Anyone who can write them can choose which review and which push are
paired -- but they cannot make a reviewer read a commit that is not the pull
request's head, because the runner re-derives that from GitHub and the
workspace resolves ``refs/pull/N/head`` from the remote. The files select;
git and GitHub decide.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .model import FULL_SHA_PATTERN
from .push_response import BOUNDARY_CLEAN
from .review_target import ReviewTarget
from .rereview import RE_REVIEW_ROUND
from .routing import RoutingInputError
from .routing import load_handoff as load_review_handoff
from .verdict import Finding, Recommendation

#: The only push outcome a re-review may start from. Every other value either
#: wrote nothing, wrote something whose CI is not evidence about the pull
#: request's present state, or left remote state unknown.
RE_REVIEWABLE_PUSH_OUTCOME = "PUSH_READY"


class ReReviewInputError(ValueError):
    """The inputs are not a validated review plus the push of its fix."""


@dataclass(frozen=True)
class ReReviewRequest:
    """One re-review turn's authoritative inputs, all runner-derived.

    ``target`` is the *fix* state: the same pull request, at the pushed fix
    commit, with the merge context the push turn verified. ``original_*``
    describes what is being re-evaluated, and is history -- it is never used
    to decide which commit gets read.
    """

    #: The pushed fix, as the push turn verified it. Refreshed against
    #: GitHub before a reviewer runs; this is the claim, not the evidence.
    target: ReviewTarget
    #: The commit the original review was written against, and the parent of
    #: the fix commit.
    original_head_sha: str
    #: The round the original findings belong to. Always the initial review
    #: in this slice; carried explicitly so the record can name it.
    original_round: int
    #: The findings whose resolution this turn evaluates, exactly as round 1
    #: validated them -- ids, severities and required outcomes included.
    original_findings: tuple[Finding, ...]
    #: The round this turn's own findings and record belong to.
    round: int = RE_REVIEW_ROUND

    @property
    def pushed_fix_sha(self) -> str:
        """The exact commit a reviewer must read. An alias, for readability."""
        return self.target.head_sha

    @property
    def original_finding_ids(self) -> tuple[str, ...]:
        return tuple(f.finding_id for f in self.original_findings)


def _require(payload: dict, key: str, *, where: str):
    if key not in payload or payload[key] is None:
        raise ReReviewInputError(f"{where} is missing the required field {key!r}")
    return payload[key]


def _sha(value: object, key: str, *, where: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_PATTERN.match(value):
        raise ReReviewInputError(
            f"{where}: {key!r} must be an exact 40-character lowercase hex SHA, "
            f"got {value!r}"
        )
    return value


def _object(payload: dict, key: str, *, where: str) -> dict:
    value = _require(payload, key, where=where)
    if not isinstance(value, dict):
        raise ReReviewInputError(f"{where}'s {key!r} is not an object")
    return value


def _true(payload: dict, key: str, *, where: str) -> None:
    if payload.get(key) is not True:
        raise ReReviewInputError(
            f"{where} reports {key}={payload.get(key)!r}; a re-review may only start "
            f"from a push that established {key}"
        )


def _false(payload: dict, key: str, *, where: str) -> None:
    if payload.get(key, False) is not False:
        raise ReReviewInputError(
            f"{where} reports {key}={payload.get(key)!r}, so it does not describe a "
            "fix that is really on the pull request branch"
        )


def _push_target(payload: dict, *, expected_repo: str | None) -> tuple[str, int]:
    """Read the repository and pull request the push document describes."""
    target = _object(payload, "target", where="the push input")
    repo = target.get("repo")
    if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
        raise ReReviewInputError(
            f"the push input's repository {repo!r} is not in owner/name form"
        )
    if expected_repo is not None and repo != expected_repo:
        raise ReReviewInputError(
            f"the push input describes {repo}, but --repo says {expected_repo}"
        )
    number = target.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ReReviewInputError(
            f"the push input's pull request number {number!r} is not a positive integer"
        )
    return repo, number


def _verified_target(payload: dict, *, repo: str, number: int, pushed_sha: str) -> ReviewTarget:
    """The merge context the push turn verified for the pushed commit."""
    verified = _object(payload, "verified_target", where="the push input")

    if verified.get("repo") != repo or verified.get("number") != number:
        raise ReReviewInputError(
            "the push input's verified target names "
            f"{verified.get('repo')!r} #{verified.get('number')!r}, not the "
            f"{repo} #{number} it says it pushed to"
        )

    head = _sha(
        _require(verified, "head_sha", where="the push input's verified target"),
        "head_sha",
        where="the push input's verified target",
    )
    if head != pushed_sha:
        raise ReReviewInputError(
            f"the push input reports pushing {pushed_sha} but verified {head}; a "
            "re-review target is the commit CI actually verified"
        )

    base_ref = verified.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ReReviewInputError(
            "the push input's verified target has no base branch"
        )

    return ReviewTarget(
        repo=repo,
        number=number,
        head_sha=head,
        base_ref=base_ref.strip(),
        ci_merge_base_sha=_sha(
            _require(verified, "ci_merge_base_sha", where="the push input's verified target"),
            "ci_merge_base_sha",
            where="the push input's verified target",
        ),
    )


def _check_ci(payload: dict, *, pushed_sha: str, merge_base_sha: str) -> None:
    """Re-read the push turn's own PUSH_READY CI invariants.

    Not decoration: this is the ``base advancement has not made the existing
    CI evidence stale`` requirement as the push turn recorded it. The runner
    re-establishes it live against GitHub as well -- but a document that never
    satisfied it in the first place is not a starting point, and refusing it
    here costs no API call.
    """
    ci = _object(payload, "ci", where="the push input")

    if ci.get("verdict") != "READY":
        raise ReReviewInputError(
            f"the push input reports CI {ci.get('verdict')!r} for the pushed fix; "
            "only READY is evidence a re-review may start from"
        )
    if ci.get("bound_to_pushed_commit") is not True:
        raise ReReviewInputError(
            "the push input's CI evidence is not bound to the pushed commit, so it "
            "says nothing about the commit a re-review would read"
        )
    if ci.get("head_sha") != pushed_sha:
        raise ReReviewInputError(
            f"the push input's CI describes head {ci.get('head_sha')!r}, not the "
            f"pushed fix {pushed_sha}"
        )
    if ci.get("ci_merge_base_sha") != merge_base_sha:
        raise ReReviewInputError(
            f"the push input's CI tested a merge onto {ci.get('ci_merge_base_sha')!r}, "
            f"but its verified target reports {merge_base_sha}"
        )
    if ci.get("base_tip_at_verification") != merge_base_sha:
        raise ReReviewInputError(
            f"the push input's CI tested a merge onto {merge_base_sha}, which was not "
            f"the base tip ({ci.get('base_tip_at_verification')!r}) when it was "
            "verified; that CI evidence was already stale"
        )


def _check_commit(payload: dict, *, pushed_sha: str, reviewed_head_sha: str) -> None:
    """Check the created commit, when this push document reports one.

    A push that found the fix already on the branch reports no commit, and
    that is a legitimate PUSH_READY. When there *is* one, its parent is the
    tightest mechanical link this pipeline has between a fix and the review it
    answers: the fix commit sits directly on the commit that was reviewed.
    """
    commit = payload.get("commit")
    if commit is None:
        return
    if not isinstance(commit, dict):
        raise ReReviewInputError("the push input's 'commit' is not an object")

    sha = _sha(_require(commit, "sha", where="the push input's commit"), "sha",
               where="the push input's commit")
    if sha != pushed_sha:
        raise ReReviewInputError(
            f"the push input created commit {sha} but reports pushing {pushed_sha}"
        )
    parent = _sha(
        _require(commit, "parent_sha", where="the push input's commit"),
        "parent_sha",
        where="the push input's commit",
    )
    if parent != reviewed_head_sha:
        raise ReReviewInputError(
            f"the fix commit's parent is {parent}, not the reviewed head "
            f"{reviewed_head_sha}; it is not a fix for this review"
        )


def load_request(
    review_document: str,
    push_document: str,
    *,
    expected_repo: str | None = None,
) -> ReReviewRequest:
    """Read one ``review --json`` and one ``push --json`` as a re-review target."""
    try:
        review = load_review_handoff(review_document, expected_repo=expected_repo)
    except RoutingInputError as exc:
        raise ReReviewInputError(f"the review input is not usable: {exc}") from exc

    # The round is not re-checked here: ``routing`` accepts only round 1, and
    # a second copy of that rule is a second place for it to drift.
    verdict = review.verdict
    if not verdict.open_findings:
        raise ReReviewInputError(
            "the review input reports no open finding, so there is no original "
            "finding whose resolution a re-review could evaluate"
        )
    if verdict.recommendation is not Recommendation.CHANGES_REQUESTED:
        raise ReReviewInputError(
            f"the review input recommends {verdict.recommendation.value!r}; only a "
            "review that requested changes is one a bounded fix and a re-review "
            "follow from"
        )

    try:
        payload = json.loads(push_document)
    except json.JSONDecodeError as exc:
        raise ReReviewInputError(f"the push input is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReReviewInputError("the push input is not a JSON object")

    outcome = _require(payload, "outcome", where="the push input")
    if outcome != RE_REVIEWABLE_PUSH_OUTCOME:
        raise ReReviewInputError(
            f"the push input reports push outcome {outcome!r}; only "
            f"{RE_REVIEWABLE_PUSH_OUTCOME} means the fix is on the branch with "
            "authoritative CI green against the current merge context"
        )
    _false(payload, "dry_run", where="the push input")
    _true(payload, "repository_mutated", where="the push input")

    boundary = payload.get("boundary_status")
    if boundary != BOUNDARY_CLEAN:
        raise ReReviewInputError(
            f"the push input reports boundary_status={boundary!r}; a re-review does "
            "not start from a push whose write boundary is exceeded or unproven"
        )

    repo, number = _push_target(payload, expected_repo=expected_repo)
    if repo != review.target.repo or number != review.target.number:
        raise ReReviewInputError(
            f"the push input describes {repo} #{number}, but the review it is paired "
            f"with describes {review.target.repo} #{review.target.number}"
        )

    reviewed_head = _sha(
        _require(_object(payload, "target", where="the push input"), "head_sha",
                 where="the push input's target"),
        "head_sha",
        where="the push input's target",
    )
    if reviewed_head != review.target.head_sha:
        raise ReReviewInputError(
            f"the push input fixes {reviewed_head}, but the review it is paired with "
            f"reviewed {review.target.head_sha}"
        )

    pushed_sha = _sha(
        _require(payload, "pushed_sha", where="the push input"),
        "pushed_sha",
        where="the push input",
    )
    if pushed_sha == reviewed_head:
        raise ReReviewInputError(
            f"the push input reports pushing {pushed_sha}, which is the reviewed head "
            "itself; there is no fix commit to re-review"
        )

    target = _verified_target(payload, repo=repo, number=number, pushed_sha=pushed_sha)
    _check_ci(payload, pushed_sha=pushed_sha, merge_base_sha=target.ci_merge_base_sha)
    _check_commit(payload, pushed_sha=pushed_sha, reviewed_head_sha=reviewed_head)

    return ReReviewRequest(
        target=target,
        original_head_sha=reviewed_head,
        original_round=verdict.round,
        original_findings=verdict.open_findings,
    )
