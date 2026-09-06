"""Carry a validated candidate patch from the fix turn to the push turn.

:mod:`review_loop.routing` reads a review turn's own JSON back through the
invariants that produced it. This module does the same job one stage later,
and the standard is higher for a reason that is easy to state: the previous
handoff decided which findings an agent was allowed to *read*, while this one
decides what gets committed to a branch GitHub will build and a human will
merge. A mistake there survives the run.

So a fix handoff is admissible only if it describes **one specific thing**:
a fix turn that ended ``FIX_APPLIED``, against one exact reviewed commit,
whose captured patch has an identity, and whose responses agree with the
changed-path set the runner observed in the working tree. Every weaker shape
is refused rather than interpreted:

* Any other fix outcome. ``FIX_NOT_APPLIED``, ``FIX_ESCALATED`` and
  ``PATCH_TOO_LARGE`` are not "a fix with caveats"; the first two produced no
  fix at all, and the third produced one nobody can retrieve.
* A dry run, or a document that already reports a commit or push. Neither
  describes a candidate patch waiting to be committed.
* A missing or malformed ``patch_sha256``. The digest *is* the handoff's
  load-bearing field: without it the push stage would be committing a file it
  can only assume came from the fix turn, which is precisely the assumption
  this pipeline exists to remove.
* Responses that do not all report ``fixed``, or whose reported files do not
  reconstruct the observed changed-path set exactly.

**A handoff file is operator-controlled input**, exactly as the review handoff
is, and the same limits apply. Anyone who can write it can describe a
different patch -- but they cannot make the runner commit one, because the
digest recorded here is checked against the patch file's actual bytes, and
the reviewed commit recorded here is checked against what GitHub and the
remote say this pull request's head is. The file selects; git and GitHub
decide.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass

from .fix_response import MAX_FILES_CHANGED, MAX_PATCH_BYTES, MAX_PATH_CHARS
from .model import FULL_SHA_PATTERN
from .review_target import ReviewTarget
from .verdict import MAX_FIELD_CHARS, SUPPORTED_ROUND
from .verdict_validation import _FINDING_ID_PATTERN

#: The only fix outcome that leaves a validated candidate patch behind.
PUSHABLE_FIX_OUTCOME = "FIX_APPLIED"

#: A SHA-256 hex digest, as :mod:`review_loop.patch_identity` renders one.
DIGEST_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

_UNSAFE_PATH = re.compile(r"[\x00-\x1f\\]")


class FixHandoffError(ValueError):
    """The push input is not a validated candidate patch this runner produced."""


@dataclass(frozen=True)
class FixHandoff:
    """One validated candidate patch, carried across a process boundary."""

    target: ReviewTarget
    #: Every path the fix turn observed as changed, and the exact set the
    #: candidate patch must reproduce when it is applied.
    changed_paths: tuple[str, ...]
    #: SHA-256 of the candidate patch, as the fix turn captured it.
    patch_sha256: str
    patch_bytes: int
    #: Where the fix turn wrote the patch, if it was asked to. Advisory: the
    #: digest decides whether a file is the candidate patch, not its name.
    patch_path: str | None
    finding_ids: tuple[str, ...]
    #: The review round these findings belong to. Pinned to
    #: :data:`review_loop.verdict.SUPPORTED_ROUND` by the check below, and
    #: carried explicitly rather than assumed so that the push turn can
    #: report *which review* caused this fix as a read fact rather than as a
    #: constant a later reader has to trust.
    round: int = SUPPORTED_ROUND


def _require(payload: dict, key: str, *, where: str):
    if key not in payload or payload[key] is None:
        raise FixHandoffError(f"{where} is missing the required field {key!r}")
    return payload[key]


def _text(value: object, key: str, *, where: str, limit: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixHandoffError(f"{where}: {key!r} must be a non-empty string")
    if len(value) > limit:
        raise FixHandoffError(
            f"{where}: {key!r} is {len(value)} characters, above the {limit} limit"
        )
    return value.strip()


def _sha(value: object, key: str, *, where: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_PATTERN.match(value):
        raise FixHandoffError(
            f"{where}: {key!r} must be an exact 40-character lowercase hex SHA, "
            f"got {value!r}"
        )
    return value


def _false(payload: dict, key: str, *, where: str) -> None:
    """Refuse a document that already claims the thing this stage would do."""
    value = payload.get(key, False)
    if value is not False:
        raise FixHandoffError(
            f"{where} reports {key}={value!r}; a candidate patch to commit is one "
            "no stage has acted on yet"
        )


def _path(value: object, *, where: str) -> str:
    """Read one observed changed path, with the fix contract's own rules."""
    if not isinstance(value, str) or not value.strip():
        raise FixHandoffError(f"{where} lists an empty changed path")
    text = value.strip()
    if len(text) > MAX_PATH_CHARS:
        raise FixHandoffError(
            f"{where} lists a changed path of {len(text)} characters, above the "
            f"{MAX_PATH_CHARS} limit"
        )
    if _UNSAFE_PATH.search(text):
        raise FixHandoffError(
            f"{where} lists a changed path containing a control character or a "
            "backslash, which is not a repository path"
        )
    if text.startswith("/") or text.startswith("~"):
        raise FixHandoffError(
            f"{where} lists {text!r} as an absolute path; changed files are "
            "recorded relative to the repository root"
        )
    normalised = posixpath.normpath(text)
    if normalised == "." or ".." in normalised.split("/"):
        raise FixHandoffError(
            f"{where} lists {text!r}, which leaves the repository root"
        )
    return normalised


