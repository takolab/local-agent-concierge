"""Turn a fresh reviewer's raw output into labelled fields, or refuse to.

The same three rules as :mod:`review_loop.verdict_parser`, for the same
reasons, so an operator who has read one format has read all three:

* Only text between the ``BEGIN``/``END`` delimiters is parsed.
* A label counts only at column 0; an indented ``Problem:`` is content.
* An unrecognised label-shaped line at column 0 is an error, never content.

Two rules are specific to this contract, and both exist to keep the two kinds
of evidence apart at the *syntax* level rather than only at validation:

* There are **two block openers**, ``Finding ID`` for an original finding's
  resolution and ``Fresh finding ID`` for a finding raised by this turn. They
  are different words, not the same word in different places, so a reviewer
  cannot report a fresh finding as though it were a resolution -- or an
  original id as though it were newly discovered -- by accident.
* **Resolutions come first, then fresh findings.** Once a fresh finding block
  has opened, a ``Finding ID`` line is an error rather than a late
  resolution. The ordering costs the reviewer nothing and makes the section a
  line belongs to a property of the text, decidable without lookahead.

``Evidence`` is deliberately spelled the same in both kinds of block: it
means the same thing in both, and which block it lands in is decided by the
opener above it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rereview import RE_REVIEW_BEGIN, RE_REVIEW_END, ReReviewParseError

#: Envelope labels, and whether the value is a single line.
_ENVELOPE_LABELS: dict[str, bool] = {
    "Round": True,
    "Reviewed head SHA": True,
    "Recommendation": True,
    "Escalation reason": False,
}

#: Labels of one original finding's resolution block.
_RESOLUTION_LABELS: dict[str, bool] = {
    "Finding ID": True,
    "Resolution": True,
    "Evidence": False,
    "Reason": False,
}

#: Labels of one fresh finding block. The finding fields are the review
#: contract's own, so a fresh finding validates through the same rules.
_FRESH_LABELS: dict[str, bool] = {
    "Fresh finding ID": True,
    "Severity": True,
    "Location": True,
    "Problem": False,
    "Evidence": False,
    "Required outcome": False,
    "Scope boundary": False,
}

_RESOLUTION_OPENER = "Finding ID"
_FRESH_OPENER = "Fresh finding ID"

#: A line that looks like it is trying to be a label. Deliberately broader
#: than the known vocabulary so that a misspelled label fails loudly.
_LABEL_SHAPED = re.compile(r"\A([A-Za-z][A-Za-z ]{0,40}):[ \t]*(.*)\Z")


@dataclass
class RawBlock:
    """One resolution or fresh-finding block exactly as written."""

    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class RawReReview:
    """A parsed re-review block: labels and their text, nothing more."""

    envelope: dict[str, str] = field(default_factory=dict)
    resolutions: list[RawBlock] = field(default_factory=list)
    fresh: list[RawBlock] = field(default_factory=list)


def extract_block(output: str) -> str:
    """Return the text between the re-review delimiters.

    Both delimiters must appear exactly once. A second ``BEGIN`` would make
    "which block is the re-review?" a judgement call, and this parser does not
    make judgement calls.
    """
    if not output.strip():
        raise ReReviewParseError("the reviewer produced no output")

    lines = output.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == RE_REVIEW_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == RE_REVIEW_END]

    if not starts or not ends:
        raise ReReviewParseError(
            f"the reviewer output contains no {RE_REVIEW_BEGIN!r} / "
            f"{RE_REVIEW_END!r} block"
        )
    if len(starts) > 1 or len(ends) > 1:
        raise ReReviewParseError(
            "the reviewer output contains more than one re-review block, so which "
            "one is the re-review is undecidable"
        )
    if ends[0] < starts[0]:
        raise ReReviewParseError("the re-review block ends before it begins")

    return "\n".join(lines[starts[0] + 1 : ends[0]])


def parse(output: str) -> RawReReview:
    """Parse reviewer output into labelled fields, or raise."""
    block = extract_block(output)
    parsed = RawReReview()

    #: ``None`` while the envelope is open, then the block currently being
    #: filled. ``in_fresh`` records that the fresh section has started, which
    #: is what closes the resolution section for good.
    current: RawBlock | None = None
    current_labels: dict[str, bool] = _ENVELOPE_LABELS
    in_fresh = False
    current_label: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_label, buffer
        if current_label is None:
            return
        target = parsed.envelope if current is None else current.fields
        target[current_label] = "\n".join(buffer).strip()
        current_label, buffer = None, []

    for number, line in enumerate(block.splitlines(), start=1):
        match = _LABEL_SHAPED.match(line)
        label = match.group(1) if match else None

        if label is None:
            if current_label is None:
                if line.strip():
                    raise ReReviewParseError(
                        f"line {number} of the re-review block is not part of any "
                        f"field: {line.strip()[:80]!r}"
                    )
                continue
            buffer.append(line)
            continue

        known = (
            label in _ENVELOPE_LABELS
            or label in _RESOLUTION_LABELS
            or label in _FRESH_LABELS
        )
        if not known:
            raise ReReviewParseError(
                f"line {number} of the re-review block uses an unknown label "
                f"{label!r}; indent a line that begins with 'Word:' if it is "
                "part of a field's text"
            )

        if label == _RESOLUTION_OPENER:
            if in_fresh:
                raise ReReviewParseError(
                    f"line {number}: {label!r} opens a resolution for an original "
                    "finding, but the fresh-findings section has already begun; "
                    "report every resolution before the first fresh finding"
                )
            flush()
            current = RawBlock()
            current_labels = _RESOLUTION_LABELS
            parsed.resolutions.append(current)
        elif label == _FRESH_OPENER:
            flush()
            in_fresh = True
            current = RawBlock()
            current_labels = _FRESH_LABELS
            parsed.fresh.append(current)
        elif label in _ENVELOPE_LABELS:
            if current is not None:
                raise ReReviewParseError(
                    f"line {number}: {label!r} belongs to the re-review envelope but "
                    "appears after a finding block began"
                )
            flush()
        else:
            if current is None:
                raise ReReviewParseError(
                    f"line {number}: {label!r} belongs to a finding block but no "
                    f"{_RESOLUTION_OPENER!r} or {_FRESH_OPENER!r} line has opened one"
                )
            if label not in current_labels:
                other = (
                    "a resolution of an original finding"
                    if in_fresh
                    else "a fresh finding"
                )
                raise ReReviewParseError(
                    f"line {number}: {label!r} belongs to {other}, not to the block "
                    "it appears in"
                )
            flush()

        container = parsed.envelope if current is None else current.fields
        if label in container:
            raise ReReviewParseError(
                f"line {number}: {label!r} appears more than once in the same block"
            )

        current_label = label
        buffer = [match.group(2)]

        single_line = current_labels.get(label, _ENVELOPE_LABELS.get(label, False))
        if single_line:
            container[label] = buffer[0].strip()
            current_label, buffer = None, []

    flush()
    return parsed
