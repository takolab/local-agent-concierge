"""Wire the read-only client, the workflow configuration and the evaluator."""

from __future__ import annotations

from .evaluate import evaluate
from .github_client import GitHubApiError, GitHubClient
from .model import CiEvaluation, NotAFullShaError, PullRequestTarget, Verdict
from .runs import RunParseError, parse_runs
from .workflow_config import classify_workflow_files


def build_target(
    payload: dict, number: int, changed_files: tuple[str, ...] = ()
) -> PullRequestTarget:
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
        changed_files=changed_files,
    )


def verify_pull_request(client: GitHubClient, number: int) -> CiEvaluation:
    """Resolve a pull request's exact head and evaluate that commit's CI.

    Both ends of the tested merge are re-read after the evidence has been
    collected: the head, because it identifies the review target, and the base
    branch tip, because a ``pull_request`` run tests the head merged onto the
    base rather than the head alone. Either one moving invalidates the
    evidence, even though only the first changes what would be reviewed.

    The changed-file list is fetched so that a path-filtered workflow's absence
    can be checked against this pull request's own diff instead of merely
    noting that a filter exists.
    """
    try:
        target = build_target(
            client.get_pull_request(number),
            number,
            changed_files=client.list_pull_request_files(number),
        )
        runs = parse_runs(client.list_workflow_runs_for_sha(target.head_sha))
        workflow_files = client.list_workflow_files(target.head_sha)
        definitions = classify_workflow_files(workflow_files, target.base_ref)
        head_after = (client.get_pull_request(number).get("head") or {}).get("sha")
        base_tip = client.get_branch_tip(target.base_ref) if target.base_ref else None
    except (GitHubApiError, RunParseError, NotAFullShaError) as exc:
        return CiEvaluation(verdict=Verdict.API_ERROR, reasons=(str(exc),))

    return evaluate(
        target=target,
        runs=runs,
        definitions=definitions,
        head_sha_at_verification=head_after if isinstance(head_after, str) else "",
        base_tip_at_verification=base_tip,
    )
