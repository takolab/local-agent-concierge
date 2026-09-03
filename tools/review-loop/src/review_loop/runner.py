"""Wire the read-only client, the workflow configuration and the evaluator."""

from __future__ import annotations

from .evaluate import evaluate
from .github_client import GitHubApiError, GitHubClient
from .model import CiEvaluation, NotAFullShaError, PullRequestTarget, Verdict
from .runs import RunParseError, parse_runs
from .workflow_config import classify_workflow_files


def build_target(payload: dict, number: int) -> PullRequestTarget:
    """Extract the exact review target from a pull request payload.

    ``head.sha`` is the only accepted source. ``merge_commit_sha`` describes a
    commit that does not exist on the branch, and ``base.sha`` describes the
    branch being merged into; neither is what CI ran against.
    """
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    return PullRequestTarget(
        number=int(payload.get("number", number)),
        head_sha=head.get("sha"),
        base_ref=str(base.get("ref", "")),
        head_ref=str(head.get("ref", "")),
        state=str(payload.get("state", "")),
    )


def verify_pull_request(client: GitHubClient, number: int) -> CiEvaluation:
    """Resolve a pull request's exact head and evaluate that commit's CI.

    The head is read twice: once to choose the target, and once after the runs
    and workflow configuration have been collected. If it moved in between, the
    collected evidence describes a commit that is no longer the review target.
    """
    try:
        target = build_target(client.get_pull_request(number), number)
        runs = parse_runs(client.list_workflow_runs_for_sha(target.head_sha))
        workflow_files = client.list_workflow_files(target.head_sha)
        definitions = classify_workflow_files(workflow_files, target.base_ref)
        head_after = (client.get_pull_request(number).get("head") or {}).get("sha")
    except (GitHubApiError, RunParseError, NotAFullShaError) as exc:
        return CiEvaluation(verdict=Verdict.API_ERROR, reasons=(str(exc),))

    return evaluate(
        target=target,
        runs=runs,
        definitions=definitions,
        head_sha_at_verification=head_after if isinstance(head_after, str) else "",
    )
