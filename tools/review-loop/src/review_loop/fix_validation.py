"""Decide whether a Coding Agent's answer is an admissible Structured Fix.

Everything here fails closed, and for a sharper reason than on the review
side. A rejected verdict costs one wasted reviewer run. A wrongly accepted
fix response is a claim that a finding is resolved, attached to a diff that a
later slice is meant to push -- so the cost of believing it is a change
nobody checked, applied to a commit nobody meant.

Validation happens in two passes, and the second is the one that matters.

**Identity.** Each response must name exactly one routed finding id and
exactly the 40-character commit the fix started from. Abbreviated is not
"close enough": it is a claim about a commit this runner refuses to resolve.
The set of responses must correspond one-to-one with the set of routed
findings -- no missing answer, no extra one, no repeat -- because a fix turn
that silently drops a finding looks exactly like a fix turn that handled it.

**Consistency with the working tree.** The response says which files it
changed; ``git status`` says which files changed. They must be the same set.
This is what makes the contract worth having: a response is not believed, it
is checked. A hidden edit the agent did not report, and a reported file the
agent did not touch, both fail -- the first because it is a change nobody
reviewed, the second because a response that misdescribes its own work
cannot be evidence for anything.

Two smaller rules follow from that:

* ``fixed`` requires at least one changed file **and** a verification claim.
  A no-code fix is not supported. If a finding turns out to need no change,
  that is not a fix -- it is a disagreement with the reviewer, and the
  contract has a word for it: ``escalate``.
* ``unable_to_fix`` and ``escalate`` require a reason and must leave nothing
  behind. Half-finished edits with no claim attached are the worst possible
  artifact to hand a human: they look like a fix and are not one.

What this module does **not** do is judge whether the fix is correct. It
cannot: the reviewer's ``Required outcome`` is prose, and a runner that
graded prose would be a second reviewer with none of the first one's
independence. Nor does it re-run the verification command the agent reports
-- that string comes from the agent, and executing it would hand an untrusted
process exactly the arbitrary-command channel the rest of this design refuses
it. Whether the fix is *right*, and whether it should be used at all, stay
with the human. This module establishes only that the fix is *the one that
was asked for, in the place it was allowed, and no more*.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from .agent_workspace import WorkspaceInspection
from .fix_request import FixRequest
from .fix_response import (
    MAX_FILES_CHANGED,
    MAX_FIX_FIELD_CHARS,
    MAX_PATH_CHARS,
    FixFindingBindingError,
    FixOutcome,
    FixResponse,
    FixResponseValidationError,
    FixTargetBindingError,
)
from .fix_response_parser import RawFixResponse, parse_files_changed
from .model import FULL_SHA_PATTERN

_REQUIRED_FIELDS = ("Finding ID", "Target head SHA", "Outcome", "Summary")

#: A control character or a leading separator in a reported path means the
#: value is not a repository path, whatever else it is.
_UNSAFE_PATH = re.compile(r"[\x00-\x1f\\]")


class ScopeViolation(FixResponseValidationError):
    """The working tree holds a change the routed request did not permit."""


@dataclass(frozen=True)
class ValidatedFix:
    """Every routed finding's answer, checked against the tree it changed."""

    responses: tuple[FixResponse, ...]
    inspection: WorkspaceInspection

    @property
    def outcomes(self) -> dict[str, FixOutcome]:
        return {r.finding_id: r.outcome for r in self.responses}

    def count(self, outcome: FixOutcome) -> int:
        return sum(1 for r in self.responses if r.outcome is outcome)


def _require(fields: dict[str, str], label: str, *, where: str) -> str:
    value = fields.get(label)
    if value is None:
        raise FixResponseValidationError(f"{where} is missing the required field {label!r}")
    if not value.strip():
        raise FixResponseValidationError(f"{where} has an empty {label!r}")
    return value.strip()


def _check_text(value: str, label: str, *, where: str) -> str:
    if len(value) > MAX_FIX_FIELD_CHARS:
        raise FixResponseValidationError(
            f"{where}: {label!r} is {len(value)} characters, above the "
            f"{MAX_FIX_FIELD_CHARS} limit"
        )
    return value


