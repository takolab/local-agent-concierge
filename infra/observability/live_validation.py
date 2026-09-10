"""Read-only evidence helper for the Slack -> Orchestrator -> Hermes live
validation (docs/observability/slack-orchestrator-live-validation.md).

This collects and formats evidence a Human inspects. It is deliberately
incapable of driving the validation:

- It sends no Slack message, dispatches no `AgentRequest`, and calls no
  Agent. The only HTTP it performs is `GET` against Phoenix's read API and
  a liveness endpoint.
- It starts, stops, restarts and recreates nothing. `docker inspect` and
  `docker compose ps` are the only container commands used.
- It never prints a sentinel value. `scan` reports `absent` / `LEAKED`
  per label, so the runbook can check a real credential or a real Slack
  identifier against exported telemetry without that value being echoed
  into a terminal, a CI log, or a pasted evidence record.

Standard library only, matching the other stdlib-only tooling in this
repository. Its pure logic (span-tree building, expected-chain checking,
needle parsing and scanning) is unit-tested in
`infra/observability/tests/test_live_validation.py` with fixtures -- those
tests perform no network or Docker access, and they do not stand in for
live evidence.

Usage:

    python3 infra/observability/live_validation.py provenance
    python3 infra/observability/live_validation.py trace <TRACE_ID>
    python3 infra/observability/live_validation.py scan <TRACE_ID> \\
        --needles-file <path> [--env HERMES_API_SERVER_KEY]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]

PHOENIX_BASE_URL = "http://127.0.0.1:6006"

# Must match `x-project-name` in infra/observability/otel-collector.yaml.
# Asserted against that file by the tests, so a rename there fails here
# rather than silently querying an empty project.
PHOENIX_PROJECT = "local-agent-concierge-infra-smoke-test"

# The Compose services whose identity is worth pinning to an evidence
# record. Ordered from the Slack entry point down.
PROVENANCE_SERVICES = (
    "slack-gateway",
    "orchestrator",
    "hermes-agent",
    "ollama",
    "otel-collector",
)

# The expected span chain for one Slack request, by name, parent first.
# These are the names the code actually emits:
#   concierge.request, orchestrator.dispatch  -> slack_gateway.telemetry
#   POST /dispatch, hermes.request            -> orchestrator.telemetry
#   /v1/responses  -> Hermes Agent's aiohttp auto-instrumentation
EXPECTED_CHAIN = (
    "concierge.request",
    "orchestrator.dispatch",
    "POST /dispatch",
    "hermes.request",
    "/v1/responses",
)


class Span(NamedTuple):
    name: str
    span_id: str
    parent_id: str | None
    start_time: str
    attributes: dict[str, Any]


class TreeRow(NamedTuple):
    depth: int
    span: Span


class ChainResult(NamedTuple):
    relationship: str
    ok: bool
    detail: str


class ScanResult(NamedTuple):
    label: str
    found: bool


def parse_spans(payload: Any) -> list[Span]:
    """Normalize Phoenix's `/v1/projects/{p}/spans` response into Spans.

    Tolerates a missing `attributes` or `parent_id` rather than raising:
    the point of this helper is to report what arrived, and a span that
    arrived in an unexpected shape is itself evidence.
    """
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    spans: list[Span] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        context = row.get("context") or {}
        spans.append(
            Span(
                name=str(row.get("name", "<unnamed>")),
                span_id=str(context.get("span_id", "")),
                parent_id=row.get("parent_id") or None,
                start_time=str(row.get("start_time", "")),
                attributes=row.get("attributes") or {},
            )
        )

    return spans


def build_span_tree(spans: Iterable[Span]) -> list[TreeRow]:
    """Order spans by start time and compute each one's nesting depth.

    Depth is resolved by walking `parent_id` links. A parent that is not
    present in this set (a span from another trace, or one that never
    reached Phoenix) terminates the walk, so its child renders at the
    depth it can be proven to have rather than being dropped. A cyclic
    parent chain -- which no correct exporter produces, but which must not
    hang an evidence tool -- is bounded by the number of spans.
    """
    ordered = sorted(spans, key=lambda span: (span.start_time, span.name))
    by_id = {span.span_id: span for span in ordered if span.span_id}

    rows: list[TreeRow] = []
    for span in ordered:
        depth = 0
        seen: set[str] = {span.span_id}
        parent = span.parent_id

        while parent and parent in by_id and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = by_id[parent].parent_id

        rows.append(TreeRow(depth=depth, span=span))

    return rows


def check_expected_chain(
    spans: Iterable[Span],
    expected: Iterable[str] = EXPECTED_CHAIN,
) -> list[ChainResult]:
    """Check each expected parent -> child link by span name.

    Reports one result per link. A missing span and a present-but-wrongly
    parented span are distinguished, because they mean different things: the
    first says a hop did not emit, the second says trace context did not
    continue across it.

    Name-based on purpose -- the runbook's reader is checking the shape of
    a known path, not discovering an unknown one. A duplicated span name
    inside one trace would make this ambiguous; that is reported rather
    than guessed at.
    """
    spans = list(spans)
    expected = list(expected)
    results: list[ChainResult] = []

    by_name: dict[str, list[Span]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)

    for parent_name, child_name in zip(expected, expected[1:]):
        relationship = f"{parent_name} -> {child_name}"
        parents = by_name.get(parent_name, [])
        children = by_name.get(child_name, [])

        if not parents or not children:
            missing = [
                name
                for name, found in (
                    (parent_name, parents),
                    (child_name, children),
                )
                if not found
            ]
            results.append(
                ChainResult(relationship, False, f"missing span(s): {missing}")
            )
            continue

        if len(parents) > 1 or len(children) > 1:
            results.append(
                ChainResult(
                    relationship,
                    False,
                    "ambiguous: more than one span with this name in the trace",
                )
            )
            continue

        parent, child = parents[0], children[0]
        if child.parent_id == parent.span_id:
            results.append(ChainResult(relationship, True, "parented correctly"))
        else:
            results.append(
                ChainResult(
                    relationship,
                    False,
                    f"child's parent_id is {child.parent_id!r}, "
                    f"expected {parent.span_id!r}",
                )
            )

    return results


def parse_needles(text: str) -> dict[str, str]:
    """Parse a `label=value` needles file.

    Blank lines and `#` comments are ignored. The value may contain `=`.
    Raises ValueError on a malformed line rather than skipping it -- a
    silently dropped needle would turn a missed leak into a clean report.
    """
    needles: dict[str, str] = {}

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        label, separator, value = line.partition("=")
        if not separator or not label.strip() or not value.strip():
            raise ValueError(
                f"line {number} is not `label=value`: {line[:20]!r}..."
            )

        needles[label.strip()] = value.strip()

    return needles


def scan_for_needles(payload_text: str, needles: dict[str, str]) -> list[ScanResult]:
    """Report which needles appear in `payload_text`, never the values.

    Case-insensitive, because identifiers and hex digests can be
    re-cased in transit; a case-only difference would still be a leak.
    """
    haystack = payload_text.lower()

    return [
        ScanResult(label=label, found=value.lower() in haystack)
        for label, value in sorted(needles.items())
    ]


def _run(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"<unavailable: {type(error).__name__}>"

    if completed.returncode != 0:
        return "<unavailable>"

    return completed.stdout.strip()


def _fetch_trace_payload(trace_id: str) -> str:
    query = urllib.parse.urlencode({"trace_id": trace_id, "limit": 200})
    url = (
        f"{PHOENIX_BASE_URL}/v1/projects/"
        f"{urllib.parse.quote(PHOENIX_PROJECT)}/spans?{query}"
    )

    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read().decode("utf-8")


def command_provenance(_: argparse.Namespace) -> int:
    """Print what pins this run to a repository state and a set of images."""
    sha = _run(["git", "rev-parse", "HEAD"])
    dirty = _run(["git", "status", "--porcelain"])

    print("repository")
    print(f"  SHA           {sha}")
    print(
        "  working tree  "
        + ("clean" if dirty == "" else f"DIRTY ({len(dirty.splitlines())} files)")
    )
    print()
    print("containers")

    for service in PROVENANCE_SERVICES:
        container = _run(["docker", "compose", "ps", "-q", service])
        if not container or container.startswith("<"):
            print(f"  {service:<16} <not running>")
            continue

        image = _run(
            ["docker", "inspect", container, "--format", "{{.Image}}"]
        )
        started = _run(
            ["docker", "inspect", container, "--format", "{{.State.StartedAt}}"]
        )
        print(f"  {service:<16} image={image[:26]}  started={started[:19]}")

    print()
    print(
        "note: `image` is a local image ID, not a registry digest. Locally "
        "built images\n      (slack-gateway, orchestrator) have no digest "
        "and no mechanical link to a\n      source commit -- the repository "
        "SHA above plus a clean tree is what ties\n      them to source. See "
        "the runbook's 'Exact Runtime Provenance' section."
    )
    return 0


def command_trace(args: argparse.Namespace) -> int:
    """Print one trace's span tree and check the expected chain."""
    try:
        payload_text = _fetch_trace_payload(args.trace_id)
    except OSError as error:
        print(f"could not reach Phoenix at {PHOENIX_BASE_URL}: {error}")
        return 2

    spans = parse_spans(json.loads(payload_text))

    if not spans:
        print(f"no spans found for trace {args.trace_id}")
        return 1

    print(f"trace {args.trace_id}  ({len(spans)} spans)")
    print()

    for row in build_span_tree(spans):
        indent = "  " * row.depth
        print(
            f"  {indent}{row.span.name:<26} "
            f"span={row.span.span_id[:12]} parent={(row.span.parent_id or '-')[:12]}"
        )

    print()
    print("expected chain")
    failures = 0
    for result in check_expected_chain(spans):
        mark = "OK  " if result.ok else "FAIL"
        if not result.ok:
            failures += 1
        print(f"  [{mark}] {result.relationship:<48} {result.detail}")

    print()
    print("attributes")
    for row in build_span_tree(spans):
        keys = ", ".join(sorted(row.span.attributes))
        print(f"  {row.span.name:<26} {keys}")

    return 1 if failures else 0


