"""Offline stand-ins for the commit / push / CI-wait turn.

Two things are faked here and one deliberately is not.

**GitHub is faked**, by :class:`PushGitHubClient`, which is read-only in the
same way the real client is and additionally lets a test move the pull
request head and the CI answer *between polls* -- which is the whole point,
because every interesting property of the CI wait is about state changing
underneath it. The state is mutated from the injected ``sleep``, so a test
describes a timeline rather than a call count.

**Git is not faked.** Every property about what was committed, what was
pushed and what a remote ref reads back as is exercised against real
repositories in ``tmp_path``, with a bare repository on disk standing in for
the remote -- the approach PR #32 and PR #34 established, for the same reason:
a faked git would let a claim be true of a git that does not exist, and the
claims in this slice are exactly claims about what git did.
"""

from __future__ import annotations

import json
import os
import subprocess

from fakes import (
    BASE_TIP,
    BASELINE_PATH,
    DEFAULT_WORKFLOW_FILES,
    REPO,
    pull_request_payload,
    run_payload,
)
from review_loop.github_client import GitHubApiError

#: A diff that misses both path-filtered workflows, so the baseline workflow
#: is the only run the evaluator requires.
UNFILTERED_CHANGED_FILES = ("tools/review-loop/src/review_loop/verdict.py",)

DEFAULT_BRANCH_NAME = "feat/example"
DEFAULT_NUMBER = 29


def git(cwd, *argv, check=True):
    """Run git with a fixed identity, so commits are reproducible in tests."""
    completed = subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )
    return completed.stdout.strip()


class PushGitHubClient:
    """A read-only GitHub whose head and CI a test can move between polls.

    ``head_sha`` and ``ci`` are plain attributes on purpose: a test injects a
    ``sleep`` that changes them, which reads as the timeline it is describing
    rather than as a sequence of scripted return values.
    """

    #: What one commit's CI looks like. ``missing`` means no run exists yet,
    #: which is what GitHub reports in the seconds after a push.
    STATUSES = ("missing", "pending", "success", "failure")

    def __init__(
        self,
        *,
        number: int = DEFAULT_NUMBER,
        head_sha: str,
        branch: str = DEFAULT_BRANCH_NAME,
        base_ref: str = "master",
        repo: str = REPO,
        head_repo: str | None = REPO,
        default_branch: str = "master",
        state: str = "open",
        base_tip: str = BASE_TIP,
        merge_base: str = BASE_TIP,
        ci: dict[str, str] | None = None,
        #: The pull request an observed run claims to belong to. Differing
        #: from ``number`` is how a test reproduces CI that cannot be shown to
        #: belong to this pull request at all.
        run_pr_number: int | None = None,
        error: Exception | None = None,
        errors_before_success: int = 0,
    ) -> None:
        self.repo = repo
        self.number = number
        self.head_sha = head_sha
        self.branch = branch
        self.base_ref = base_ref
        self.head_repo = head_repo
        self.default_branch = default_branch
        self.state = state
        self.base_tip = base_tip
        self.merge_base = merge_base
        self.ci = dict(ci or {})
        self.run_pr_number = run_pr_number if run_pr_number is not None else number
        self.error = error
        self.errors_before_success = errors_before_success
        self.calls: list[tuple[str, object]] = []

    # -- transport ---------------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.errors_before_success > 0:
            self.errors_before_success -= 1
            raise GitHubApiError("gh api failed (exit 1): HTTP 502")
        if self.error is not None:
            raise self.error

    # -- endpoints ---------------------------------------------------------

    def get_pull_request(self, number: int) -> dict:
        self.calls.append(("get_pull_request", number))
        self._maybe_fail()
        return pull_request_payload(
            number=self.number,
            head_sha=self.head_sha,
            base_ref=self.base_ref,
            head_ref=self.branch,
            state=self.state,
            head_repo=self.head_repo,
            default_branch=self.default_branch,
        )

    def list_workflow_runs_for_sha(self, head_sha: str) -> list[dict]:
        self.calls.append(("list_workflow_runs_for_sha", head_sha))
        self._maybe_fail()
        status = self.ci.get(head_sha, "missing")
        if status == "missing":
            return []
        if status == "pending":
            state, conclusion = "in_progress", None
        elif status == "success":
            state, conclusion = "completed", "success"
        else:
            state, conclusion = "completed", "failure"
        return [
            run_payload(
                run_id=900,
                path=BASELINE_PATH,
                head_sha=head_sha,
                status=state,
                conclusion=conclusion,
                pr_number=self.run_pr_number,
                merge_base=self.merge_base,
            )
        ]

    def list_workflow_files(self, ref: str) -> dict[str, str]:
        self.calls.append(("list_workflow_files", ref))
        self._maybe_fail()
        return dict(DEFAULT_WORKFLOW_FILES)

    def list_pull_request_files(self, number: int) -> tuple[str, ...]:
        self.calls.append(("list_pull_request_files", number))
        self._maybe_fail()
        return UNFILTERED_CHANGED_FILES

    def get_branch_tip(self, branch: str) -> str:
        self.calls.append(("get_branch_tip", branch))
        self._maybe_fail()
        return self.base_tip


