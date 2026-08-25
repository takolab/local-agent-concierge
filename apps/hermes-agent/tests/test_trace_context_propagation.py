"""Integration tests for Hermes Agent's incoming W3C Trace Context extraction.

These tests build the derived Hermes Agent image (see ../Dockerfile) and run
it as a real container, because the behavior under test is OpenTelemetry
auto-instrumentation attaching to Hermes' own aiohttp server process — it
cannot be verified by unit-testing application code, since no
local-agent-concierge source calls into it directly. They require a Docker
daemon reachable from wherever pytest runs (true on GitHub Actions runners
and on a developer machine, not inside the existing `--profile test`
containers, which do not have Docker-in-Docker access).
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE_DIR = REPO_ROOT / "apps" / "hermes-agent"
IMAGE_TAG = "local-agent-concierge-hermes-agent-otel:pytest"
CONTAINER_NAME = "local-agent-concierge-hermes-agent-otel-pytest"
API_SERVER_KEY = "pytest-hermes-api-server-key-0001"
HOST_PORT = 18742
BASE_URL = f"http://127.0.0.1:{HOST_PORT}"
STARTUP_TIMEOUT_SECONDS = 90
SPAN_EXPORT_WAIT_SECONDS = 5


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _container_logs(container_name: str = CONTAINER_NAME) -> str:
    return _docker("logs", container_name, check=False).stdout


def _exported_response_spans(
    container_name: str = CONTAINER_NAME,
) -> list[dict[str, Any]]:
    """Parse ConsoleSpanExporter's pretty-printed JSON span dumps out of the
    container's stdout logs.
    """
    logs = _container_logs(container_name)
    spans: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = logs.find("{", idx)
        if idx == -1:
            break
        try:
            obj, end = decoder.raw_decode(logs, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict) and obj.get("name") == "/v1/responses":
            spans.append(obj)
        idx = end
    return spans


def _post_responses(
    headers: dict[str, str] | None = None,
    base_url: str = BASE_URL,
) -> httpx.Response:
    return httpx.post(
        f"{base_url}/v1/responses",
        headers={
            "Authorization": f"Bearer {API_SERVER_KEY}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
        json={"model": "hermes-agent", "input": "hello", "store": False},
        timeout=30,
    )


@pytest.fixture(scope="module")
def hermes_container() -> str:
    build = _docker(
        "build",
        "-t", IMAGE_TAG,
        "-f", str(DOCKERFILE_DIR / "Dockerfile"),
        str(DOCKERFILE_DIR),
        check=False,
    )
    if build.returncode != 0:
        pytest.fail(f"docker build failed:\n{build.stdout}\n{build.stderr}")

    _docker("rm", "-f", CONTAINER_NAME, check=False)

    run = _docker(
        "run", "-d", "--name", CONTAINER_NAME,
        "-p", f"{HOST_PORT}:8642",
        "-e", "API_SERVER_ENABLED=true",
        "-e", "API_SERVER_HOST=0.0.0.0",
        "-e", f"API_SERVER_KEY={API_SERVER_KEY}",
        "-e", "OTEL_SERVICE_NAME=hermes-agent",
        "-e", "OTEL_TRACES_EXPORTER=console",
        "-e", "OTEL_METRICS_EXPORTER=none",
        "-e", "OTEL_LOGS_EXPORTER=none",
        IMAGE_TAG,
        # No opentelemetry-instrument wrapper and no
        # HERMES_GATEWAY_NO_SUPERVISE here on purpose: instrumentation is
        # activated by the PYTHONPATH baked into the image (Dockerfile),
        # so it survives Hermes' own supervised-restart path
        # (`hermes gateway run --replace` under a dynamically created
        # s6-rc "gateway-<profile>" service) instead of only covering the
        # one foreground process this command starts. A command-line
        # wrapper here previously caused a real "gateway already running"
        # crash loop on any profile with prior run state — see
        # docs/observability/hermes-trace-context.md.
        "gateway", "run",
        check=False,
    )
    if run.returncode != 0:
        pytest.fail(f"docker run failed:\n{run.stdout}\n{run.stderr}")

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    healthy = False
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{BASE_URL}/health", timeout=2)
            if response.status_code == 200:
                healthy = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)

    if not healthy:
        logs = _container_logs()
        _docker("rm", "-f", CONTAINER_NAME, check=False)
        pytest.fail(f"Hermes container did not become healthy in time:\n{logs}")

    yield BASE_URL

    _docker("rm", "-f", CONTAINER_NAME, check=False)


def test_health_endpoint_is_unaffected(hermes_container: str) -> None:
    response = httpx.get(f"{hermes_container}/health", timeout=5)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_valid_traceparent_is_extracted_as_parent_span(
    hermes_container: str,
) -> None:
    trace_id = uuid.uuid4().hex
    parent_id = uuid.uuid4().hex[:16]
    traceparent = f"00-{trace_id}-{parent_id}-01"

    response = _post_responses(headers={"traceparent": traceparent})
    assert response.status_code == 200

    time.sleep(SPAN_EXPORT_WAIT_SECONDS)
    spans = _exported_response_spans()
    matching = [
        span for span in spans
        if span["context"]["trace_id"] == f"0x{trace_id}"
    ]

    assert matching, f"no exported span matched injected trace id; spans={spans}"
    assert matching[-1]["parent_id"] == f"0x{parent_id}"
    assert matching[-1]["kind"] == "SpanKind.SERVER"


def test_missing_traceparent_starts_a_new_root_trace(
    hermes_container: str,
) -> None:
    spans_before = len(_exported_response_spans())

    response = _post_responses()
    assert response.status_code == 200

    time.sleep(SPAN_EXPORT_WAIT_SECONDS)
    spans = _exported_response_spans()

    assert len(spans) > spans_before
    assert spans[-1]["parent_id"] is None


def test_invalid_traceparent_does_not_break_the_request(
    hermes_container: str,
) -> None:
    response = _post_responses(headers={"traceparent": "not-a-valid-traceparent"})
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "completed"

    time.sleep(SPAN_EXPORT_WAIT_SECONDS)
    spans = _exported_response_spans()

    assert spans[-1]["parent_id"] is None


def test_exported_spans_carry_no_sensitive_attributes(
    hermes_container: str,
) -> None:
    trace_id = uuid.uuid4().hex
    parent_id = uuid.uuid4().hex[:16]
    traceparent = f"00-{trace_id}-{parent_id}-01"

    response = _post_responses(headers={"traceparent": traceparent})
    assert response.status_code == 200

    time.sleep(SPAN_EXPORT_WAIT_SECONDS)
    spans = _exported_response_spans()
    matching = [
        span for span in spans
        if span["context"]["trace_id"] == f"0x{trace_id}"
    ]
    assert matching

    attributes = matching[-1]["attributes"]
    serialized = json.dumps(attributes)

    assert "Authorization" not in serialized
    assert "Bearer" not in serialized
    assert "hello" not in serialized


def test_container_restart_with_prior_state_does_not_crash_loop() -> None:
    """Regression test for a real incident: a `command:` wrapper around
    `hermes gateway run` worked on a first, fresh start, but Hermes' own
    cont-init (`02-reconcile-profiles`) restores a dynamically
    s6-supervised `gateway-<profile>` service on every subsequent start
    once any prior run has recorded `desired_state: running`. That
    supervised process is started independently of this container's
    `command:`, so a wrapper only instrumented the very first invocation —
    and disabling supervision to keep the wrapper in place then collided
    with reconcile-profiles' own supervised restart, producing a
    `gateway already running` crash loop on every restart. This exercises
    that exact restart path directly, using its own container so it does
    not interfere with the shared `hermes_container` fixture.

    It also confirms the post-restart process is actually *serving*
    requests — not just that the container reports `running` — by calling
    `/health` and sending one `traceparent`-tagged request and checking the
    resulting span, the same way the fixture-based tests above do.
    """
    container_name = f"{CONTAINER_NAME}-restart"
    host_port = HOST_PORT + 1
    base_url = f"http://127.0.0.1:{host_port}"
    _docker("rm", "-f", container_name, check=False)

    run = _docker(
        "run", "-d", "--name", container_name,
        "-p", f"{host_port}:8642",
        "-e", "API_SERVER_ENABLED=true",
        "-e", "API_SERVER_HOST=0.0.0.0",
        "-e", f"API_SERVER_KEY={API_SERVER_KEY}",
        "-e", "OTEL_SERVICE_NAME=hermes-agent",
        "-e", "OTEL_TRACES_EXPORTER=console",
        "-e", "OTEL_METRICS_EXPORTER=none",
        "-e", "OTEL_LOGS_EXPORTER=none",
        IMAGE_TAG,
        "gateway", "run",
        check=False,
    )
    assert run.returncode == 0, f"docker run failed:\n{run.stdout}\n{run.stderr}"

    try:
        def _wait_up(seconds: int) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                status = _docker(
                    "inspect", "-f", "{{.State.Status}}", container_name,
                    check=False,
                ).stdout.strip()
                if status == "running":
                    return
                time.sleep(1)
            pytest.fail(
                f"container did not reach 'running' in time (last status: {status})"
            )

        def _wait_healthy(seconds: int) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    response = httpx.get(f"{base_url}/health", timeout=2)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            logs = _container_logs(container_name)
            pytest.fail(f"/health never returned 200 after restart:\n{logs}")

        _wait_up(STARTUP_TIMEOUT_SECONDS)
        _wait_healthy(STARTUP_TIMEOUT_SECONDS)

        # Restart the same container (same /opt/data as before) to force
        # reconcile-profiles to see prior run state, exactly like `docker
        # compose up` after any earlier successful run.
        _docker("stop", container_name, check=False)
        restart = _docker("start", container_name, check=False)
        assert restart.returncode == 0, restart.stderr

        _wait_up(STARTUP_TIMEOUT_SECONDS)

        # A crash loop shows up as repeated Restarting cycles; give it a
        # window and confirm it stays up rather than exiting again.
        time.sleep(10)
        status = _docker(
            "inspect", "-f", "{{.State.Status}}", container_name, check=False,
        ).stdout.strip()
        logs = _container_logs(container_name)
        assert status == "running", f"status={status}\n{logs}"
        assert "A gateway is already running" not in logs

        # Prove the post-restart process actually serves requests, not
        # just that the container reports "running".
        _wait_healthy(STARTUP_TIMEOUT_SECONDS)

        trace_id = uuid.uuid4().hex
        parent_id = uuid.uuid4().hex[:16]
        traceparent = f"00-{trace_id}-{parent_id}-01"

        response = _post_responses(
            headers={"traceparent": traceparent}, base_url=base_url,
        )
        assert response.status_code == 200

        time.sleep(SPAN_EXPORT_WAIT_SECONDS)
        spans = _exported_response_spans(container_name)
        matching = [
            span for span in spans
            if span["context"]["trace_id"] == f"0x{trace_id}"
        ]
        assert matching, (
            f"no exported span matched injected trace id after restart; "
            f"spans={spans}"
        )
        assert matching[-1]["parent_id"] == f"0x{parent_id}"
    finally:
        _docker("rm", "-f", container_name, check=False)
