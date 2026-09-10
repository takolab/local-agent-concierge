"""Tests for the live-validation evidence helper.

These cover the helper's pure logic only -- span-tree building,
expected-relationship checking, needle parsing and scanning -- against
fixtures. They perform no network access, no Docker access, and no Slack
access, and they deliberately do **not** stand in for live evidence: a
passing run here says the tool reports correctly, never that the live path
works.

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


# --- check_expected_relationships --------------------------------------


def test_expected_relationships_pass_for_a_correctly_parented_trace():
    results = lv.check_expected_relationships(_healthy_trace())

    assert len(results) == len(lv.EXPECTED_RELATIONSHIPS)
    assert all(result.ok for result in results)


def test_a_trace_missing_slack_response_cannot_pass():
    """Regression for a false PASS: `slack.response` is a second child of
    `concierge.request`, not part of the dispatch chain, so a purely linear
    expectation ignored it -- and a trace where the Slack reply never
    emitted reported every link OK. The runbook requires all six spans."""
    spans = [s for s in _healthy_trace() if s.name != "slack.response"]

    results = lv.check_expected_relationships(spans)
    failed = [r for r in results if not r.ok]

    assert len(failed) == 1
    assert failed[0].relationship == "concierge.request -> slack.response"
    assert "missing" in failed[0].detail


def test_every_expected_span_is_covered_by_some_relationship():
    """Requiring all relationships must also require all six spans -- that
    equivalence is what lets the command's exit status stand in for the
    runbook's span-count criterion."""
    assert set(lv.EXPECTED_SPAN_NAMES) == {
        "concierge.request",
        "orchestrator.dispatch",
        "POST /dispatch",
        "hermes.request",
        "/v1/responses",
        "slack.response",
    }

    covered = {name for pair in lv.EXPECTED_RELATIONSHIPS for name in pair}
    assert covered == set(lv.EXPECTED_SPAN_NAMES)


def test_expected_relationships_report_a_missing_span_distinctly():
    spans = [s for s in _healthy_trace() if s.name != "POST /dispatch"]

    results = {r.relationship: r for r in lv.check_expected_relationships(spans)}

    broken = results["orchestrator.dispatch -> POST /dispatch"]
    assert not broken.ok
    assert "missing" in broken.detail


def test_expected_relationships_report_a_broken_parent_link_distinctly():
    """A hop that emitted but did not continue the trace is a different
    failure from a hop that never emitted."""
    spans = _healthy_trace()
    spans[2] = _span("POST /dispatch", "c3", None, "2020-01-01T00:00:02")

    results = {r.relationship: r for r in lv.check_expected_relationships(spans)}

    broken = results["orchestrator.dispatch -> POST /dispatch"]
    assert not broken.ok
    assert "parent_id" in broken.detail
    assert "missing" not in broken.detail


def test_expected_relationships_report_ambiguity_rather_than_guessing():
    spans = _healthy_trace()
    spans.append(_span("hermes.request", "d9", "c3", "2020-01-01T00:00:09"))

    results = {r.relationship: r for r in lv.check_expected_relationships(spans)}

    assert not results["POST /dispatch -> hermes.request"].ok
    assert "ambiguous" in results["POST /dispatch -> hermes.request"].detail


def test_expected_span_names_match_what_the_code_emits():
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
    assert '"slack.response"' in gateway
    assert 'DISPATCH_SPAN_NAME = "POST /dispatch"' in orchestrator
    assert 'HERMES_SPAN_NAME = "hermes.request"' in orchestrator


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
    with pytest.raises(lv.NeedlesFileError):
        lv.parse_needles(text)


def test_parse_needles_rejects_a_duplicate_label():
    """Regression for a false PASS: `needles[label] = value` overwrote the
    earlier entry, so a repeated label silently dropped the first value
    from the scan entirely."""
    with pytest.raises(lv.NeedlesFileError, match="repeats a label"):
        lv.parse_needles("m=first-value\nm=second-value\n")


def test_duplicate_label_cannot_produce_a_clean_scan(
    monkeypatch, tmp_path, capsys
):
    """End-to-end: the *first* of two same-labelled values is present in
    the payload. Overwriting silently made that scan clean."""
    needles_file = tmp_path / "needles.txt"
    needles_file.write_text(f"m={SYNTHETIC_SECRET}\nm=synthetic-other-value\n")

    def _must_not_be_called(trace_id):  # pragma: no cover - asserted below
        raise AssertionError("Phoenix must not be queried on an unusable file")

    monkeypatch.setattr(lv, "_fetch_trace_payload", _must_not_be_called)

    exit_code = lv.main(
        ["scan", "synthetic-trace-id", "--needles-file", str(needles_file)]
    )

    assert exit_code != 0

    output = capsys.readouterr().out
    assert "INCOMPLETE" in output
    assert SYNTHETIC_SECRET not in output