def _target(raw: object, *, expected_repo: str | None) -> ReviewTarget:
    if not isinstance(raw, dict):
        raise FixHandoffError("the push input's 'target' is not an object")

    repo = _text(_require(raw, "repo", where="the target"), "repo", where="the target")
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise FixHandoffError(
            f"the target's repository {repo!r} is not in owner/name form"
        )
    if expected_repo is not None and repo != expected_repo:
        raise FixHandoffError(
            f"the push input describes {repo}, but --repo says {expected_repo}"
        )

    number = _require(raw, "number", where="the target")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise FixHandoffError(
            f"the target's pull request number {number!r} is not a positive integer"
        )

    return ReviewTarget(
        repo=repo,
        number=number,
        head_sha=_sha(
            _require(raw, "head_sha", where="the target"), "head_sha", where="the target"
        ),
        base_ref=_text(
            _require(raw, "base_ref", where="the target"),
            "base_ref",
            where="the target",
        ),
        ci_merge_base_sha=_sha(
            _require(raw, "ci_merge_base_sha", where="the target"),
            "ci_merge_base_sha",
            where="the target",
        ),
    )


def _changed_paths(raw: object, *, head_sha: str) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        raise FixHandoffError("the push input's 'workspace' is not an object")

    observed = _sha(
        _require(raw, "head_sha", where="the workspace"),
        "head_sha",
        where="the workspace",
    )
    if observed != head_sha:
        raise FixHandoffError(
            f"the fix turn's working tree was at {observed}, but its target is "
            f"{head_sha}; a candidate patch and the commit it is against must be "
            "the same commit"
        )

    if raw.get("patch_refused") is not None:
        raise FixHandoffError(
            f"the fix turn refused to capture its own patch ({raw['patch_refused']}), "
            "so there is no candidate patch to commit"
        )

    paths = _require(raw, "changed_paths", where="the workspace")
    if not isinstance(paths, list) or not paths:
        raise FixHandoffError(
            "the push input records no changed path, so it describes no fix"
        )
    if len(paths) > MAX_FILES_CHANGED:
        raise FixHandoffError(
            f"the push input records {len(paths)} changed paths, above the "
            f"{MAX_FILES_CHANGED} a bounded fix may touch"
        )

    normalised = tuple(
        _path(entry, where="the workspace's 'changed_paths'") for entry in paths
    )
    if len(set(normalised)) != len(normalised):
        raise FixHandoffError(
            "the push input lists the same changed path more than once"
        )
    if raw.get("unexpected_ignored"):
        raise FixHandoffError(
            "the fix turn recorded git-ignored paths that are not build or test "
            "residue; that fix was not admissible and is not pushable"
        )
    return tuple(sorted(normalised))