def _normalise_reported_path(value: str, *, where: str) -> str:
    """Read one ``Files changed`` entry as a repository-relative path."""
    text = value.strip().strip("`").strip()
    if not text:
        raise FixResponseValidationError(f"{where} reports an empty changed path")
    if len(text) > MAX_PATH_CHARS:
        raise FixResponseValidationError(
            f"{where} reports a changed path of {len(text)} characters, above the "
            f"{MAX_PATH_CHARS} limit"
        )
    if _UNSAFE_PATH.search(text):
        raise FixResponseValidationError(
            f"{where} reports a changed path containing a control character or a "
            "backslash, which is not a repository path"
        )
    if text.startswith("/") or text.startswith("~"):
        raise FixResponseValidationError(
            f"{where} reports {text!r} as an absolute path; changed files are "
            "reported relative to the repository root"
        )
    normalised = posixpath.normpath(text)
    if normalised == "." or ".." in normalised.split("/"):
        raise FixResponseValidationError(
            f"{where} reports {text!r}, which leaves the repository root"
        )
    return normalised


def validate_response(raw: RawFixResponse, *, index: int, target_head_sha: str) -> FixResponse:
    """Validate one response block against the commit the fix started from."""
    where = f"fix response {index}"
    for label in _REQUIRED_FIELDS:
        _require(raw.fields, label, where=where)

    reported_sha = raw.fields["Target head SHA"].strip()
    if not FULL_SHA_PATTERN.match(reported_sha) or reported_sha != target_head_sha:
        raise FixTargetBindingError(
            f"{where} reports fixing {reported_sha!r}, which is not the exact "
            f"40-character commit the fix started from ({target_head_sha})"
        )

    outcome_text = raw.fields["Outcome"].strip()
    outcome = {o.value: o for o in FixOutcome}.get(
        outcome_text.lower().replace(" ", "_").replace("-", "_")
    )
    if outcome is None:
        raise FixResponseValidationError(
            f"{where}: unknown outcome {outcome_text!r}; the contract admits only "
            + ", ".join(o.value for o in FixOutcome)
        )

    files = tuple(
        _normalise_reported_path(path, where=where)
        for path in parse_files_changed(raw.fields.get("Files changed", ""))
    )
    if len(files) > MAX_FILES_CHANGED:
        raise FixResponseValidationError(
            f"{where} reports {len(files)} changed files, above the "
            f"{MAX_FILES_CHANGED} a bounded fix may touch"
        )
    if len(set(files)) != len(files):
        duplicates = sorted({path for path in files if files.count(path) > 1})
        raise FixResponseValidationError(
            f"{where} lists {', '.join(duplicates)} more than once in 'Files changed'"
        )

    summary = _check_text(raw.fields["Summary"].strip(), "Summary", where=where)
    verification = raw.fields.get("Verification", "").strip() or None
    reason = raw.fields.get("Reason", "").strip() or None
    scope_notes = raw.fields.get("Scope notes", "").strip() or None
    for label, value in (
        ("Verification", verification),
        ("Reason", reason),
        ("Scope notes", scope_notes),
    ):
        if value is not None:
            _check_text(value, label, where=where)

    if outcome is FixOutcome.FIXED:
        if not files:
            raise FixResponseValidationError(
                f"{where} reports 'fixed' but lists no changed file. A fix with no "
                "code change is not supported: if the finding needs no change, the "
                "outcome is 'escalate'"
            )
        if verification is None:
            raise FixResponseValidationError(
                f"{where} reports 'fixed' without a 'Verification' field; a fix "
                "reported with no verification is an assertion"
            )
    else:
        if reason is None:
            raise FixResponseValidationError(
                f"{where} reports {outcome.value!r} without a 'Reason' field, so "
                "what stopped the fix is unstated"
            )
        if files:
            raise FixResponseValidationError(
                f"{where} reports {outcome.value!r} but lists "
                f"{len(files)} changed file(s); an outcome that is not a fix must "
                "leave the working tree as it was found"
            )

    return FixResponse(
        finding_id=raw.fields["Finding ID"].strip(),
        target_head_sha=reported_sha,
        outcome=outcome,
        summary=summary,
        files_changed=files,
        verification=verification,
        reason=reason,
        scope_notes=scope_notes,
    )


