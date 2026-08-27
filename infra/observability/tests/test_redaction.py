"""Verification for the shared OpenTelemetry Collector's redaction processor.

Application instrumentation (apps/slack-gateway, mcp/google-calendar) is the
first line of defense against exporting sensitive data, and is covered by
its own tests (apps/slack-gateway/tests/test_telemetry.py,
mcp/google-calendar/tests/test_telemetry.py). This module verifies the
second, Collector-side layer described in
docs/observability/collector-redaction.md: that a known-sensitive span
attribute is masked before it reaches *any* exporter, even if application
code (or an auto-instrumented dependency) attaches one by mistake.

These tests run the real `otel/opentelemetry-collector-contrib` image — the
same one docker-compose.yml uses — against this repository's actual
infra/observability/otel-collector.yaml, because the behavior under test is
the Collector's own configuration, not application code. They require a
Docker daemon reachable from wherever pytest runs (true on GitHub Actions
runners and on a developer machine, not inside the existing `--profile test`
containers), following the same pattern as
apps/hermes-agent/tests/test_trace_context_propagation.py.

Only clearly-synthetic placeholder values are used below — never a real
secret, token, or personal identifier.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_CONFIG = REPO_ROOT / "infra" / "observability" / "otel-collector.yaml"
IMAGE = "otel/opentelemetry-collector-contrib:latest"
CONTAINER_NAME = "local-agent-concierge-otel-collector-redaction-pytest"

# Distinct from the Compose stack's 4317/4318/13133 so this can run
# alongside a developer's already-running `docker compose up`.
HTTP_PORT = 24318
HEALTH_PORT = 24133
BASE_URL = f"http://127.0.0.1:{HTTP_PORT}"
HEALTH_URL = f"http://127.0.0.1:{HEALTH_PORT}/"

STARTUP_TIMEOUT_SECONDS = 30
EXPORT_WAIT_SECONDS = 3

# Synthetic-only placeholder values — see docs/observability/
# collector-redaction.md "Synthetic verification". None of these are real
# secrets, tokens, or personal data.
FAKE_SECRET = "fake-secret-value-for-redaction-test"
FAKE_EMAIL = "person@example.invalid"
FAKE_EVENT_ID = "fake-calendar-event-id"
FAKE_MESSAGE_BODY = "synthetic-message-body"

# One representative attribute per blocked_key_patterns entry in
# infra/observability/otel-collector.yaml.
SENSITIVE_ATTRIBUTES: dict[str, str] = {
    "authorization": f"Bearer {FAKE_SECRET}",
    "http.request.header.authorization": f"Bearer {FAKE_SECRET}",
    "oauth.refresh_token": FAKE_SECRET,
    "oauth.access_token": FAKE_SECRET,
    "client_secret": FAKE_SECRET,
    "api_key": FAKE_SECRET,
    "user.email": FAKE_EMAIL,
    "attendee.email": FAKE_EMAIL,
    "calendar.id": "fake-calendar-id",
    "calendar.event.id": FAKE_EVENT_ID,
    "calendar.event.summary": "synthetic calendar event summary",
    "slack.message.text": FAKE_MESSAGE_BODY,
    "slack.user.id": "U-SYNTHETIC0001",
    "slack.channel.id": "C-SYNTHETIC0001",
    "slack.workspace.id": "T-SYNTHETIC0001",
    "conversation.id": "synthetic-conversation-id",
    "exception.message": "synthetic sensitive exception detail",
    "request.body": "synthetic raw request body",
}

# Attributes the Collector must not alter: real, currently-emitted
# low-cardinality span attributes (Slack Gateway, Google Calendar MCP), plus
# two of the task's own example safe attributes to prove unknown-but-safe
# keys also survive (allow_all_keys fail-open), not just the explicit
# ignored_keys allowlist.
SAFE_ATTRIBUTES: dict[str, str] = {
    "operation.name": "synthetic-operation",
    "error.type": "synthetic_error",
    "mcp.method.name": "tools/call",
    "gen_ai.tool.name": "list_events",
    "slack.message.threaded": "true",
    "concierge.request.source": "slack",
}

SAFE_RESOURCE_ATTRIBUTES: dict[str, str] = {
    "service.name": "synthetic-redaction-test",
    "service.namespace": "local-agent-concierge",
}


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _container_logs() -> str:
    result = _docker("logs", CONTAINER_NAME, check=False)
    return result.stdout + result.stderr


def _attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _send_probe_span(base_url: str, span_name: str) -> None:
    start_ns = time.time_ns()
    end_ns = start_ns + 10_000_000

    attributes = [
        _attr(key, value)
        for key, value in {**SAFE_ATTRIBUTES, **SENSITIVE_ATTRIBUTES}.items()
    ]

    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr(key, value)
                        for key, value in SAFE_RESOURCE_ATTRIBUTES.items()
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "collector-redaction-pytest"},
                        "spans": [
                            {
                                "traceId": secrets.token_hex(16),
                                "spanId": secrets.token_hex(8),
                                "name": span_name,
                                "kind": 1,
                                "startTimeUnixNano": str(start_ns),
                                "endTimeUnixNano": str(end_ns),
                                "attributes": attributes,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    request = urllib.request.Request(
        f"{base_url}/v1/traces",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def _probe_span_section(span_name: str) -> str:
    """The debug exporter's log output covering one probe span: from the
    `ResourceSpans #` block containing it (so resource-level attributes like
    `service.name` are included) to the end of the (so far captured) logs.
    Each test sends its own uniquely-named span into a container private to
    this test module, so nothing else is logged after it at the point this
    is called.
    """
    logs = _container_logs()
    span_start = logs.rindex(span_name)
    resource_start = logs.rfind("ResourceSpans #", 0, span_start)
    start = resource_start if resource_start != -1 else span_start
    return logs[start:]


@pytest.fixture(scope="module")
def collector_container() -> str:
    _docker("rm", "-f", CONTAINER_NAME, check=False)

    run = _docker(
        "run", "-d", "--name", CONTAINER_NAME,
        "-p", f"127.0.0.1:{HTTP_PORT}:4318",
        "-p", f"127.0.0.1:{HEALTH_PORT}:13133",
        "-v", f"{COLLECTOR_CONFIG}:/etc/otelcol-contrib/config.yaml:ro",
        IMAGE,
        "--config=/etc/otelcol-contrib/config.yaml",
        check=False,
    )
    if run.returncode != 0:
        pytest.fail(f"docker run failed:\n{run.stdout}\n{run.stderr}")

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    healthy = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    healthy = True
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)

    if not healthy:
        logs = _container_logs()
        _docker("rm", "-f", CONTAINER_NAME, check=False)
        pytest.fail(f"Collector did not become healthy in time:\n{logs}")

    yield BASE_URL

    _docker("rm", "-f", CONTAINER_NAME, check=False)


def test_collector_config_validates() -> None:
    result = _docker(
        "run", "--rm",
        "-v", f"{COLLECTOR_CONFIG}:/etc/otelcol-contrib/config.yaml:ro",
        IMAGE,
        "validate", "--config=/etc/otelcol-contrib/config.yaml",
        check=False,
    )
    assert result.returncode == 0, (
        f"Collector config validation failed:\n{result.stdout}\n{result.stderr}"
    )


def test_collector_starts_and_reports_healthy(
    collector_container: str,
) -> None:
    with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
        assert response.status == 200


def test_sensitive_values_do_not_reach_the_debug_exporter(
    collector_container: str,
) -> None:
    span_name = f"redaction-pytest-values-{secrets.token_hex(4)}"
    _send_probe_span(collector_container, span_name)

    time.sleep(EXPORT_WAIT_SECONDS)
    logs = _container_logs()

    assert span_name in logs, f"probe span not found in Collector output:\n{logs}"

    for value in {FAKE_SECRET, FAKE_EMAIL, FAKE_EVENT_ID, FAKE_MESSAGE_BODY}:
        assert value not in logs, (
            f"synthetic sensitive value {value!r} reached exported telemetry"
        )


def test_sensitive_keys_are_masked_not_silently_dropped(
    collector_container: str,
) -> None:
    """The attribute *key* survives (masked) rather than vanishing, so this
    also proves the Collector is actively matching and rewriting these
    attributes rather than e.g. failing to parse them at all.
    """
    span_name = f"redaction-pytest-masked-{secrets.token_hex(4)}"
    _send_probe_span(collector_container, span_name)

    time.sleep(EXPORT_WAIT_SECONDS)
    section = _probe_span_section(span_name)

    for key in SENSITIVE_ATTRIBUTES:
        assert f"{key}: Str(****)" in section, (
            f"expected {key} to be present but masked; logs:\n{section}"
        )

    assert (
        f"redaction.masked.count: Int({len(SENSITIVE_ATTRIBUTES)})" in section
    )


def test_safe_attributes_survive_redaction(
    collector_container: str,
) -> None:
    span_name = f"redaction-pytest-safe-{secrets.token_hex(4)}"
    _send_probe_span(collector_container, span_name)

    time.sleep(EXPORT_WAIT_SECONDS)
    section = _probe_span_section(span_name)

    for key, value in SAFE_ATTRIBUTES.items():
        assert f"{key}: Str({value})" in section, (
            f"safe attribute {key}={value!r} did not survive redaction"
        )

    for key, value in SAFE_RESOURCE_ATTRIBUTES.items():
        assert f"{key}: Str({value})" in section, (
            f"safe resource attribute {key}={value!r} did not survive redaction"
        )