@pytest.mark.parametrize(
    "line",
    [f"={SYNTHETIC_SECRET}", SYNTHETIC_SECRET, f"label with space {SYNTHETIC_SECRET}"],
    ids=["no_label", "pasted_value_only", "no_separator"],
)
def test_malformed_needle_errors_never_echo_the_input(line):
    """A malformed entry is exactly where a secret is most likely sitting
    -- a pasted value with no label, a stray `=`. Quoting the input to be
    helpful printed the thing this tool exists to keep out of terminals
    and tracebacks."""
    with pytest.raises(lv.NeedlesFileError) as error:
        lv.parse_needles(line + "\n")

    assert SYNTHETIC_SECRET not in str(error.value)
    assert SYNTHETIC_SECRET not in repr(error.value)


def test_malformed_needles_file_exits_cleanly_without_the_secret(
    monkeypatch, tmp_path, capsys
):
    """The error must surface as a defined INCOMPLETE, not an uncaught
    traceback -- a traceback prints the raising line's source context."""
    needles_file = tmp_path / "needles.txt"
    needles_file.write_text(f"={SYNTHETIC_SECRET}\n")

    monkeypatch.setattr(
        lv,
        "_fetch_trace_payload",
        lambda trace_id: '{"data": []}',
    )

    exit_code = lv.main(
        ["scan", "synthetic-trace-id", "--needles-file", str(needles_file)]
    )

    assert exit_code == 2

    captured = capsys.readouterr()
    assert "INCOMPLETE" in captured.out
    assert "Nothing was checked." in captured.out
    assert SYNTHETIC_SECRET not in captured.out
    assert SYNTHETIC_SECRET not in captured.err
    assert "Traceback" not in captured.err


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


# --- env sentinel resolution -------------------------------------------


def test_env_needle_resolves_from_the_process_environment():
    resolved, unresolved = lv.resolve_env_needles(
        ["SYNTHETIC_KEY"], {"SYNTHETIC_KEY": SYNTHETIC_SECRET}
    )

    assert resolved == {"SYNTHETIC_KEY": SYNTHETIC_SECRET}
    assert unresolved == []


def test_env_needle_falls_back_to_the_env_file():
    """Docker Compose reads .env itself; a host-side python3 process does
    not, which is exactly how a required sentinel came to be skipped."""
    resolved, unresolved = lv.resolve_env_needles(
        ["SYNTHETIC_KEY"], {}, f"SYNTHETIC_KEY={SYNTHETIC_SECRET}\n"
    )

    assert resolved == {"SYNTHETIC_KEY": SYNTHETIC_SECRET}
    assert unresolved == []


def test_service_values_are_consulted_only_when_requested():
    """There is no precedence ladder any more: the container source is
    either the requested authority (and then exclusive) or it is not
    consulted at all. `command_scan` passes both arguments together or
    neither, so this asserts the guard rather than a supported call."""
    resolved, _ = lv.resolve_env_needles(
        ["SYNTHETIC_KEY"],
        {"SYNTHETIC_KEY": "synthetic-host-value"},
        None,
        {"SYNTHETIC_KEY": SYNTHETIC_SECRET},
        service_name=None,
    )

    assert resolved == {"SYNTHETIC_KEY": "synthetic-host-value"}


def test_service_authority_is_exclusive_and_fails_closed():
    """Regression for a silent degradation: with `--env-from-service`
    requested and the container unreadable, falling back to the host's
    value would scan a stale credential -- reporting it absent while the
    one the container actually holds is the one that leaked."""
    resolved, unresolved = lv.resolve_env_needles(
        ["SYNTHETIC_KEY"],
        {"SYNTHETIC_KEY": "synthetic-stale-host-value"},
        "SYNTHETIC_KEY=synthetic-stale-file-value\n",
        {},
        "orchestrator",
    )

    assert resolved == {}
    assert [entry.name for entry in unresolved] == ["SYNTHETIC_KEY"]
    assert "orchestrator" in unresolved[0].reason
    assert "no fallback" in unresolved[0].reason


def test_service_authority_still_resolves_when_the_container_has_it():
    resolved, unresolved = lv.resolve_env_needles(
        ["SYNTHETIC_KEY"],
        {"SYNTHETIC_KEY": "synthetic-stale-host-value"},
        None,
        {"SYNTHETIC_KEY": SYNTHETIC_SECRET},
        "orchestrator",
    )

    assert resolved == {"SYNTHETIC_KEY": SYNTHETIC_SECRET}
    assert unresolved == []


