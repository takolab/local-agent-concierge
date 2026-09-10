"""Tests for the live-validation evidence helper.

These cover the helper's pure logic only -- span-tree building,
expected-chain checking, needle parsing and scanning -- against fixtures.
They perform no network access, no Docker access, and no Slack access, and
they deliberately do **not** stand in for live evidence: a passing run here
says the tool reports correctly, never that the live path works.

Only clearly-synthetic placeholder values are used -- never a real secret,
token, or identifier.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "infra" / "observability"))

import live_validation as lv  # noqa: E402

SYNTHETIC_SECRET = "synthetic-not-a-real-credential"


def _span(name, span_id, parent_id=None, start_time="2020-01-01T00:00:00", **attrs):
    return lv.Span(
        name=name,
        span_id=span_id,
        parent_id=parent_id,
        start_time=start_time,
        attributes=dict(attrs),
    )


def _healthy_trace() -> list[lv.Span]:
    """The span set one successful Slack request is expected to produce."""
    return [
        _span("concierge.request", "a1", None, "2020-01-01T00:00:00"),
        _span("orchestrator.dispatch", "b2", "a1", "2020-01-01T00:00:01"),
        _span("POST /dispatch", "c3", "b2", "2020-01-01T00:00:02"),
        _span("hermes.request", "d4", "c3", "2020-01-01T00:00:03"),
        _span("/v1/responses", "e5", "d4", "2020-01-01T00:00:04"),
        _span("slack.response", "f6", "a1", "2020-01-01T00:00:05"),
    ]


# --- parse_spans -------------------------------------------------------


def test_parse_spans_reads_the_phoenix_response_shape():
    payload = {
        "data": [
            {
                "name": "concierge.request",
                "context": {"span_id": "a1", "trace_id": "t1"},
                "parent_id": None,
                "start_time": "2020-01-01T00:00:00",
                "attributes": {"concierge.request.source": "slack"},
            }
        ]
    }

    (span,) = lv.parse_spans(payload)

    assert span.name == "concierge.request"
    assert span.span_id == "a1"
    assert span.parent_id is None
    assert span.attributes["concierge.request.source"] == "slack"


def test_parse_spans_tolerates_missing_optional_fields():
    """A span that arrived in an unexpected shape is itself evidence, so it
    is reported rather than dropped or raised on."""
    (span,) = lv.parse_spans({"data": [{"context": {"span_id": "a1"}}]})

    assert span.name == "<unnamed>"
    assert span.parent_id is None
    assert span.attributes == {}


@pytest.mark.parametrize(
    "payload", [{}, {"data": []}, {"data": ["not-a-span"]}, "not-a-mapping"]
)
def test_parse_spans_returns_empty_for_unusable_payloads(payload):
    assert lv.parse_spans(payload) == []


# --- build_span_tree ---------------------------------------------------


def test_span_tree_depths_follow_the_parent_chain():
    rows = lv.build_span_tree(_healthy_trace())

    depths = {row.span.name: row.depth for row in rows}

    assert depths == {
        "concierge.request": 0,
        "orchestrator.dispatch": 1,
        "POST /dispatch": 2,
        "hermes.request": 3,
        "/v1/responses": 4,
        "slack.response": 1,
    }


def test_span_tree_is_ordered_by_start_time():
    spans = list(reversed(_healthy_trace()))

    names = [row.span.name for row in lv.build_span_tree(spans)]

    assert names[0] == "concierge.request"
    assert names[-1] == "slack.response"


def test_span_with_an_absent_parent_is_kept_at_its_provable_depth():
    """A parent from another trace, or one that never reached Phoenix,
    must not make its child disappear from the evidence."""
    spans = [_span("orphan", "z9", "missing-parent")]

    (row,) = lv.build_span_tree(spans)

    assert row.span.name == "orphan"
    assert row.depth == 0


def test_cyclic_parent_chain_terminates():
    """No correct exporter produces this; an evidence tool must not hang
    on it either."""
    spans = [
        _span("a", "a1", "b2", "2020-01-01T00:00:00"),
        _span("b", "b2", "a1", "2020-01-01T00:00:01"),
    ]

    rows = lv.build_span_tree(spans)

    assert len(rows) == 2
    assert all(row.depth <= len(spans) for row in rows)


# --- check_expected_chain ----------------------------------------------


def test_expected_chain_passes_for_a_correctly_parented_trace():
    results = lv.check_expected_chain(_healthy_trace())

    assert len(results) == len(lv.EXPECTED_CHAIN) - 1
    assert all(result.ok for result in results)


def test_expected_chain_reports_a_missing_span_distinctly():
    spans = [s for s in _healthy_trace() if s.name != "POST /dispatch"]

    results = {r.relationship: r for r in lv.check_expected_chain(spans)}

    broken = results["orchestrator.dispatch -> POST /dispatch"]
    assert not broken.ok
    assert "missing" in broken.detail


def test_expected_chain_reports_a_broken_parent_link_distinctly():
    """A hop that emitted but did not continue the trace is a different
    failure from a hop that never emitted."""
    spans = _healthy_trace()
    spans[2] = _span("POST /dispatch", "c3", None, "2020-01-01T00:00:02")

    results = {r.relationship: r for r in lv.check_expected_chain(spans)}

    broken = results["orchestrator.dispatch -> POST /dispatch"]
    assert not broken.ok
    assert "parent_id" in broken.detail
    assert "missing" not in broken.detail


def test_expected_chain_reports_ambiguity_rather_than_guessing():
    spans = _healthy_trace()
    spans.append(_span("hermes.request", "d9", "c3", "2020-01-01T00:00:09"))

    results = {r.relationship: r for r in lv.check_expected_chain(spans)}

    assert not results["POST /dispatch -> hermes.request"].ok
    assert "ambiguous" in results["POST /dispatch -> hermes.request"].detail


def test_expected_chain_matches_the_span_names_the_code_emits():
    """Guards the runbook against drift: these names come from
    slack_gateway.telemetry, orchestrator.telemetry, and Hermes Agent's
    auto-instrumented route."""
    gateway = (
        REPO_ROOT / "apps/slack-gateway/src/slack_gateway/telemetry.py"
    ).read_text()
    orchestrator = (
        REPO_ROOT / "services/orchestrator/src/orchestrator/telemetry.py"
    ).read_text()

    assert '"concierge.request"' in gateway
    assert '"orchestrator.dispatch"' in gateway
    assert 'DISPATCH_SPAN_NAME = "POST /dispatch"' in orchestrator
    assert 'HERMES_SPAN_NAME = "hermes.request"' in orchestrator

    assert lv.EXPECTED_CHAIN == (
        "concierge.request",
        "orchestrator.dispatch",
        "POST /dispatch",
        "hermes.request",
        "/v1/responses",
    )


def test_phoenix_project_matches_the_collector_configuration():
    """A rename in otel-collector.yaml must fail here rather than make the
    helper silently query an empty project."""
    config = (REPO_ROOT / "infra/observability/otel-collector.yaml").read_text()

    assert f"x-project-name: {lv.PHOENIX_PROJECT}" in config


# --- needles -----------------------------------------------------------


def test_parse_needles_reads_labelled_values():
    needles = lv.parse_needles(
        "# comment\n\nslack user = U-synthetic\nkey=abc=def\n"
    )

    assert needles == {"slack user": "U-synthetic", "key": "abc=def"}


@pytest.mark.parametrize(
    "text", ["no-separator\n", "=value\n", "label=\n"],
    ids=["no_separator", "no_label", "no_value"],
)
def test_parse_needles_rejects_malformed_lines(text):
    """A silently dropped needle would turn a missed leak into a clean
    report, so a malformed line is an error, not a skip."""
    with pytest.raises(ValueError):
        lv.parse_needles(text)


def test_scan_finds_a_present_value_case_insensitively():
    payload = '{"attributes": {"x": "U-SYNTHETIC-USER"}}'

    (result,) = lv.scan_for_needles(payload, {"slack user": "u-synthetic-user"})

    assert result.found is True


def test_scan_reports_absent_values():
    payload = '{"attributes": {"concierge.operation": "dispatch"}}'

    results = {r.label: r.found for r in lv.scan_for_needles(
        payload,
        {"secret": SYNTHETIC_SECRET, "slack user": "U-synthetic"},
    )}

    assert results == {"secret": False, "slack user": False}


def test_scan_results_never_carry_the_needle_value():
    """The whole point of routing this through a tool: a real credential
    can be checked without being echoed into a terminal or an evidence
    record."""
    payload = f'{{"leak": "{SYNTHETIC_SECRET}"}}'

    results = lv.scan_for_needles(payload, {"secret": SYNTHETIC_SECRET})

    assert results[0].found is True
    assert SYNTHETIC_SECRET not in str(results)
    assert SYNTHETIC_SECRET not in "".join(r.label for r in results)


# --- surface -----------------------------------------------------------


HELPER_SOURCE = (REPO_ROOT / "infra/observability/live_validation.py").read_text()

# `docker` subcommands this tool may run. Everything that changes container
# state -- up, down, start, stop, restart, rm, exec, kill, run -- is absent
# on purpose.
ALLOWED_DOCKER_SUBCOMMANDS = {"inspect", "compose"}
ALLOWED_COMPOSE_SUBCOMMANDS = {"ps"}


def _shell_commands() -> list[list[str]]:
    """Every literal command list the helper passes to `_run`."""
    tree = ast.parse(HELPER_SOURCE)
    commands: list[list[str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_run"):
            continue

        (argument,) = node.args
        assert isinstance(argument, ast.List), (
            "every _run() call must take a literal command list, so this "
            "test can see what it runs"
        )

        # A non-constant element (a container id resolved at runtime) is
        # represented as "<var>". Only the constant prefix -- the binary and
        # its subcommand -- carries the read-only property being asserted,
        # and that prefix is always literal.
        commands.append(
            [
                element.value if isinstance(element, ast.Constant) else "<var>"
                for element in argument.elts
            ]
        )

    return commands


def test_helper_runs_only_read_only_shell_commands():
    """The tool must remain incapable of changing runtime state."""
    commands = _shell_commands()
    assert commands, "no commands found -- the AST walk above is broken"

    for command in commands:
        binary = command[0]
        assert binary in {"git", "docker"}, command

        if binary == "docker":
            assert command[1] in ALLOWED_DOCKER_SUBCOMMANDS, command
            if command[1] == "compose":
                assert command[2] in ALLOWED_COMPOSE_SUBCOMMANDS, command

        if binary == "git":
            assert command[1] in {"rev-parse", "status"}, command


def test_helper_issues_no_write_http_request():
    """`urlopen(url, timeout=...)` is a GET. A `Request` object, a body, or
    an explicit method would allow a POST -- including to `/dispatch`,
    which would make this tool dispatch the request it is meant to
    observe."""
    assert "urllib.request.Request" not in HELPER_SOURCE
    assert "data=" not in HELPER_SOURCE
    assert "method=" not in HELPER_SOURCE

    tree = ast.parse(HELPER_SOURCE)
    urlopen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "urlopen"
    ]

    assert urlopen_calls
    for call in urlopen_calls:
        assert len(call.args) == 1, "a bare URL only"
        assert {kw.arg for kw in call.keywords} <= {"timeout"}


def test_helper_exposes_exactly_the_three_read_only_subcommands():
    tree = ast.parse(HELPER_SOURCE)
    names = [
        ast.literal_eval(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
    ]

    assert sorted(names) == ["provenance", "scan", "trace"]