def _digest(raw: dict) -> tuple[str, int]:
    digest = _require(raw, "patch_sha256", where="the workspace")
    if not isinstance(digest, str) or not DIGEST_PATTERN.match(digest):
        raise FixHandoffError(
            f"the workspace's 'patch_sha256' must be a 64-character lowercase "
            f"SHA-256 digest, got {digest!r}. Without it the patch being committed "
            "could not be shown to be the one the fix turn validated"
        )
    size = _require(raw, "patch_bytes", where="the workspace")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise FixHandoffError(
            f"the workspace's 'patch_bytes' {size!r} is not a positive integer"
        )
    if size > MAX_PATCH_BYTES:
        raise FixHandoffError(
            f"the push input describes a {size}-byte patch, above the "
            f"{MAX_PATCH_BYTES} a bounded fix may produce"
        )
    return digest, size


def _finding_ids(raw: object, *, head_sha: str, changed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise FixHandoffError(
            "the push input carries no fix response, so nothing says what this "
            "patch is for"
        )

    ids: list[str] = []
    reported: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        where = f"fix response {index}"
        if not isinstance(entry, dict):
            raise FixHandoffError(f"{where} is not an object")

        finding_id = _text(
            _require(entry, "finding_id", where=where), "finding_id", where=where
        )
        if not _FINDING_ID_PATTERN.match(finding_id):
            raise FixHandoffError(f"{where}: {finding_id!r} is not a usable finding id")
        if finding_id in ids:
            raise FixHandoffError(
                f"finding id {finding_id!r} is answered more than once"
            )
        ids.append(finding_id)

        if _sha(
            _require(entry, "target_head_sha", where=where),
            "target_head_sha",
            where=where,
        ) != head_sha:
            raise FixHandoffError(
                f"{where} describes a commit other than the target {head_sha}"
            )

        outcome = _require(entry, "outcome", where=where)
        if outcome != "fixed":
            raise FixHandoffError(
                f"{where} reports {outcome!r}; only a turn in which every routed "
                "finding was fixed produces a patch to push"
            )

        files = entry.get("files_changed")
        if not isinstance(files, list) or not files:
            raise FixHandoffError(f"{where} reports 'fixed' but lists no changed file")
        reported.update(_path(path, where=where) for path in files)

    if reported != set(changed):
        missing = sorted(set(changed) - reported)
        extra = sorted(reported - set(changed))
        raise FixHandoffError(
            "the fix responses and the observed working tree describe different "
            "changes"
            + (f"; unreported: {', '.join(missing)}" if missing else "")
            + (f"; reported but unobserved: {', '.join(extra)}" if extra else "")
        )
    return tuple(ids)


def load_handoff(document: str, *, expected_repo: str | None = None) -> FixHandoff:
    """Read a ``review-loop fix --json`` document as a candidate patch to push."""
    try:
        payload = json.loads(document)
    except json.JSONDecodeError as exc:
        raise FixHandoffError(f"the push input is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FixHandoffError("the push input is not a JSON object")

    outcome = _require(payload, "outcome", where="the push input")
    if outcome != PUSHABLE_FIX_OUTCOME:
        raise FixHandoffError(
            f"the push input reports fix outcome {outcome!r}; only "
            f"{PUSHABLE_FIX_OUTCOME} leaves a validated candidate patch behind"
        )
    _false(payload, "dry_run", where="the push input")
    _false(payload, "commit_or_push_performed", where="the push input")

    target = _target(_require(payload, "target", where="the push input"),
                     expected_repo=expected_repo)

    request = payload.get("request")
    if not isinstance(request, dict):
        raise FixHandoffError("the push input's 'request' is not an object")
    round_number = request.get("round")
    if round_number != SUPPORTED_ROUND:
        raise FixHandoffError(
            f"the push input reports round {round_number!r}; this runner "
            f"pushes only the initial round (round {SUPPORTED_ROUND})"
        )

    workspace = _require(payload, "workspace", where="the push input")
    changed = _changed_paths(workspace, head_sha=target.head_sha)
    digest, size = _digest(workspace)

    finding_ids = _finding_ids(
        _require(payload, "responses", where="the push input"),
        head_sha=target.head_sha,
        changed=changed,
    )

    patch_path = payload.get("patch_path")
    if patch_path is not None and not isinstance(patch_path, str):
        raise FixHandoffError("the push input's 'patch_path' is not a string")

    return FixHandoff(
        target=target,
        changed_paths=changed,
        patch_sha256=digest,
        patch_bytes=size,
        patch_path=patch_path or None,
        finding_ids=finding_ids,
        round=round_number,
    )
