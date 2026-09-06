"""Offline builders for the re-review turn's two inputs and its response.

The documents here are built the way the earlier stages actually emit them --
``review-loop review --json`` and ``review-loop push --json`` -- so a test
that changes one field changes exactly one fact, and a test that forgets a
field fails the way a real malformed document would.
"""

from __future__ import annotations

import json

from review_loop.rereview import RE_REVIEW_BEGIN, RE_REVIEW_END, RE_REVIEW_ROUND

from fakes import BASE_TIP, FULL_SHA, REPO

#: The commit the round-1 review read, and the parent of the fix.
REVIEWED_SHA = FULL_SHA
#: The pushed fix commit: the only commit a re-review turn reads.
PUSHED_SHA = "5a0d6cbb0f0f4b0e0d0b9a1c2d3e4f5061728394"
#: A third commit, for "someone pushed on top" cases.
LATER_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a0f9e8d7c6"

PR = 27
BASE_REF = "master"

#: The candidate patch digest the fix turn validated, as the push turn
#: reports it on both the created-commit and already-pushed paths.
CANDIDATE_DIGEST = "b" * 64

#: The round-1 findings every re-review test re-evaluates unless it says
#: otherwise. Two of them, so "one resolved, one not" is expressible.
DEFAULT_FINDINGS = (
    {
        "finding_id": "F1",
        "severity": "Major",
        "location": "services/orchestrator/src/orchestrator/http_server.py:42",
        "problem": "The dispatch handler swallows the agent error.",
        "evidence": "test_dispatch_error asserts only the status code.",
        "required_outcome": "The error is surfaced and a test proves it.",
        "scope_boundary": None,
    },
    {
        "finding_id": "F2",
        "severity": "Minor",
        "location": "docs/roadmap.md",
        "problem": "The roadmap claims a capability the code does not have.",
        "evidence": "No module implements the described behaviour.",
        "required_outcome": "The claim is removed or made true.",
        "scope_boundary": None,
    },
)


def review_document(
    *,
    outcome: str = "REVIEW_VALID",
    repo: str = REPO,
    number: int = PR,
    head_sha: str = REVIEWED_SHA,
    base_ref: str = BASE_REF,
    merge_base: str = BASE_TIP,
    round_number: int = 1,
    recommendation: str = "changes_requested",
    findings: tuple[dict, ...] | None = None,
) -> str:
    """A ``review-loop review --json`` document."""
    payload = {
        "outcome": outcome,
        "target": {
            "repo": repo,
            "number": number,
            "head_sha": head_sha,
            "base_ref": base_ref,
            "ci_merge_base_sha": merge_base,
        },
        "verdict": {
            "round": round_number,
            "reviewed_head_sha": head_sha,
            "recommendation": recommendation,
            "escalation_reason": None,
            "open_findings": list(
                DEFAULT_FINDINGS if findings is None else findings
            ),
        },
    }
    return json.dumps(payload, indent=2)