def test_scan_fails_closed_when_the_service_cannot_be_inspected(
    monkeypatch, tmp_path, capsys
):
    """End-to-end: `--env-from-service` supplied, `service_environment()`
    returns {}, a stale value sits in the host environment. The command
    must fail closed and never query Phoenix."""
    needles_file = tmp_path / "needles.txt"
    needles_file.write_text("harmless=synthetic-absent-value\n")

    def _must_not_be_called(trace_id):  # pragma: no cover - asserted below
        raise AssertionError("Phoenix must not be queried on an incomplete check")

    monkeypatch.setattr(lv, "_fetch_trace_payload", _must_not_be_called)
    monkeypatch.setattr(lv, "service_environment", lambda service: {})
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-stale-host-value")

    exit_code = lv.main(
        [
            "scan",
            "synthetic-trace-id",
            "--needles-file",
            str(needles_file),
            "--env",
            "SYNTHETIC_KEY",
            "--env-from-service",
            "orchestrator",
        ]
    )

    assert exit_code == 2

    output = capsys.readouterr().out
    assert "INCOMPLETE" in output
    assert "SYNTHETIC_KEY" in output
    assert "synthetic-stale-host-value" not in output


def test_unresolvable_env_needle_is_reported_not_skipped():
    """Regression for a false PASS: an explicitly requested sentinel that
    could not be resolved must reach the caller as unresolved, so the run
    can be failed. Skipping it let `scan` exit 0 while never checking a
    credential."""
    resolved, unresolved = lv.resolve_env_needles(
        ["SYNTHETIC_KEY"], {}, "SOMETHING_ELSE=value\n"
    )

    assert resolved == {}
    assert [entry.name for entry in unresolved] == ["SYNTHETIC_KEY"]
    assert unresolved[0].reason == "not found"


# --- dotenv semantics this tool refuses to guess at ---------------------


def test_interpolated_env_file_value_is_rejected_not_parsed():
    """Regression for an evidence-integrity hole: Compose expands
    `KEY=${BASE}` to BASE's value, while a literal parse reads back
    "${BASE}". Scanning that literal would report the *real* credential as
    absent even when it leaked."""
    text = f"BASE_SECRET={SYNTHETIC_SECRET}\nSYNTHETIC_KEY=${{BASE_SECRET}}\n"

    values, rejected = lv.parse_env_file(text)

    assert "SYNTHETIC_KEY" not in values
    assert "interpolation" in rejected["SYNTHETIC_KEY"]

    resolved, unresolved = lv.resolve_env_needles(["SYNTHETIC_KEY"], {}, text)

    assert resolved == {}
    assert [entry.name for entry in unresolved] == ["SYNTHETIC_KEY"]


def test_interpolated_env_file_cannot_produce_a_clean_scan(
    monkeypatch, tmp_path, capsys
):
    """End-to-end: the real injected value leaks into telemetry, the
    env-file defines the sentinel by interpolation. The tool must not
    report a clean run by scanning the literal `${BASE_SECRET}`."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"BASE_SECRET={SYNTHETIC_SECRET}\nSYNTHETIC_KEY=${{BASE_SECRET}}\n"
    )

    def _must_not_be_called(trace_id):  # pragma: no cover - asserted below
        raise AssertionError("Phoenix must not be queried on an incomplete check")

    monkeypatch.setattr(lv, "_fetch_trace_payload", _must_not_be_called)
    monkeypatch.delenv("SYNTHETIC_KEY", raising=False)

    exit_code = lv.main(
        [
            "scan",
            "synthetic-trace-id",
            "--env",
            "SYNTHETIC_KEY",
            "--env-file",
            str(env_file),
        ]
    )

    assert exit_code != 0

    output = capsys.readouterr().out
    assert "INCOMPLETE" in output
    assert "interpolation" in output
    assert SYNTHETIC_SECRET not in output


@pytest.mark.parametrize(
    ("line", "expected_reason_fragment"),
    [
        ("SYNTHETIC_KEY=$OTHER", "interpolation"),
        ("SYNTHETIC_KEY=value # trailing", "inline comment"),
        ("SYNTHETIC_KEY=a\\nb", "backslash"),
        ("SYNTHETIC_KEY=`whoami`", "command substitution"),
        ('SYNTHETIC_KEY="${OTHER}"', "interpolation"),
        ("export SYNTHETIC_KEY=value", "export"),
    ],
    ids=[
        "bare_interpolation",
        "inline_comment",
        "backslash_escape",
        "command_substitution",
        "quoted_interpolation",
        "export_prefix",
    ],
)
def test_unreproducible_dotenv_syntax_is_rejected(line, expected_reason_fragment):
    """Each of these means Compose would inject something other than what a
    literal parse reads back. Rejecting is fail-closed; guessing would
    produce a clean report on an unchecked credential."""
    values, rejected = lv.parse_env_file(line + "\n")

    assert "SYNTHETIC_KEY" not in values
    assert expected_reason_fragment in rejected["SYNTHETIC_KEY"]


def test_rejection_reasons_never_quote_the_value():
    values, rejected = lv.parse_env_file(
        f"SYNTHETIC_KEY={SYNTHETIC_SECRET} # comment\n"
    )

    assert SYNTHETIC_SECRET not in str(rejected)
    assert SYNTHETIC_SECRET not in str(values)


def test_service_environment_reads_the_injected_values(monkeypatch):
    """Ground truth: whatever the container's own Config.Env holds."""
    monkeypatch.setattr(
        lv,
        "_run",
        lambda command: (
            "container-id"
            if command[:3] == ["docker", "compose", "ps"]
            else f"PATH=/usr/bin\nSYNTHETIC_KEY={SYNTHETIC_SECRET}\nEMPTY="
        ),
    )

    values = lv.service_environment("orchestrator")

    assert values["SYNTHETIC_KEY"] == SYNTHETIC_SECRET
    assert "EMPTY" not in values


