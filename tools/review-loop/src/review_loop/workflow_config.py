"""Decide, from a workflow file, whether a pull request must start it.

This is deliberately *not* a re-implementation of GitHub's path-filter
matching. It answers one narrower question: can the absence of a run for this
workflow ever be explained by the pull request's diff?

* no ``paths``/``paths-ignore`` filter -> no, absence is unexplainable
  (:data:`~review_loop.model.TriggerExpectation.REQUIRED`)
* a path filter is present -> only if the pull request's own diff misses the
  filter (:data:`~review_loop.model.TriggerExpectation.CONDITIONAL`). The
  filter patterns are carried on the definition so that the evaluator can
  decide that against the actual diff; see :mod:`review_loop.path_filter`.

Anything the parser does not confidently understand becomes ``UNKNOWN``, which
the evaluator treats as ambiguous rather than as absence.
"""

from __future__ import annotations

import yaml

from .model import PathFilter, TriggerExpectation, WorkflowDefinition

_PATH_FILTER_KEYS = ("paths", "paths-ignore")

#: PyYAML follows YAML 1.1, where a bare ``on:`` key is the boolean ``True``.
#: Both spellings must be accepted or every workflow would look untriggered.
_ON_KEYS = ("on", True)


def _trigger_block(document: object) -> object:
    if not isinstance(document, dict):
        return _MISSING
    for key in _ON_KEYS:
        if key in document:
            return document[key]
    return _MISSING


class _Missing:
    pass


_MISSING = _Missing()


#: Branch patterns are matched by literal equality only. GitHub's branch glob
#: syntax is not Python's: its ``*`` does not span ``/`` (which is why
#: ``releases/**`` exists as a separate documented form), and it supports
#: ordered negation with ``!``. ``fnmatch`` disagrees on both, and a wrong
#: "this workflow does not apply to this branch" silently excuses a missing
#: run. Anything beyond a literal is therefore undecidable.
_BRANCH_PATTERN_METACHARACTERS = frozenset("*?[]!+")


def _branch_matches(patterns: object, base_ref: str) -> bool | None:
    """Return whether ``base_ref`` matches, or ``None`` if undecidable."""
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return None
    if any(set(pattern) & _BRANCH_PATTERN_METACHARACTERS for pattern in patterns):
        return None
    return any(pattern == base_ref for pattern in patterns)


def classify_trigger(source: str, path: str, base_ref: str) -> WorkflowDefinition:
    """Classify one workflow file against the pull request's base branch."""
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError:
        return WorkflowDefinition(path=path, name=path, expectation=TriggerExpectation.UNKNOWN)

    name = path
    if isinstance(document, dict) and isinstance(document.get("name"), str):
        name = document["name"]

    triggers = _trigger_block(document)
    if isinstance(triggers, _Missing):
        return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.UNKNOWN)

    # ``on: pull_request`` and ``on: [pull_request, push]`` carry no filters at
    # all, so the workflow always runs for a pull request.
    if isinstance(triggers, str):
        expectation = (
            TriggerExpectation.REQUIRED
            if triggers == "pull_request"
            else TriggerExpectation.NOT_EXPECTED
        )
        return WorkflowDefinition(path=path, name=name, expectation=expectation)

    if isinstance(triggers, list):
        if not all(isinstance(item, str) for item in triggers):
            return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.UNKNOWN)
        expectation = (
            TriggerExpectation.REQUIRED
            if "pull_request" in triggers
            else TriggerExpectation.NOT_EXPECTED
        )
        return WorkflowDefinition(path=path, name=name, expectation=expectation)

    if not isinstance(triggers, dict):
        return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.UNKNOWN)

    if "pull_request" not in triggers:
        return WorkflowDefinition(
            path=path, name=name, expectation=TriggerExpectation.NOT_EXPECTED
        )

    pull_request = triggers["pull_request"]
    if pull_request is None:
        return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.REQUIRED)

    if not isinstance(pull_request, dict):
        return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.UNKNOWN)

    # GitHub does not allow both keys for one event. Evaluating either of them
    # on an invalid configuration would produce a confident NOT_EXPECTED, which
    # silently excuses a missing run -- the same reason ``paths`` and
    # ``paths-ignore`` together are treated as unreadable.
    if "branches" in pull_request and "branches-ignore" in pull_request:
        return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.UNKNOWN)

    if "branches-ignore" in pull_request:
        ignored = _branch_matches(pull_request["branches-ignore"], base_ref)
        if ignored is None:
            return WorkflowDefinition(
                path=path, name=name, expectation=TriggerExpectation.UNKNOWN
            )
        if ignored:
            return WorkflowDefinition(
                path=path, name=name, expectation=TriggerExpectation.NOT_EXPECTED
            )

    if "branches" in pull_request:
        matched = _branch_matches(pull_request["branches"], base_ref)
        if matched is None:
            return WorkflowDefinition(
                path=path, name=name, expectation=TriggerExpectation.UNKNOWN
            )
        if not matched:
            return WorkflowDefinition(
                path=path, name=name, expectation=TriggerExpectation.NOT_EXPECTED
            )

    present = [key for key in _PATH_FILTER_KEYS if key in pull_request]
    if present:
        return WorkflowDefinition(
            path=path,
            name=name,
            expectation=TriggerExpectation.CONDITIONAL,
            path_filter=_path_filter(pull_request, present),
        )

    return WorkflowDefinition(path=path, name=name, expectation=TriggerExpectation.REQUIRED)


def _path_filter(pull_request: dict, present: list[str]) -> PathFilter:
    """Extract the filter patterns, or mark them unreadable.

    Both keys together is not valid GitHub configuration for one event, so it
    is treated as unreadable rather than guessed at.
    """
    if len(present) != 1:
        return PathFilter(mode="unreadable")
    key = present[0]
    patterns = pull_request[key]
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return PathFilter(mode="unreadable")
    return PathFilter(mode=key, patterns=tuple(patterns))


def classify_workflow_files(
    files: dict[str, str], base_ref: str
) -> tuple[WorkflowDefinition, ...]:
    """Classify every workflow file, ordered by path for stable output."""
    return tuple(
        classify_trigger(source, path, base_ref) for path, source in sorted(files.items())
    )
