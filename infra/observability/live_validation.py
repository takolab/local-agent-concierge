"""Read-only evidence helper for the Slack -> Orchestrator -> Hermes live
validation (docs/observability/slack-orchestrator-live-validation.md).

This collects and formats evidence a Human inspects. It is deliberately
incapable of driving the validation:

- It sends no Slack message, dispatches no `AgentRequest`, and calls no
  Agent. The only HTTP it performs is `GET` against Phoenix's read API.
  (The runbook's Orchestrator `/health` liveness check is a separate
  Human-run command, deliberately not part of this tool's surface.)
- It starts, stops, restarts and recreates nothing. `docker inspect` and
  `docker compose ps` are the only container commands used.
- It never prints a sentinel value. `scan` reports `absent` / `LEAKED`
  per label, so the runbook can check a real credential or a real Slack
  identifier against exported telemetry without that value being echoed
  into a terminal, a CI log, or a pasted evidence record.

Standard library only, matching the other stdlib-only tooling in this
repository. Its pure logic (span-tree building, expected-relationship
checking, needle parsing and scanning) is unit-tested in
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
# Every service whose identity an evidence record depends on -- including
# `phoenix` and `mlflow`, which are where the evidence is *read from*: a
# record that pins the producers but not the backends cannot later be
# correlated against what those backends held at the time.
PROVENANCE_SERVICES = (
    "slack-gateway",
    "orchestrator",
    "hermes-agent",
    "google-calendar-mcp",
    "ollama",
    "otel-collector",
    "phoenix",
    "mlflow",
)

# Every parent -> child relationship one successful Slack request is
# expected to produce, by span name. These are the names the code actually
# emits:
#   concierge.request, orchestrator.dispatch, slack.response
#                                             -> slack_gateway.telemetry
#   POST /dispatch, hermes.request            -> orchestrator.telemetry
#   /v1/responses  -> Hermes Agent's aiohttp auto-instrumentation
#
# Modelled as relationships rather than one linear chain because the trace
# is not linear: `slack.response` is a second child of `concierge.request`,
# not a descendant of the dispatch. A linear chain silently ignored it, so a
# trace where the Slack reply never emitted could still report every link OK
# -- a false PASS against the runbook's "one trace contains all six expected
# spans". Every expected span appears in at least one pair here, so
# requiring all relationships also requires all six spans.
EXPECTED_RELATIONSHIPS = (
    ("concierge.request", "orchestrator.dispatch"),
    ("orchestrator.dispatch", "POST /dispatch"),
    ("POST /dispatch", "hermes.request"),
    ("hermes.request", "/v1/responses"),
    ("concierge.request", "slack.response"),
)

EXPECTED_SPAN_NAMES = tuple(
    dict.fromkeys(
        name for relationship in EXPECTED_RELATIONSHIPS for name in relationship
    )
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


class RelationshipResult(NamedTuple):
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
    parent-link loop -- which no correct exporter produces, but
    which must not hang an evidence tool -- is bounded by the number of
    spans. (This walk is about resolving one span's depth; it is unrelated
    to `EXPECTED_RELATIONSHIPS`, which is the shape the trace is checked
    against.)
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


def check_expected_relationships(
    spans: Iterable[Span],
    expected: Iterable[tuple[str, str]] = EXPECTED_RELATIONSHIPS,
) -> list[RelationshipResult]:
    """Check each expected parent -> child relationship by span name.

    Reports one result per relationship. A missing span and a
    present-but-wrongly parented span are distinguished, because they mean
    different things: the first says a hop did not emit, the second says
    trace context did not continue across it.

    Name-based on purpose -- the runbook's reader is checking the shape of
    a known path, not discovering an unknown one. A duplicated span name
    inside one trace would make this ambiguous; that is reported rather
    than guessed at.
    """
    spans = list(spans)
    results: list[RelationshipResult] = []

    by_name: dict[str, list[Span]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)

    for parent_name, child_name in expected:
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
                RelationshipResult(relationship, False, f"missing span(s): {missing}")
            )
            continue

        if len(parents) > 1 or len(children) > 1:
            results.append(
                RelationshipResult(
                    relationship,
                    False,
                    "ambiguous: more than one span with this name in the trace",
                )
            )
            continue

        parent, child = parents[0], children[0]
        if child.parent_id == parent.span_id:
            results.append(RelationshipResult(relationship, True, "parented correctly"))
        else:
            results.append(
                RelationshipResult(
                    relationship,
                    False,
                    f"child's parent_id is {child.parent_id!r}, "
                    f"expected {parent.span_id!r}",
                )
            )

    return results


class NeedlesFileError(ValueError):
    """A needles file that cannot be used as supplied.

    Its message names the offending line *number* and nothing else --
    never the line, a fragment of it, or the value. A malformed entry is
    exactly where a secret is most likely to be sitting (a pasted value
    with no label, a stray `=`), so quoting the input to be helpful would
    print the thing this tool exists to keep out of terminals, tracebacks
    and pasted evidence records.
    """


def parse_needles(text: str) -> dict[str, str]:
    """Parse a `label=value` needles file.

    Blank lines and `#` comments are ignored. The value may contain `=`.

    Two things are errors rather than best-effort recoveries, both because
    the failure mode is a clean report on an unchecked sentinel:

    - a malformed line, which would otherwise be skipped;
    - a duplicate label, which would otherwise overwrite the earlier value
      and silently drop it from the scan.
    """
    needles: dict[str, str] = {}

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        label, separator, value = line.partition("=")
        label = label.strip()

        if not separator or not label or not value.strip():
            raise NeedlesFileError(
                f"line {number} is not a valid `label=value` entry"
            )

        if label in needles:
            raise NeedlesFileError(
                f"line {number} repeats a label used earlier; "
                "every sentinel needs its own label, or one of them is "
                "never checked"
            )

        needles[label] = value.strip()

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
        print(
            f"  {service:<16} container[:{MIN_CONTAINER_PREFIX}]="
            f"{container[:MIN_CONTAINER_PREFIX]}  "
            f"image={image[:26]}  started={started[:19]}"
        )

    print()
    print(
        # Deliberately does not restate §3's rules. An earlier version of
        # this note claimed the repository SHA plus a clean tree ties a
        # local image to its source; §3 retired that, because a clean
        # checkout says nothing about what the running image was built
        # from -- a case this stack actually exhibited. Restating rules
        # here just creates a second copy to drift.
        "note: a local image ID does not establish source provenance. For "
        "slack-gateway\n      and orchestrator, run the recorded-SHA source "
        "comparison in the runbook's\n      'Exact Runtime Provenance' "
        "section before claiming a source match. The other\n      services "
        "have different identity semantics -- see the same section.\n\n"
        f"      `container[:{MIN_CONTAINER_PREFIX}]` is an ID *prefix*, "
        "which is what `--expect-container`\n      compares against. "
        "Record the orchestrator one and pass it to `scan`: without it\n"
        "      the credential is read from whatever is running when the "
        "scan happens, not\n      from the instance that handled the "
        "request. `scan` refuses to run unbound."
    )
    return 0


def command_trace(args: argparse.Namespace) -> int:
    """Print one trace's span tree and check the expected relationships."""
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
    print("expected relationships")
    failures = 0
    for result in check_expected_relationships(spans):
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