def push_document(
    *,
    outcome: str = "PUSH_READY",
    repo: str = REPO,
    number: int = PR,
    reviewed_sha: str = REVIEWED_SHA,
    pushed_sha: str = PUSHED_SHA,
    base_ref: str = BASE_REF,
    merge_base: str = BASE_TIP,
    base_tip: str | None = None,
    ci_verdict: str = "READY",
    ci_head_sha: str | None = None,
    bound_to_pushed_commit: bool = True,
    repository_mutated: bool = True,
    boundary_status: str = "clean",
    dry_run: bool = False,
    include_commit: bool = True,
    commit_parent: str | None = None,
    include_verified_target: bool = True,
    verified_head_sha: str | None = None,
    include_provenance: bool = True,
    source_round: int = 1,
    source_finding_ids: tuple[str, ...] = ("F1", "F2"),
    source_head_sha: str | None = None,
    source_patch_sha256: str = CANDIDATE_DIGEST,
    fix_patch_sha256: str | None = None,
    fix_sha: str | None = None,
    fix_parent_sha: str | None = None,
) -> str:
    """A ``review-loop push --json`` document."""
    payload = {
        "outcome": outcome,
        "dry_run": dry_run,
        "repository_mutated": repository_mutated,
        "boundary_status": boundary_status,
        "push_performed": True,
        "already_pushed": False,
        "commit_created": include_commit,
        "pushed_sha": pushed_sha,
        "github_write_performed": False,
        "target": {
            "repo": repo,
            "number": number,
            "head_sha": reviewed_sha,
            "base_ref": base_ref,
            "ci_merge_base_sha": merge_base,
        },
        "ci": {
            "verdict": ci_verdict,
            "polls": 1,
            "head_sha": pushed_sha if ci_head_sha is None else ci_head_sha,
            "bound_to_pushed_commit": bound_to_pushed_commit,
            "ci_merge_base_sha": merge_base,
            "base_tip_at_verification": merge_base if base_tip is None else base_tip,
            "reasons": [],
        },
    }
    if include_provenance:
        payload["fix_provenance"] = {
            "source_round": source_round,
            "source_reviewed_head_sha": reviewed_sha
            if source_head_sha is None
            else source_head_sha,
            "source_finding_ids": list(source_finding_ids),
            "source_patch_sha256": source_patch_sha256,
            "fix_sha": pushed_sha if fix_sha is None else fix_sha,
            "fix_parent_sha": reviewed_sha
            if fix_parent_sha is None
            else fix_parent_sha,
            "fix_patch_sha256": source_patch_sha256
            if fix_patch_sha256 is None
            else fix_patch_sha256,
        }
    if include_commit:
        payload["commit"] = {
            "sha": pushed_sha,
            "parent_sha": reviewed_sha if commit_parent is None else commit_parent,
            "tree_sha": "1111111111111111111111111111111111111111",
            "patch_sha256": CANDIDATE_DIGEST,
            "changed_paths": ["pkg/code.py"],
        }
    if include_verified_target:
        payload["verified_target"] = {
            "repo": repo,
            "number": number,
            "head_sha": pushed_sha if verified_head_sha is None else verified_head_sha,
            "base_ref": base_ref,
            "ci_merge_base_sha": merge_base,
        }
    return json.dumps(payload, indent=2)


def resolution_block(
    finding_id: str = "F1",
    resolution: str = "RESOLVED",
    evidence: str = "http_server.py now raises, and test_dispatch_error asserts it.",
    reason: str | None = None,
) -> list[str]:
    lines = [
        f"Finding ID: {finding_id}",
        f"Resolution: {resolution}",
        f"Evidence: {evidence}",
    ]
    if reason is not None:
        lines.append(f"Reason: {reason}")
    return lines


def fresh_block(
    finding_id: str = "R2.F1",
    severity: str = "Major",
    location: str = "services/orchestrator/src/orchestrator/http_server.py:51",
    problem: str = "The new error path leaks the upstream request id.",
    evidence: str = "The raised message interpolates request.headers['x-request-id'].",
    required_outcome: str = "The id is not included in the surfaced error.",
    scope_boundary: str | None = None,
) -> list[str]:
    lines = [
        f"Fresh finding ID: {finding_id}",
        f"Severity: {severity}",
        f"Location: {location}",
        f"Problem: {problem}",
        f"Evidence: {evidence}",
        f"Required outcome: {required_outcome}",
    ]
    if scope_boundary is not None:
        lines.append(f"Scope boundary: {scope_boundary}")
    return lines


def rereview_text(
    *,
    head_sha: str = PUSHED_SHA,
    round_number: int | str = RE_REVIEW_ROUND,
    recommendation: str = "approved",
    resolutions: tuple[list[str], ...] | None = None,
    fresh: tuple[list[str], ...] = (),
    escalation_reason: str | None = None,
    preamble: str = "I read the pushed fix commit.\n",
) -> str:
    """Build reviewer output in the Bounded Re-Review Response contract."""
    if resolutions is None:
        resolutions = (
            resolution_block("F1"),
            resolution_block(
                "F2", evidence="The roadmap paragraph now matches the code."
            ),
        )

    lines = [
        f"Round: {round_number}",
        f"Reviewed head SHA: {head_sha}",
        f"Recommendation: {recommendation}",
    ]
    if escalation_reason is not None:
        lines.append(f"Escalation reason: {escalation_reason}")
    for block in resolutions:
        lines.extend(block)
    for block in fresh:
        lines.extend(block)

    return preamble + "\n".join([RE_REVIEW_BEGIN, *lines, RE_REVIEW_END]) + "\n"
