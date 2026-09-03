"""Decide whether a pull request's diff triggers a path-filtered workflow.

This answers one question and refuses the rest: *given these changed files,
must this workflow have run?* It is only ever consulted when a path-filtered
workflow produced no run, because a run that exists needs no explaining.

GitHub's filter-pattern syntax includes `?`, `+`, `[]` and leading `!` with
semantics that differ from ordinary globbing. Rather than re-implement them,
any pattern using them is reported as undecidable, and the caller fails closed.
Only literals, `/`, `*` (which does not cross `/`) and `**` (which does) are
interpreted.
"""

from __future__ import annotations

import re
from enum import Enum

from .model import PathFilter

#: Characters whose GitHub filter-pattern meaning this module does not model.
UNSUPPORTED_PATTERN_CHARACTERS = frozenset("?+[]!")


class FilterOutcome(Enum):
    """Whether the diff triggers the workflow."""

    MATCHES = "MATCHES"
    NO_MATCH = "NO_MATCH"
    UNDECIDABLE = "UNDECIDABLE"


def _to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile(r"\A" + "".join(parts) + r"\Z")


def _supported(pattern: str) -> bool:
    return isinstance(pattern, str) and not (set(pattern) & UNSUPPORTED_PATTERN_CHARACTERS)


def _matches_any(patterns: list[str], changed_file: str) -> bool:
    return any(_to_regex(pattern).match(changed_file) for pattern in patterns)


def evaluate(path_filter: PathFilter | None, changed_files: tuple[str, ...]) -> FilterOutcome:
    """Decide whether ``changed_files`` triggers a workflow with this filter."""
    if path_filter is None:
        # No filter at all: the workflow always runs.
        return FilterOutcome.MATCHES
    if path_filter.mode == "unreadable":
        return FilterOutcome.UNDECIDABLE
    if not changed_files:
        # Without a diff there is nothing to decide against.
        return FilterOutcome.UNDECIDABLE

    supported = [p for p in path_filter.patterns if _supported(p)]
    has_unsupported = len(supported) != len(path_filter.patterns)

    if path_filter.mode == "paths":
        # The workflow runs if any changed file matches any pattern.
        if any(_matches_any(supported, changed) for changed in changed_files):
            return FilterOutcome.MATCHES
        return FilterOutcome.UNDECIDABLE if has_unsupported else FilterOutcome.NO_MATCH

    if path_filter.mode == "paths-ignore":
        # The workflow runs if any changed file is *not* ignored. An
        # uninterpretable pattern could be the one ignoring a file, so no
        # confident answer is possible.
        if has_unsupported:
            return FilterOutcome.UNDECIDABLE
        if any(not _matches_any(supported, changed) for changed in changed_files):
            return FilterOutcome.MATCHES
        return FilterOutcome.NO_MATCH

    return FilterOutcome.UNDECIDABLE