class Unresolved(NamedTuple):
    name: str
    reason: str


def resolve_env_needles(
    names: Iterable[str],
    environ: dict[str, str],
    env_file_text: str | None = None,
    service_values: dict[str, str] | None = None,
    service_name: str | None = None,
    service_problem: str | None = None,
) -> tuple[dict[str, str], list[Unresolved]]:
    """Resolve `--env NAME` values, returning (resolved, unresolved).

    **When `service_name` is given, that source is exclusive.** The running
    container's environment is the credential this stack is actually using;
    if it cannot be read, the honest outcome is "not checked", not a
    quietly substituted value from somewhere else. Falling back would let
    an unreadable container degrade the check to a stale host value --
    scanning `OLD_SECRET`, reporting it absent, and exiting 0 while the
    `NEW_SECRET` the container holds is the one that leaked. A
    lower-authority answer is worse than no answer here, because only one
    of them is visibly incomplete.

    With no service requested, resolution falls back in order: the process
    environment, then an env-file parsed by `parse_env_file` (which
    refuses any value whose Compose semantics this tool cannot reproduce).

    Unresolved names are *returned* with a reason, never skipped. The
    caller must fail on them: a sentinel that was not checked cannot
    support "every sensitive sentinel reports absent", and reporting it as
    a clean run would be a false PASS. See `command_scan`.
    """
    file_values, rejected = (
        parse_env_file(env_file_text) if env_file_text else ({}, {})
    )
    service_values = service_values or {}

    resolved: dict[str, str] = {}
    unresolved: list[Unresolved] = []

    for name in names:
        if service_name is not None:
            value = service_values.get(name)
            if value:
                resolved[name] = value
            else:
                unresolved.append(
                    Unresolved(
                        name,
                        service_problem
                        or (
                            f"not present in the running {service_name!r} "
                            "container's environment (no fallback is used "
                            "when --env-from-service is given)"
                        ),
                    )
                )
            continue

        value = environ.get(name) or file_values.get(name)
        if value:
            resolved[name] = value
        elif name in rejected:
            unresolved.append(Unresolved(name, rejected[name]))
        else:
            unresolved.append(Unresolved(name, "not found"))

    return resolved, unresolved


