"""Turn a reviewer's raw output into labelled fields, or refuse to.

The format is the one this repository already uses by hand in its review
comments -- ``Label: value`` lines, with paragraph-shaped fields running to
the next label -- made explicit enough to parse without an LLM and without a
strict grammar that fights prose containing colons, code and indentation.

Three rules do the work:

* Only text between the ``BEGIN``/``END`` delimiters is parsed, so a reviewer
  may reason out loud without any of it reaching a validator or a comment.
* A label counts only at column 0. A ``Problem:`` inside an indented code
  snippet is content, not a field boundary.
* An unrecognised label-shaped line at column 0 is an error, never content.
  Silently absorbing ``Sevrity:`` into the previous paragraph would turn a
  typo into a missing-field rejection whose cause is invisible, and absorbing
  a genuinely unknown label would discard something the reviewer meant.

This module answers "what did the reviewer write?" only. Whether those fields
describe an admissible verdict is :mod:`review_loop.verdict_validation`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .verdict import VERDICT_BEGIN, VERDICT_END, VerdictParseError

#: Envelope labels, and whether the value is a single line.
_ENVELOPE_LABELS: dict[str, bool] = {
    "Round": True,
    "Reviewed head SHA": True,
    "Recommendation": True,
    "Resolved": True,
    "Escalation reason": False,
}

#: Per-finding labels. ``Finding ID`` opens a new finding block.
_FINDING_LABELS: dict[str, bool] = {
    "Finding ID": True,
    "Severity": True,
    "Location": True,
    "Problem": False,
    "Evidence": False,
    "Required outcome": False,
    "Scope boundary": False,
}

_FINDING_OPENER = "Finding ID"

#: A line that looks like it is trying to be a label. Deliberately broader
#: than the known vocabulary so that a misspelled label fails loudly.
_LABEL_SHAPED = re.compile(r"\A([A-Za-z][A-Za-z ]{0,40}):(?: (.*))?\Z")


@dataclass
class RawFinding:
    """One finding block exactly as written, before any interpretation."""

    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class RawVerdict:
    """A parsed verdict block: labels and their text, nothing more."""

    envelope: dict[str, str] = field(default_factory=dict)
    findings: list[RawFinding] = field(default_factory=list)


def extract_block(output: str) -> str:
    """Return the text between the verdict delimiters.

    Both delimiters must appear exactly once. A second ``BEGIN`` would make
    "which block is the verdict?" a judgement call, and this parser does not
    make judgement calls.
    """
    if not output.strip():
        raise VerdictParseError("the reviewer produced no output")

    lines = output.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == VERDICT_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == VERDICT_END]

    if not starts or not ends:
        raise VerdictParseError(
            f"the reviewer output contains no {VERDICT_BEGIN!r} / {VERDICT_END!r} block"
        )
    if len(starts) > 1 or len(ends) > 1:
        raise VerdictParseError(
            "the reviewer output contains more than one verdict block, so which "
            "one is the verdict is undecidable"
        )
    if ends[0] < starts[0]:
        raise VerdictParseError("the verdict block ends before it begins")

    return "\n".join(lines[starts[0] + 1 : ends[0]])


def parse(output: str) -> RawVerdict:
    """Parse reviewer output into labelled fields, or raise."""
    block = extract_block(output)
    verdict = RawVerdict()

    current_finding: RawFinding | None = None
    current_label: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_label, buffer
        if current_label is None:
            return
        target = verdict.envelope if current_finding is None else current_finding.fields
        target[current_label] = "\n".join(buffer).strip()
        current_label, buffer = None, []

    for number, line in enumerate(block.splitlines(), start=1):
        match = _LABEL_SHAPED.match(line)
        label = match.group(1) if match else None

        if label is None:
            if current_label is None:
                if line.strip():
                    raise VerdictParseError(
                        f"line {number} of the verdict block is not part of any "
                        f"field: {line.strip()[:80]!r}"
                    )
                continue
            buffer.append(line)
            continue

        if label not in _ENVELOPE_LABELS and label not in _FINDING_LABELS:
            raise VerdictParseError(
                f"line {number} of the verdict block uses an unknown label "
                f"{label!r}; indent a line that begins with 'Word:' if it is "
                "part of a field's text"
            )

        if label == _FINDING_OPENER:
            flush()
            current_finding = RawFinding()
            verdict.findings.append(current_finding)
        elif label in _ENVELOPE_LABELS:
            if current_finding is not None:
                raise VerdictParseError(
                    f"line {number}: {label!r} belongs to the verdict envelope but "
                    "appears after a finding block began"
                )
            flush()
        else:
            if current_finding is None:
                raise VerdictParseError(
                    f"line {number}: {label!r} belongs to a finding but no "
                    f"{_FINDING_OPENER!r} line has opened one"
                )
            flush()

        container = verdict.envelope if current_finding is None else current_finding.fields
        if label in container:
            raise VerdictParseError(
                f"line {number}: {label!r} appears more than once in the same block"
            )

        current_label = label
        buffer = [match.group(2) or ""]

        single_line = _ENVELOPE_LABELS.get(label, _FINDING_LABELS.get(label, False))
        if single_line:
            container[label] = buffer[0].strip()
            current_label, buffer = None, []

    flush()
    return verdict
