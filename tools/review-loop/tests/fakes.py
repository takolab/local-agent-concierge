"""Offline stand-ins for the GitHub API.

No test in this suite performs network access or requires credentials.
"""

from __future__ import annotations

from review_loop.github_client import GitHubApiError
from review_loop.github_comments import ExistingComment
from review_loop.reviewer_process import ReviewerRun
from review_loop.verdict import VERDICT_BEGIN, VERDICT_END

FULL_SHA = "3b514700c1c2c257a39a7037f1a21ca5b9064106"
OTHER_SHA = "36f33930b6f15137b160b4b05da1fd6359e0a035"

BASELINE_PATH = ".github/workflows/pytest.yml"
FILTERED_PATH = ".github/workflows/orchestrator.yml"
SECOND_FILTERED_PATH = ".github/workflows/agent-contracts.yml"

BASELINE_YAML = """\
name: Python tests
on:
  pull_request:
    branches:
      - master
  push:
    branches:
      - master
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""

FILTERED_YAML = """\
name: Orchestrator tests
on:
  pull_request:
    branches:
      - master
    paths:
      - "services/orchestrator/**"
  workflow_dispatch:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""

SECOND_FILTERED_YAML = """\
name: Agent Contracts tests
on:
  pull_request:
    branches:
      - master
    paths:
      - "packages/agent-contracts/**"
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""

DEFAULT_WORKFLOW_FILES = {
    BASELINE_PATH: BASELINE_YAML,
    FILTERED_PATH: FILTERED_YAML,
    SECOND_FILTERED_PATH: SECOND_FILTERED_YAML,
}


#: The base branch tip. ``pull_request`` runs merge the head onto this.
BASE_TIP = "6a2f7cfe8cc8cb4af22b7824d1c70e6fce389bb8"
ADVANCED_BASE_TIP = "7dc3c2e1ca1977eed14e143457e6b037824af95b"

DEFAULT_CHANGED_FILES = ("services/orchestrator/src/orchestrator/http_server.py",)


def pull_request_payload(
    number: int = 27,
    head_sha: str = FULL_SHA,
    base_ref: str = "master",
    head_ref: str = "feat/example",
    state: str = "open",
) -> dict:
    return {
        "number": number,
        "state": state,
        "head": {"sha": head_sha, "ref": head_ref},
        "base": {"sha": OTHER_SHA, "ref": base_ref},
        "merge_commit_sha": "6a2f7cfe8cc8cb4af22b7824d1c70e6fce389bb8",
    }


def run_payload(
    *,
    run_id: int,
    workflow_id: int = 331860080,
    path: str = BASELINE_PATH,
    name: str = "Python tests",
    head_sha: str = FULL_SHA,
    status: str = "completed",
    conclusion: str | None = "success",
    run_attempt: int = 1,
    event: str = "pull_request",
    created_at: str = "2026-09-03T07:49:59Z",
    pr_number: int | None = 27,
    merge_base: str | None = BASE_TIP,
) -> dict:
    """Build a run payload.

    ``pull_requests`` mirrors the live API: it is populated while the pull
    request is open and empty once it closes.
    """
    associations = []
    if pr_number is not None:
        association = {"number": pr_number, "head": {"sha": head_sha}}
        if merge_base is not None:
            association["base"] = {"sha": merge_base, "ref": "master"}
        associations.append(association)
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "path": path,
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": run_attempt,
        "event": event,
        "created_at": created_at,
        "pull_requests": associations,
    }


class FakeGitHubClient:
    """Records every call so tests can assert the runner stays read-only."""

    def __init__(
        self,
        *,
        pull_requests: list[dict] | None = None,
        runs: list[dict] | None = None,
        workflow_files: dict[str, str] | None = None,
        changed_files: tuple[str, ...] = DEFAULT_CHANGED_FILES,
        base_tip: str = BASE_TIP,
        error: Exception | None = None,
    ) -> None:
        self._pull_requests = list(pull_requests or [pull_request_payload()])
        self._runs = list(runs if runs is not None else [])
        self._workflow_files = (
            dict(DEFAULT_WORKFLOW_FILES) if workflow_files is None else dict(workflow_files)
        )
        self._changed_files = tuple(changed_files)
        self._base_tip = base_tip
        self._error = error
        self.calls: list[tuple[str, object]] = []

    def _maybe_fail(self) -> None:
        if self._error is not None:
            raise self._error

    def get_pull_request(self, number: int) -> dict:
        self.calls.append(("get_pull_request", number))
        self._maybe_fail()
        # Successive calls walk the list, so a moving head can be simulated.
        # The last entry repeats once exhausted.
        index = min(
            sum(1 for call in self.calls if call[0] == "get_pull_request") - 1,
            len(self._pull_requests) - 1,
        )
        return self._pull_requests[index]

    def list_workflow_runs_for_sha(self, head_sha: str) -> list[dict]:
        self.calls.append(("list_workflow_runs_for_sha", head_sha))
        self._maybe_fail()
        # Mirrors the real endpoint: it matches on the exact SHA only, and
        # answers anything else with an empty list rather than an error.
        return [run for run in self._runs if run.get("head_sha") == head_sha]

    def list_workflow_files(self, ref: str) -> dict[str, str]:
        self.calls.append(("list_workflow_files", ref))
        self._maybe_fail()
        return dict(self._workflow_files)

    def list_pull_request_files(self, number: int) -> tuple[str, ...]:
        self.calls.append(("list_pull_request_files", number))
        self._maybe_fail()
        return self._changed_files

    def get_branch_tip(self, branch: str) -> str:
        self.calls.append(("get_branch_tip", branch))
        self._maybe_fail()
        return self._base_tip


class FailingGitHubClient(FakeGitHubClient):
    def __init__(self, message: str = "gh api failed (exit 1): HTTP 502") -> None:
        super().__init__(error=GitHubApiError(message))


# --- review turn fakes ------------------------------------------------------


def verdict_text(
    *,
    head_sha: str = FULL_SHA,
    round_number: int | str = 1,
    recommendation: str = "changes_requested",
    findings: tuple[dict, ...] | None = None,
    resolved: str | None = None,
    escalation_reason: str | None = None,
    preamble: str = "Here is what I found.\n",
) -> str:
    """Build reviewer output in the Structured Verdict contract."""
    if findings is None:
        findings = (
            {
                "Finding ID": "F1",
                "Severity": "Major",
                "Location": "services/orchestrator/src/orchestrator/http_server.py:42",
                "Problem": "The dispatch handler swallows the agent error.",
                "Evidence": "test_dispatch_error asserts only the status code.",
                "Required outcome": "The error is surfaced and a test proves it.",
            },
        )

    lines = [
        f"Round: {round_number}",
        f"Reviewed head SHA: {head_sha}",
        f"Recommendation: {recommendation}",
    ]
    if resolved is not None:
        lines.append(f"Resolved: {resolved}")
    if escalation_reason is not None:
        lines.append(f"Escalation reason: {escalation_reason}")
    for finding in findings:
        lines.extend(f"{label}: {value}" for label, value in finding.items())

    return preamble + "\n".join([VERDICT_BEGIN, *lines, VERDICT_END]) + "\n"


class FakeReviewer:
    """A reviewer that returns a canned run and records what it was asked."""

    def __init__(self, run: ReviewerRun | None = None) -> None:
        self.run = run if run is not None else ReviewerRun(stdout=verdict_text())
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> ReviewerRun:
        self.prompts.append(prompt)
        return self.run

    @property
    def invoked(self) -> bool:
        return bool(self.prompts)


class FakeCommentReader:
    """Issue comments already on the pull request."""

    def __init__(self, bodies: list[str] | None = None, error: Exception | None = None) -> None:
        self.bodies = list(bodies or [])
        self.error = error
        self.calls = 0

    def list_comments(self, number: int):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return tuple(
            ExistingComment(comment_id=1000 + index, author="takolab", body=body)
            for index, body in enumerate(self.bodies)
        )


class FakeCommentWriter:
    """Records every comment it is asked to create."""

    def __init__(self, error: Exception | None = None, comment_id: int = 5555) -> None:
        self.error = error
        self.comment_id = comment_id
        self.posted: list[tuple[int, str]] = []

    def create_comment(self, number: int, body: str) -> int:
        self.posted.append((number, body))
        if self.error is not None:
            raise self.error
        return self.comment_id
