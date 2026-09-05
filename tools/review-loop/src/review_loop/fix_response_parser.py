"""Turn a Coding Agent's raw output into labelled fields, or refuse to.

The same three rules as :mod:`review_loop.verdict_parser`, for the same
reasons, so an operator who has read one format has read both:

* Only text between the ``BEGIN``/``END`` delimiters is parsed, so an agent
  may narrate its work without any of it reaching a validator.
* A label counts only at column 0. A ``Summary:`` inside an indented snippet
  is content, not a field boundary.
* An unrecognised label-shaped line at column 0 is an error, never content.

One rule differs, and it is the reason this is a separate parser rather than
a parameter on the other one: **a fix turn produces several blocks**, one per
routed finding, and their order is the order the agent chose. A verdict is
one block or it is undecidable; a fix response is a sequence, and an empty
sequence is a failure rather than an empty answer.

``Files changed`` is the only list-valued field in either contract. It is
parsed strictly -- ``- path`` per line, or an explicit ``(none)`` -- because
it is the field the working-tree cross-check is run against, and a lenient
reading of it would let a hidden edit through as a formatting quirk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .fix_response import (
    FIX_RESPONSE_BEGIN,
    FIX_RESPONSE_END,
    FixResponseParseError,
)

#: Block labels, and whether the value is a single line.
_LABELS: dict[str, bool] = {
    "Finding ID": True,
    "Target head SHA": True,
    "Outcome": True,
    "Files changed": False,
    "Verification": False,
    "Summary": False,
    "Reason": False,
    "Scope notes": False,
}

#: A line that looks like it is trying to be a label. Deliberately broader
#: than the known vocabulary so that a misspelled label fails loudly.
_LABEL_SHAPED = re.compile(r"\A([A-Za-z][A-Za-z ]{0,40}):[ \t]*(.*)\Z")

#: A line of the ``Files changed`` list.
_LIST_ITEM = re.compile(r"\A[-*][ \t]+(.+?)[ \t]*\Z")

#: Spellings of "no files", accepted as the whole value and nothing else.
_EMPTY_LIST_VALUES = frozenset({"", "-", "none", "(none)", "n/a"})


@dataclass
class RawFixResponse:
    """One response block exactly as written, before any interpretation."""

    fields: dict[str, str] = field(default_factory=dict)


def extract_blocks(output: str) -> list[str]:
    """Return the text inside each response block, in the order written.

    Delimiters must alternate. A ``BEGIN`` inside a block, or an ``END``
    without one, makes "where does this response stop?" a judgement call, and
    this parser does not make judgement calls.
    """
    if not output.strip():
        raise FixResponseParseError("the coding agent produced no output")

    blocks: list[str] = []
    current: list[str] | None = None

    for number, line in enumerate(output.splitlines(), start=1):
        stripped = line.strip()
        if stripped == FIX_RESPONSE_BEGIN:
            if current is not None:
                raise FixResponseParseError(
                    f"line {number}: a fix response block begins while another is "
                    "still open"
                )
            current = []
            continue
        if stripped == FIX_RESPONSE_END:
            if current is None:
                raise FixResponseParseError(
                    f"line {number}: a fix response block ends without having begun"
                )
            blocks.append("\n".join(current))
            current = None
            continue
        if current is not None:
            current.append(line)

    if current is not None:
        raise FixResponseParseError(
            f"a fix response block was opened but never closed with "
            f"{FIX_RESPONSE_END!r}"
        )
    if not blocks:
        raise FixResponseParseError(
            f"the coding agent output contains no {FIX_RESPONSE_BEGIN!r} / "
            f"{FIX_RESPONSE_END!r} block"
        )
    return blocks


def parse_files_changed(value: str) -> tuple[str, ...]:
    """Read the ``Files changed`` list, or refuse the whole response.

    Accepts ``- path`` lines, or one of the explicit "nothing" spellings as
    the entire value. A bare path with no marker is rejected: it is
    indistinguishable from a wrapped continuation of the previous line, and
    guessing which one it is would be guessing about the set of files this
    runner is about to hold the agent to.
    """
    text = value.strip()
    if text.lower() in _EMPTY_LIST_VALUES:
        return ()

    paths: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = _LIST_ITEM.match(line.strip())
        if match is None:
            raise FixResponseParseError(
                f"line {number} of 'Files changed' is {line.strip()[:80]!r}; each "
                "entry must be written as '- path/to/file', or the whole value as "
                "'(none)'"
            )
        paths.append(match.group(1))
    if not paths:
        raise FixResponseParseError(
            "'Files changed' has a value but lists no path; write '(none)' to "
            "report that nothing changed"
        )
    return tuple(paths)


def parse_block(block: str, *, where: str) -> RawFixResponse:
    """Parse one block's labelled fields."""
    response = RawFixResponse()
    current_label: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_label, buffer
        if current_label is None:
            return
        response.fields[current_label] = "\n".join(buffer).strip()
        current_label, buffer = None, []

    for number, line in enumerate(block.splitlines(), start=1):
        match = _LABEL_SHAPED.match(line)
        label = match.group(1) if match else None

        if label is None:
            if current_label is None:
                if line.strip():
                    raise FixResponseParseError(
                        f"{where}, line {number} is not part of any field: "
                        f"{line.strip()[:80]!r}"
                    )
                continue
            buffer.append(line)
            continue

        if label not in _LABELS:
            raise FixResponseParseError(
                f"{where}, line {number} uses an unknown label {label!r}; indent a "
                "line that begins with 'Word:' if it is part of a field's text"
            )
        # Flushed before the duplicate check, not after: an unflushed
        # multi-line field is not yet in ``fields``, so checking first would
        # miss two consecutive ``Summary:`` lines -- the exact shape an agent
        # produces when it restates an answer.
        flush()
        if label in response.fields:
            raise FixResponseParseError(
                f"{where}, line {number}: {label!r} appears more than once in the "
                "same block"
            )

        current_label = label
        buffer = [match.group(2)]

        if _LABELS[label]:
            response.fields[label] = buffer[0].strip()
            current_label, buffer = None, []

    flush()
    return response


def parse(output: str) -> list[RawFixResponse]:
    """Parse coding agent output into one raw response per block, or raise."""
    return [
        parse_block(block, where=f"fix response {index}")
        for index, block in enumerate(extract_blocks(output), start=1)
    ]
