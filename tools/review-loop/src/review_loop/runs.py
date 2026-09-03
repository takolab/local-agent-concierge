"""Normalize GitHub Actions runs into one authoritative run per workflow.

Two different things can produce several runs for a single exact commit, and
they need different tie-breakers:

* **Re-running a workflow** keeps the same ``run_id`` and increments
  ``run_attempt``. The highest attempt of a ``run_id`` supersedes its earlier
  attempts, so an older failed attempt never outvotes a newer successful one.
* **Re-triggering a workflow** (a second event on the same commit) creates a
  new ``run_id``. The most recently created run wins, so an older successful
  run never hides a newer failed or still-running one.

Workflows are grouped by workflow file path. Job and check display names are
never used: this repository's three workflows all expose a single job named
``test``, so display names cannot tell them apart.
"""

from __future__ import annotations

from .model import NotAFullShaError, WorkflowOutcome, WorkflowRun, require_full_sha

_REQUIRED_FIELDS = ("id", "workflow_id", "path", "name", "head_sha", "status", "run_attempt")


class RunParseError(ValueError):
    """A run payload was missing or malformed beyond safe interpretation."""


class WorkflowIdentityCollision(ValueError):
    """One workflow path was reported under more than one workflow id."""


def parse_run(payload: dict) -> WorkflowRun:
    """Build a :class:`WorkflowRun` from one raw Actions API run object."""
    missing = [field for field in _REQUIRED_FIELDS if payload.get(field) is None]
    if missing:
        raise RunParseError(f"workflow run payload is missing fields: {sorted(missing)}")

    conclusion = payload.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        raise RunParseError(f"workflow run conclusion is not a string: {conclusion!r}")

    try:
        return WorkflowRun(
            run_id=int(payload["id"]),
            workflow_id=int(payload["workflow_id"]),
            workflow_path=str(payload["path"]),
            workflow_name=str(payload["name"]),
            head_sha=require_full_sha(payload["head_sha"], label="workflow run head_sha"),
            status=str(payload["status"]),
            conclusion=conclusion,
            run_attempt=int(payload["run_attempt"]),
            event=str(payload.get("event", "")),
            created_at=str(payload.get("created_at", "")),
        )
    except (RunParseError, NotAFullShaError):
        # An abbreviated or malformed SHA is its own, more specific failure.
        raise
    except (TypeError, ValueError) as exc:
        raise RunParseError(f"workflow run payload could not be parsed: {exc}") from exc


def parse_runs(payloads: list[dict]) -> tuple[WorkflowRun, ...]:
    return tuple(parse_run(payload) for payload in payloads)


def _latest_attempt_per_run_id(runs: list[WorkflowRun]) -> list[WorkflowRun]:
    by_run_id: dict[int, WorkflowRun] = {}
    for run in runs:
        existing = by_run_id.get(run.run_id)
        if existing is None or run.run_attempt > existing.run_attempt:
            by_run_id[run.run_id] = run
    return list(by_run_id.values())


def normalize(runs: tuple[WorkflowRun, ...]) -> tuple[WorkflowOutcome, ...]:
    """Select the authoritative run for each workflow path.

    Raises :class:`WorkflowIdentityCollision` if one path maps to several
    workflow ids, because the runs could then no longer be attributed to a
    single workflow with confidence.
    """
    grouped: dict[str, list[WorkflowRun]] = {}
    for run in runs:
        grouped.setdefault(run.workflow_path, []).append(run)

    outcomes: list[WorkflowOutcome] = []
    for path, group in sorted(grouped.items()):
        workflow_ids = {run.workflow_id for run in group}
        if len(workflow_ids) > 1:
            raise WorkflowIdentityCollision(
                f"workflow path {path!r} maps to multiple workflow ids {sorted(workflow_ids)}"
            )

        candidates = _latest_attempt_per_run_id(group)
        winner = max(candidates, key=lambda run: (run.created_at, run.run_id))
        superseded = tuple(
            sorted(run.run_id for run in group if run.run_id != winner.run_id)
        )
        outcomes.append(
            WorkflowOutcome(
                workflow_path=path,
                workflow_name=winner.workflow_name,
                run=winner,
                superseded_run_ids=superseded,
            )
        )
    return tuple(outcomes)