def check_identity(responses: tuple[FixResponse, ...], *, request: FixRequest) -> None:
    """Every routed finding is answered exactly once, and nothing else is."""
    routed = list(request.finding_ids)
    answered = [r.finding_id for r in responses]

    repeated = sorted({fid for fid in answered if answered.count(fid) > 1})
    if repeated:
        raise FixFindingBindingError(
            "the coding agent answered "
            + ", ".join(repeated)
            + " more than once; each routed finding is answered exactly once"
        )

    unknown = [fid for fid in answered if fid not in routed]
    if unknown:
        raise FixFindingBindingError(
            "the coding agent answered "
            + ", ".join(sorted(unknown))
            + ", which "
            + ("was" if len(unknown) == 1 else "were")
            + " not routed to it; the routed findings are "
            + ", ".join(routed)
        )

    missing = [fid for fid in routed if fid not in answered]
    if missing:
        raise FixFindingBindingError(
            "the coding agent did not answer "
            + ", ".join(missing)
            + "; every routed finding needs an outcome, including one it could "
            "not fix"
        )


def check_working_tree(
    responses: tuple[FixResponse, ...],
    *,
    request: FixRequest,
    inspection: WorkspaceInspection,
) -> None:
    """Hold the responses to what the working tree actually shows."""
    if inspection.head_sha != request.target.head_sha:
        raise ScopeViolation(
            f"the coding agent moved HEAD to {inspection.head_sha}, away from the "
            f"target {request.target.head_sha}. Committing is not part of this "
            "step: a committed fix is a change 'git status' no longer reports"
        )

    reported = {path for response in responses for path in response.files_changed}
    actual = set(inspection.changed_paths)

    hidden = sorted(actual - reported)
    if hidden:
        raise ScopeViolation(
            f"the working tree holds {len(hidden)} changed path(s) the coding "
            f"agent did not report: {', '.join(hidden[:5])}"
            + (", ..." if len(hidden) > 5 else "")
        )

    phantom = sorted(reported - actual)
    if phantom:
        raise ScopeViolation(
            "the coding agent reported changing "
            + ", ".join(phantom[:5])
            + (", ..." if len(phantom) > 5 else "")
            + ", but the working tree shows no change there"
        )

    outside = sorted(path for path in actual if not request.permits(path))
    if outside:
        allowed = ", ".join(entry.display() for entry in request.allowed_paths)
        raise ScopeViolation(
            f"the fix changed {len(outside)} path(s) outside the scope it was "
            f"routed with: {', '.join(outside[:5])}"
            + (", ..." if len(outside) > 5 else "")
            + f". Allowed: {allowed}"
        )

    if inspection.unexpected_ignored:
        paths = list(inspection.unexpected_ignored)
        shown = ", ".join(paths[:3]) + (", ..." if len(paths) > 3 else "")
        raise ScopeViolation(
            f"the coding agent left {len(paths)} git-ignored path(s) that are not "
            f"build or test residue ({shown}). The workspace started with none, so "
            "each was produced by this run, and this repository ignores credential "
            "files"
        )


def validate(
    raws: list[RawFixResponse],
    *,
    request: FixRequest,
    inspection: WorkspaceInspection,
) -> ValidatedFix:
    """Validate a whole fix turn: identity, contract, and the tree itself."""
    responses = tuple(
        validate_response(raw, index=index, target_head_sha=request.target.head_sha)
        for index, raw in enumerate(raws, start=1)
    )
    check_identity(responses, request=request)
    check_working_tree(responses, request=request, inspection=inspection)
    return ValidatedFix(responses=responses, inspection=inspection)