@pytest.mark.parametrize(
    "ps_output", ["", "<unavailable>"], ids=["not_running", "command_failed"]
)
def test_service_environment_fails_closed(monkeypatch, ps_output):
    """A service that cannot be inspected yields no values, which surfaces
    as an unresolved sentinel and therefore as a failed run -- never as a
    silently skipped check."""
    monkeypatch.setattr(lv, "_run", lambda command: ps_output)

    assert lv.service_environment("orchestrator") == {}


def test_scan_command_fails_when_a_requested_env_needle_is_missing(
    monkeypatch, tmp_path, capsys
):
    """End-to-end regression: other needles present and clean, the
    credential unresolvable -- the command must not return success, and
    must not even query Phoenix."""
    needles_file = tmp_path / "needles.txt"
    needles_file.write_text("harmless=synthetic-absent-value\n")

    def _must_not_be_called(trace_id):  # pragma: no cover - asserted below
        raise AssertionError("Phoenix must not be queried on an incomplete check")

    monkeypatch.setattr(lv, "_fetch_trace_payload", _must_not_be_called)
    monkeypatch.delenv("SYNTHETIC_KEY", raising=False)

    exit_code = lv.main(
        [
            "scan",
            "synthetic-trace-id",
            "--needles-file",
            str(needles_file),
            "--env",
            "SYNTHETIC_KEY",
        ]
    )

    assert exit_code != 0

    output = capsys.readouterr().out
    assert "INCOMPLETE" in output
    assert "SYNTHETIC_KEY" in output
    assert "Nothing was checked." in output


def test_scan_command_succeeds_when_every_needle_resolves_and_is_absent(
    monkeypatch, tmp_path
):
    needles_file = tmp_path / "needles.txt"
    needles_file.write_text("harmless=synthetic-absent-value\n")

    monkeypatch.setattr(
        lv,
        "_fetch_trace_payload",
        lambda trace_id: '{"data": [{"name": "concierge.request"}]}',
    )
    monkeypatch.setenv("SYNTHETIC_KEY", SYNTHETIC_SECRET)

    exit_code = lv.main(
        [
            "scan",
            "synthetic-trace-id",
            "--needles-file",
            str(needles_file),
            "--env",
            "SYNTHETIC_KEY",
        ]
    )

    assert exit_code == 0


def test_scan_command_fails_when_a_needle_leaks(monkeypatch, tmp_path, capsys):
    needles_file = tmp_path / "needles.txt"
    needles_file.write_text("leaked=synthetic-leaked-value\n")

    monkeypatch.setattr(
        lv,
        "_fetch_trace_payload",
        lambda trace_id: '{"data": [{"x": "synthetic-leaked-value"}]}',
    )

    exit_code = lv.main(
        ["scan", "synthetic-trace-id", "--needles-file", str(needles_file)]
    )

    assert exit_code != 0

    output = capsys.readouterr().out
    assert "LEAKED" in output
    # Even on a leak, the tool reports the label, never the value.
    assert "synthetic-leaked-value" not in output


def test_env_file_parsing_ignores_comments_and_strips_one_quote_pair():
    values, rejected = lv.parse_env_file(
        '# comment\n\nA=1\nB="quoted"\nC=\nNO_SEPARATOR\n'
    )

    assert values == {"A": "1", "B": "quoted"}
    assert rejected == {}


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