# dotenv constructs whose Compose semantics this tool does not reproduce.
# A value containing any of them is REJECTED rather than parsed, because
# parsing it would scan a string that is not what the container received:
# `KEY=${BASE}` reads back as the literal "${BASE}" here while Compose
# injects the expansion, so a leak of the real value would scan as absent.
#
# Rejected, not "best effort": the failure mode of guessing is a clean
# report on an unchecked credential, which is exactly what this tool exists
# to prevent.
_UNSUPPORTED_VALUE_MARKERS = (
    ("$", "interpolation (${...} or $NAME) -- Compose expands this, this tool does not"),
    ("\\", "backslash escape -- Compose's unescaping is not reproduced here"),
    ("#", "possible inline comment -- Compose may strip it, this tool does not"),
    ("`", "command substitution syntax"),
)


def parse_env_file(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read `NAME=value` lines, returning (usable values, rejected reasons).

    A value is usable only when this tool can prove it interprets the line
    the same way Docker Compose does: a plain literal, optionally wrapped
    in one matching pair of quotes, containing none of
    `_UNSUPPORTED_VALUE_MARKERS`. Everything else is reported as rejected,
    with a reason that never quotes the value.

    An `export ` prefix is rejected for the same reason -- Compose accepts
    it, so silently treating the name as "export FOO" would make a real
    sentinel look absent.
    """
    values: dict[str, str] = {}
    rejected: dict[str, str] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        name, separator, value = line.partition("=")
        if not separator:
            continue

        name = name.strip()

        if name.startswith("export ") or " " in name:
            rejected[name.removeprefix("export ").strip()] = (
                "`export` prefix or whitespace in the name is not interpreted here"
            )
            continue

        value = value.strip()

        quoted = (
            len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        )
        inner = value[1:-1] if quoted else value

        marker_reason = next(
            (
                reason
                for marker, reason in _UNSUPPORTED_VALUE_MARKERS
                if marker in inner
            ),
            None,
        )

        if marker_reason is not None:
            rejected[name] = marker_reason
            continue

        if inner:
            values[name] = inner

    return values, rejected


class ServiceLookup(NamedTuple):
    values: dict[str, str]
    container_id: str | None
    problem: str | None


# Shortest `--expect-container` prefix accepted. Twelve hex characters is
# what `docker ps` shows and is unambiguous in practice; anything shorter
# could match a container other than the recorded one, which is the
# opposite of what binding is for.
MIN_CONTAINER_PREFIX = 12


def service_environment(
    service: str,
    expected_container: str | None = None,
) -> ServiceLookup:
    """Read a running Compose service's actual environment.

    This is the ground truth for "what did Docker Compose inject": the
    container's own `Config.Env`, after all dotenv interpolation Compose
    performed. Nothing is printed -- the values are returned for use as
    scan needles only.

    `expected_container` binds the read to a specific container id, and
    exists because "authoritative" and "the same instance" are different
    properties. The runbook records provenance *before* the Slack request
    and scans *after* it, so a service recreated in between resolves to a
    new container holding a new credential: scanning that one finds it
    absent and reports clean while the credential the request actually
    used is the one that leaked. A mismatch is a problem, never a silent
    substitution.

    Every failure path returns a `problem` rather than empty values, so
    the caller can say which one occurred instead of reporting a generic
    "not found".
    """
    container = _run(["docker", "compose", "ps", "-q", service])
    if not container or container.startswith("<"):
        return ServiceLookup({}, None, f"the {service!r} service is not running")

    if expected_container is not None:
        if len(expected_container) < MIN_CONTAINER_PREFIX:
            return ServiceLookup(
                {},
                container,
                f"--expect-container needs at least {MIN_CONTAINER_PREFIX} "
                "characters to identify a container unambiguously",
            )

        if not container.startswith(expected_container):
            return ServiceLookup(
                {},
                container,
                f"the running {service!r} container is "
                f"{container[:MIN_CONTAINER_PREFIX]}, not the recorded "
                f"{expected_container[:MIN_CONTAINER_PREFIX]} -- it was "
                "replaced between the request and this scan, so its "
                "environment is not the one that handled the request",
            )

    listing = _run(
        ["docker", "inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}"]
    )
    if listing.startswith("<"):
        return ServiceLookup(
            {}, container, f"the {service!r} container could not be inspected"
        )

    values: dict[str, str] = {}
    for line in listing.splitlines():
        name, separator, value = line.partition("=")
        if separator and value:
            values[name] = value

    return ServiceLookup(values, container, None)


def command_scan(args: argparse.Namespace) -> int:
    """Check whether sentinel values reached Phoenix. Values are never printed."""
    # Argument-shape checks come first, before any file is read, any
    # container is inspected, and long before Phoenix is queried: an
    # invocation that cannot produce usable evidence should say so for
    # that reason, not fail later on an unrelated missing file.
    #
    # `--env-from-service` without a binding reads whichever container is
    # current at scan time, which is the exact false-PASS
    # `--expect-container` exists to close: a service recreated between the
    # request and the scan yields a different credential, finds it absent,
    # and reports clean. Leaving the binding optional would have fixed the
    # mechanism while leaving the path that needs it open.
    #
    # The reverse pairing is refused too. `--expect-container` alone does
    # nothing, and an option that silently does nothing is worse here than
    # one that errors: it reads, in a pasted evidence record, exactly like
    # a binding that was enforced.
    if args.env_from_service and not args.expect_container:
        print("INCOMPLETE -- --env-from-service requires --expect-container.")
        print()
        print(
            "Without it the credential is read from whichever container is "
            "running now,\nnot the one that handled the request. Record the "
            "container from\n`live_validation.py provenance` before the "
            "request and pass it here.\nNothing was checked."
        )
        return 2

    if args.expect_container and not args.env_from_service:
        print("INCOMPLETE -- --expect-container requires --env-from-service.")
        print()
        print(
            "On its own it binds nothing: no source is being read from a "
            "container.\nNothing was checked."
        )
        return 2

    needles: dict[str, str] = {}

    if args.needles_file:
        try:
            needles.update(parse_needles(Path(args.needles_file).read_text()))
        except NeedlesFileError as error:
            # Caught here so a bad needles file exits as a defined
            # INCOMPLETE rather than as a traceback -- a traceback would
            # print the raising line's source and, on some Python
            # versions, the offending expression's context.
            print(f"INCOMPLETE -- {args.needles_file} cannot be used as supplied:")
            print(f"  {error}")
            print()
            print("Nothing was checked.")
            return 2

    env_file_text = None
    if args.env_file:
        env_file_text = Path(args.env_file).read_text()

    lookup = (
        service_environment(args.env_from_service, args.expect_container)
        if args.env_from_service
        else None
    )

    resolved, unresolved = resolve_env_needles(
        args.env or [],
        dict(os.environ),
        env_file_text,
        lookup.values if lookup else None,
        args.env_from_service,
        lookup.problem if lookup else None,
    )
    needles.update(resolved)

    if unresolved:
        # Hard failure, before querying Phoenix. A requested sentinel that
        # could not be resolved was not checked, so this run cannot support
        # the runbook's "every sensitive sentinel reports absent" criterion
        # no matter what the other needles report.
        print("INCOMPLETE -- these requested sentinels could not be resolved:")
        for entry in unresolved:
            print(f"  {entry.name:<32} {entry.reason}")
        print()
        print(
            "The authoritative source is the running container's own "
            "environment, which is\nthe value Docker Compose actually "
            "injected after any interpolation:\n\n"
            "  --env-from-service orchestrator --env HERMES_API_SERVER_KEY\n\n"
            "Values are never printed. Nothing was checked."
        )
        return 2

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
        "trace",
        help="span tree and expected-relationship check for one trace",
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
        help=(
            "environment variable name whose value to search for "
            "(repeatable). A name that cannot be resolved fails the run "
            "rather than being skipped."
        ),
    )
    scan.add_argument(
        "--env-from-service",
        help=(
            "read --env names from this running Compose service's own "
            "environment -- the value Docker Compose actually injected. "
            "Exclusive: when given, no other source is consulted for those "
            "names, so an unreadable container fails the run instead of "
            "silently degrading to a stale value. Requires "
            "--expect-container."
        ),
    )
    scan.add_argument(
        "--expect-container",
        help=(
            "container ID prefix recorded in the pre-run provenance, at "
            f"least {MIN_CONTAINER_PREFIX} characters. The scan fails "
            "unless --env-from-service still resolves to a container with "
            "this prefix, so a service recreated between the request and "
            "this scan cannot be read as if it were the instance that "
            "handled the request. Requires --env-from-service."
        ),
    )
    scan.add_argument(
        "--env-file",
        help=(
            "dotenv-style file to fall back to (e.g. .env). Values whose "
            "Compose semantics this tool cannot reproduce -- interpolation, "
            "escapes, inline comments -- are rejected, not guessed at."
        ),
    )
    scan.set_defaults(func=command_scan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
