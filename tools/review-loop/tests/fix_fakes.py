"""Offline stand-ins for the routing and bounded-fix turn.

No test in this suite performs network access, invokes a real coding agent,
or requires credentials. Where a property is about what git actually did, the
tests drive real git instead of anything here.
"""

from __future__ import annotations

import json

from review_loop.agent_process import AgentRun
from review_loop.fix_response import FIX_RESPONSE_BEGIN, FIX_RESPONSE_END
from review_loop.review_target import ReviewTarget
from review_loop.verdict import Finding, Recommendation, ReviewVerdict, Severity

FULL_SHA = "3b514700c1c2c257a39a7037f1a21ca5b9064106"
OTHER_SHA = "36f33930b6f15137b160b4b05da1fd6359e0a035"
BASE_SHA = "6a2f7cfe8cc8cb4af22b7824d1c70e6fce389bb8"


def target(head_sha: str = FULL_SHA, *, number: int = 29, repo: str = "takolab/local-agent-concierge") -> ReviewTarget:
    return ReviewTarget(
        repo=repo,
        number=number,
        head_sha=head_sha,
        base_ref="master",
        ci_merge_base_sha=BASE_SHA,
    )


def finding(
    finding_id: str = "F1",
    *,
    severity: Severity = Severity.MAJOR,
    location: str = "tools/review-loop/src/review_loop/verdict.py:42",
    problem: str = "the limit is not enforced",
    evidence: str = "MAX_FINDINGS is defined but never read",
    required_outcome: str = "a verdict above the limit is rejected",
    scope_boundary: str | None = None,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=severity,
        location=location,
        problem=problem,
        evidence=evidence,
        required_outcome=required_outcome,
        scope_boundary=scope_boundary,
    )


def verdict(
    *findings: Finding,
    head_sha: str = FULL_SHA,
    recommendation: Recommendation | None = None,
    escalation_reason: str | None = None,
) -> ReviewVerdict:
    if recommendation is None:
        recommendation = (
            Recommendation.CHANGES_REQUESTED if findings else Recommendation.APPROVED
        )
    return ReviewVerdict(
        round=1,
        reviewed_head_sha=head_sha,
        recommendation=recommendation,
        open_findings=tuple(findings),
        escalation_reason=escalation_reason,
    )


def response_text(
    *,
    finding_id: str = "F1",
    head_sha: str = FULL_SHA,
    outcome: str = "fixed",
    files: tuple[str, ...] = ("tools/review-loop/src/review_loop/verdict.py",),
    verification: str | None = "python -m pytest tools/review-loop/tests: 371 passed",
    summary: str = "enforced the limit and covered it with a test",
    reason: str | None = None,
    scope_notes: str | None = None,
    preamble: str = "I read the finding and made the change.\n",
) -> str:
    lines = [
        FIX_RESPONSE_BEGIN,
        f"Finding ID: {finding_id}",
        f"Target head SHA: {head_sha}",
        f"Outcome: {outcome}",
    ]
    if files:
        lines.append("Files changed:")
        lines.extend(f"- {path}" for path in files)
    else:
        lines.append("Files changed: (none)")
    if verification is not None:
        lines.append(f"Verification: {verification}")
    lines.append(f"Summary: {summary}")
    if reason is not None:
        lines.append(f"Reason: {reason}")
    if scope_notes is not None:
        lines.append(f"Scope notes: {scope_notes}")
    lines.append(FIX_RESPONSE_END)
    return preamble + "\n".join(lines) + "\n"


def review_json(
    *findings: Finding,
    outcome: str = "REVIEW_VALID",
    head_sha: str = FULL_SHA,
    recommendation: str | None = None,
    number: int = 29,
    repo: str = "takolab/local-agent-concierge",
    round: int = 1,
    reviewed_head_sha: str | None = None,
    escalation_reason: str | None = None,
) -> str:
    """A ``review-loop review --json`` document, in its real shape."""
    if recommendation is None:
        recommendation = "changes_requested" if findings else "approved"
    return json.dumps(
        {
            "outcome": outcome,
            "exit_code": 0,
            "dry_run": False,
            "reasons": ["recorded as comment 1"],
            "reviewer_invoked": True,
            "github_write_performed": True,
            "comment_id": 1,
            "existing_comment_id": None,
            "ci_verification": "READY",
            "ci_reverification": "READY",
            "target": {
                "repo": repo,
                "number": number,
                "head_sha": head_sha,
                "base_ref": "master",
                "ci_merge_base_sha": BASE_SHA,
                "ci_evidence": [],
            },
            "verdict": {
                "round": round,
                "reviewed_head_sha": reviewed_head_sha or head_sha,
                "recommendation": recommendation,
                "blocking": 0,
                "major": 0,
                "minor": 0,
                "escalation_reason": escalation_reason,
                "open_findings": [
                    {
                        "finding_id": f.finding_id,
                        "severity": f.severity.value,
                        "location": f.location,
                        "problem": f.problem,
                        "evidence": f.evidence,
                        "required_outcome": f.required_outcome,
                        "scope_boundary": f.scope_boundary,
                    }
                    for f in findings
                ],
            },
            "comment_body": "## Independent AI Review",
        }
    )


class ScriptedAgent:
    """A coding agent that answers from a script and records what it was told."""

    def __init__(self, *, stdout: str = "", failure: str | None = None, edit=None):
        self._stdout = stdout
        self._failure = failure
        self._edit = edit
        self.prompts: list[str] = []
        self.cwds: list[str | None] = []

    def invoke(self, prompt: str, *, cwd: str | None = None) -> AgentRun:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        if self._edit is not None and cwd is not None:
            self._edit(cwd)
        if self._failure is not None:
            return AgentRun(failure=self._failure)
        return AgentRun(stdout=self._stdout)


class FakeWorkspace:
    """A workspace that yields a directory a test already prepared."""

    def __init__(self, path: str, *, error: Exception | None = None):
        self.path = path
        self._error = error
        self.opened: list[str] = []

    def open(self, head_sha: str):
        from contextlib import contextmanager

        @contextmanager
        def _open():
            if self._error is not None:
                raise self._error
            self.opened.append(head_sha)
            yield self.path

        return _open()

    def describe(self) -> str:
        return f"{self.path} (test fixture)"