def command_scan(args: argparse.Namespace) -> int:
    """Check whether sentinel values reached Phoenix. Values are never printed."""
    needles: dict[str, str] = {}

    if args.needles_file:
        needles.update(parse_needles(Path(args.needles_file).read_text()))

    for name in args.env or []:
        value = os.environ.get(name)
        if not value:
            print(f"  {name:<32} <not set in this environment; not checked>")
            continue
        needles[name] = value

    if not needles:
        print("no needles supplied -- pass --needles-file and/or --env")
        return 2

    try:
        payload_text = _fetch_trace_payload(args.trace_id)
    except OSError as error:
        print(f"could not reach Phoenix at {PHOENIX_BASE_URL}: {error}")
        return 2

    print(f"trace {args.trace_id}  --  {len(needles)} sentinel(s)")
    print()

    leaked = 0
    for result in scan_for_needles(payload_text, needles):
        if result.found:
            leaked += 1
        print(f"  {'LEAKED' if result.found else 'absent':>7}  {result.label}")

    print()
    print(
        f"{leaked} leaked / {len(needles)} checked. "
        "Values are never printed by this tool."
    )
    return 1 if leaked else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only evidence helper for the Slack -> Orchestrator -> "
            "Hermes live validation. Sends no Slack message and invokes no "
            "Agent."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    provenance = subparsers.add_parser(
        "provenance", help="repository SHA and running container image IDs"
    )
    provenance.set_defaults(func=command_provenance)

    trace = subparsers.add_parser(
        "trace", help="span tree and expected-chain check for one trace"
    )
    trace.add_argument("trace_id")
    trace.set_defaults(func=command_trace)

    scan = subparsers.add_parser(
        "scan", help="check sentinel values against a trace (values never printed)"
    )
    scan.add_argument("trace_id")
    scan.add_argument(
        "--needles-file",
        help="file of `label=value` lines to search for",
    )
    scan.add_argument(
        "--env",
        action="append",
        help="environment variable name whose value to search for (repeatable)",
    )
    scan.set_defaults(func=command_scan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