class Timeline:
    """An injectable clock and sleep that runs a script as time passes.

    ``steps`` maps "after this many sleeps" to a callback that changes the
    fake GitHub. Nothing waits in real time: ``sleep`` advances a counter and
    the clock reads it, so a half-hour bounded wait costs no seconds.
    """

    def __init__(self, steps: dict[int, object] | None = None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.steps = dict(steps or {})

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        step = self.steps.get(self.sleeps)
        if step is not None:
            step()


def fix_json(
    *,
    head_sha: str,
    changed_paths: tuple[str, ...],
    patch_sha256: str,
    patch_bytes: int,
    patch_path: str | None = None,
    outcome: str = "FIX_APPLIED",
    number: int = DEFAULT_NUMBER,
    repo: str = REPO,
    base_ref: str = "master",
    ci_merge_base_sha: str = BASE_TIP,
    finding_ids: tuple[str, ...] = ("F1",),
    responses: list[dict] | None = None,
    round: int = 1,
    dry_run: bool = False,
    commit_or_push_performed: bool = False,
    unexpected_ignored: tuple[str, ...] = (),
    patch_refused: str | None = None,
    workspace_head_sha: str | None = None,
) -> str:
    """A ``review-loop fix --json`` document, in its real shape."""
    if responses is None:
        # One response per routed finding, as a real fix turn produces: the
        # handoff's finding ids come from the responses, not from the request.
        responses = [
            {
                "finding_id": fid,
                "target_head_sha": head_sha,
                "outcome": "fixed",
                "files_changed": list(changed_paths),
                "summary": "made the change",
                "verification": "python -m pytest: 693 passed",
                "reason": None,
                "scope_notes": None,
            }
            for fid in finding_ids
        ]
    return json.dumps(
        {
            "outcome": outcome,
            "exit_code": 0,
            "dry_run": dry_run,
            "reasons": ["F1 was fixed"],
            "agent_invoked": True,
            "workspace_created": True,
            "github_write_performed": False,
            "github_requests_performed": 0,
            "commit_or_push_performed": commit_or_push_performed,
            "patch_path": patch_path,
            "target": {
                "repo": repo,
                "number": number,
                "head_sha": head_sha,
                "base_ref": base_ref,
                "ci_merge_base_sha": ci_merge_base_sha,
            },
            "request": {
                "round": round,
                "allowed_paths": list(changed_paths),
                "change_set_boundary": list(changed_paths),
                "findings": [
                    {
                        "finding_id": fid,
                        "severity": "Major",
                        "location": changed_paths[0] if changed_paths else "pkg/",
                        "cited_paths": list(changed_paths[:1]),
                        "allowed_paths": list(changed_paths),
                        "out_of_boundary_paths": [],
                    }
                    for fid in finding_ids
                ],
            },
            "workspace": {
                "head_sha": workspace_head_sha or head_sha,
                "changed_paths": list(changed_paths),
                "residue_paths": [],
                "unexpected_ignored": list(unexpected_ignored),
                "patch_sha256": patch_sha256,
                "patch_bytes": patch_bytes,
                "patch_refused": patch_refused,
            },
            "responses": responses,
        }
    )
