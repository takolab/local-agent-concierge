"""Decide whether a pull request's diff triggers a path-filtered workflow.

This answers one question and refuses the rest: *given these changed files,
must this workflow have run?* It is only ever consulted when a path-filtered
workflow produced no run, because a run that exists needs no explaining.

The invariant is narrower than "support GitHub's glob syntax": it is *never
report NO_MATCH unless GitHub would certainly agree*. A false ``NO_MATCH``
explains away a workflow that should have run, which is exactly how a false
``READY`` gets produced.

So only pattern shapes whose meaning is settled by GitHub's own documented
examples are decided:

* no ``**`` at all -- ``*`` matches within one path segment
* a literal prefix followed by a trailing ``/**`` (``services/orchestrator/**``)

Everything else is undecidable, including ``**`` in any interior or leading
position such as ``docs/**/*.md``. GitHub does document these richer forms --
``docs/**/*.md`` matches ``docs/README.md`` with zero intervening directories,
and ``**/README.md`` matches a root-level ``README.md`` -- but this first slice
deliberately models only the narrower subset this repository actually uses.
Modelling more would mean more matcher surface to get wrong, and a wrong
``NO_MATCH`` is the expensive direction. ``?``, ``+``, ``[]`` and a leading
``!`` are likewise undecidable.

Extending the subset later is a deliberate change, made against fixtures drawn
from GitHub's documented examples rather than from a reading of its syntax.
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


#: The one ``**`` shape with a settled meaning: everything beneath a literal
#: directory prefix, as in ``services/orchestrator/**``.
_TRAILING_GLOBSTAR = "/**"


def _to_regex(pattern: str) -> re.Pattern[str] | None:
    """Compile a pattern, or return ``None`` if its meaning is not settled."""
    if pattern.endswith(_TRAILING_GLOBSTAR):
        prefix = pattern[: -len(_TRAILING_GLOBSTAR)]
        if "*" in prefix:
            return None
        return re.compile(r"\A" + re.escape(prefix + "/") + r".*\Z")

    if "**" in pattern:
        # Interior or leading ``**``: not covered by any documented example.
        return None

    parts = ["[^/]*" if character == "*" else re.escape(character) for character in pattern]
    return re.compile(r"\A" + "".join(parts) + r"\Z")


def _supported(pattern: str) -> bool:
    if not isinstance(pattern, str):
        return False
    if set(pattern) & UNSUPPORTED_PATTERN_CHARACTERS:
        return False
    return _to_regex(pattern) is not None


def _matches_any(patterns: list[str], changed_file: str) -> bool:
    compiled = (_to_regex(pattern) for pattern in patterns)
    return any(regex.match(changed_file) for regex in compiled if regex is not None)


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
