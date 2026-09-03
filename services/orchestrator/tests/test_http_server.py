"""Tests for the Orchestrator's minimal HTTP runtime boundary.

Runs the real orchestrator.http_server.OrchestratorHTTPServer on an
ephemeral localhost port in a background thread and drives it with real
HTTP requests (urllib, standard library only) -- this exercises the
actual http.server request/response cycle and the agent_contracts JSON
boundary, not just direct Python-level calls into Orchestrator.dispatch().
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse, agent_response_to_dict

from orchestrator.hermes_agent import HermesAgent
from orchestrator.http_server import create_server
from orchestrator.orchestrator import Orchestrator
from orchestrator.registry import AgentRegistry

from stub_agents import ExplodingAgent, RecordingAgent

KNOWN_AGENT_NAME = "recording"


def _sample_request_data(**overrides: object) -> dict:
    data = {
        "task_id": "task-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "instruction": "do something",
        "memory_scopes": [],
        "permissions": [],
        "trace_id": None,
    }
    data.update(overrides)
    return data


@pytest.fixture
def running_server() -> Iterator[tuple[str, AgentRegistry, RecordingAgent]]:
    registry = AgentRegistry()
    recording_agent = RecordingAgent(
        response=AgentResponse(status="completed", summary="recorded"),
    )
    registry.register(KNOWN_AGENT_NAME, recording_agent)

    server = create_server(Orchestrator(registry), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        yield base_url, registry, recording_agent
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(url: str, raw_body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=raw_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    return _post(url, json.dumps(payload).encode("utf-8"))


def test_health_returns_200_with_status_ok(running_server):
    base_url, _, _ = running_server

    with urllib.request.urlopen(f"{base_url}/health") as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json"
        assert json.loads(response.read()) == {"status": "ok"}


def test_dispatch_known_agent_returns_expected_agent_response(running_server):
    base_url, _, recording_agent = running_server

    status, body = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": KNOWN_AGENT_NAME, "request": _sample_request_data()},
    )

    assert status == 200
    assert body == agent_response_to_dict(recording_agent.response)
    assert recording_agent.calls == [AgentRequest(**_sample_request_data())]


def test_dispatch_unknown_agent_returns_404_and_does_not_call_any_agent(
    running_server,
):
    base_url, _, recording_agent = running_server

    status, body = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": "does-not-exist", "request": _sample_request_data()},
    )

    assert status == 404
    assert body["error"] == "unknown_agent"
    assert recording_agent.calls == []


def test_dispatch_invalid_json_body_returns_400(running_server):
    base_url, _, _ = running_server

    status, body = _post(f"{base_url}/dispatch", b"{not valid json")

    assert status == 400
    assert body["error"] == "invalid_json"


def test_dispatch_invalid_utf8_body_returns_400_not_500(running_server):
    base_url, _, _ = running_server

    # json.loads(bytes) decodes UTF-8 internally, so a body that isn't
    # valid UTF-8 raises UnicodeDecodeError rather than JSONDecodeError.
    # This must still be reported as invalid_json (400), not fall through
    # to the unexpected-error path (500) -- regression test for that.
    status, body = _post(f"{base_url}/dispatch", b"\xff\xfe\xfd")

    assert status == 400
    assert body["error"] == "invalid_json"


def test_dispatch_missing_agent_name_returns_400(running_server):
    base_url, _, _ = running_server

    status, body = _post_json(
        f"{base_url}/dispatch",
        {"request": _sample_request_data()},
    )

    assert status == 400
    assert body["error"] == "invalid_request"


def test_dispatch_non_object_body_returns_400(running_server):
    base_url, _, _ = running_server

    status, body = _post_json(f"{base_url}/dispatch", ["not", "an", "object"])

    assert status == 400
    assert body["error"] == "invalid_request"


def test_dispatch_invalid_agent_request_returns_400(running_server):
    base_url, _, recording_agent = running_server

    status, body = _post_json(
        f"{base_url}/dispatch",
        {
            "agent_name": KNOWN_AGENT_NAME,
            "request": _sample_request_data(task_id=""),
        },
    )

    assert status == 400
    assert body["error"] == "invalid_request"
    assert recording_agent.calls == []


def test_dispatch_agent_request_missing_field_returns_400(running_server):
    base_url, _, _ = running_server

    incomplete_request = _sample_request_data()
    del incomplete_request["trace_id"]

    status, body = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": KNOWN_AGENT_NAME, "request": incomplete_request},
    )

    assert status == 400
    assert body["error"] == "invalid_request"


def test_dispatch_agent_exception_returns_500_without_leaking_internal_details(
    running_server,
):
    base_url, registry, _ = running_server

    secret_message = "super secret internal detail"
    registry.register("exploding", ExplodingAgent(RuntimeError(secret_message)))

    status, body = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": "exploding", "request": _sample_request_data()},
    )

    serialized_body = json.dumps(body)

    assert status == 500
    assert body["error"] == "internal_error"
    assert secret_message not in serialized_body
    assert "Traceback" not in serialized_body
    assert "RuntimeError" not in serialized_body

    # The server must still be responsive after an Agent-raised exception --
    # one bad dispatch must not crash or hang the runtime process.
    with urllib.request.urlopen(f"{base_url}/health") as response:
        assert response.status == 200


def test_dispatch_to_unreachable_hermes_agent_returns_500_without_leaking_internal_details(
    running_server,
):
    # A real HermesAgent (not the abstract ExplodingAgent stub above),
    # pointed at a host nothing is listening on, exercises the same
    # generic-500 path through an actual network failure -- confirming
    # HermesAgent's own exception message (which could otherwise mention
    # the configured Hermes base URL) does not leak either, and that one
    # failed real dispatch does not crash or hang the runtime process.
    base_url, registry, _ = running_server

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    unreachable_port = sock.getsockname()[1]
    sock.close()
    unreachable_hermes_url = f"http://127.0.0.1:{unreachable_port}"

    registry.register(
        "hermes-unreachable",
        HermesAgent(base_url=unreachable_hermes_url, api_key="test-key"),
    )

    status, body = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": "hermes-unreachable", "request": _sample_request_data()},
    )

    serialized_body = json.dumps(body)

    assert status == 500
    assert body["error"] == "internal_error"
    assert unreachable_hermes_url not in serialized_body
    assert "Traceback" not in serialized_body
    assert "RuntimeError" not in serialized_body

    with urllib.request.urlopen(f"{base_url}/health") as response:
        assert response.status == 200


def test_dispatch_success_logs_correlation_identifiers(running_server, caplog):
    base_url, _, recording_agent = running_server

    caplog.set_level(logging.INFO, logger="orchestrator.http")

    request_data = _sample_request_data(
        task_id="corr-task-success-1",
        conversation_id="corr-conversation-success-1",
        trace_id="corr-trace-success-1",
    )

    status, _ = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": KNOWN_AGENT_NAME, "request": request_data},
    )

    assert status == 200
    assert "corr-task-success-1" in caplog.text
    assert "corr-conversation-success-1" in caplog.text
    assert "corr-trace-success-1" in caplog.text
    assert recording_agent.response.status in caplog.text


def test_dispatch_success_with_no_trace_id_logs_it_explicitly(running_server, caplog):
    base_url, _, _ = running_server

    caplog.set_level(logging.INFO, logger="orchestrator.http")

    request_data = _sample_request_data(task_id="corr-task-no-trace", trace_id=None)

    status, _ = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": KNOWN_AGENT_NAME, "request": request_data},
    )

    assert status == 200
    assert "corr-task-no-trace" in caplog.text
    # trace_id's absence must be represented explicitly, not omitted --
    # repr(None) renders as the bare word None (no quotes), so this also
    # confirms %r formatting is being used rather than silently dropping
    # a None field.
    assert "trace_id=None" in caplog.text


def test_dispatch_unknown_agent_logs_correlation_identifiers(running_server, caplog):
    base_url, _, _ = running_server

    caplog.set_level(logging.INFO, logger="orchestrator.http")

    request_data = _sample_request_data(task_id="corr-task-unknown-agent")

    status, _ = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": "does-not-exist", "request": request_data},
    )

    assert status == 404
    assert "corr-task-unknown-agent" in caplog.text
    assert "does-not-exist" in caplog.text


def test_dispatch_agent_exception_logs_correlation_identifiers(running_server, caplog):
    base_url, registry, _ = running_server

    caplog.set_level(logging.INFO, logger="orchestrator.http")

    registry.register("exploding-with-context", ExplodingAgent(RuntimeError("boom")))

    request_data = _sample_request_data(
        task_id="corr-task-exception",
        conversation_id="corr-conversation-exception",
        trace_id="corr-trace-exception",
    )

    status, _ = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": "exploding-with-context", "request": request_data},
    )

    assert status == 500
    assert "corr-task-exception" in caplog.text
    assert "corr-conversation-exception" in caplog.text
    assert "corr-trace-exception" in caplog.text


def test_dispatch_logging_never_contains_instruction_text(running_server, caplog):
    base_url, _, _ = running_server

    caplog.set_level(logging.INFO, logger="orchestrator.http")

    sentinel = "SENTINEL-INSTRUCTION-do-not-log-me-8f2a"
    request_data = _sample_request_data(instruction=sentinel)

    status, _ = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": KNOWN_AGENT_NAME, "request": request_data},
    )

    assert status == 200
    assert sentinel not in caplog.text


def test_dispatch_logging_never_contains_hermes_api_key(running_server, caplog):
    # Exercises the real HermesAgent exception path (same idiom as
    # test_dispatch_to_unreachable_hermes_agent_returns_500_without_leaking_internal_details
    # above) with a distinctive api_key, confirming the correlation logging
    # added in this slice never captures it end to end.
    base_url, registry, _ = running_server

    caplog.set_level(logging.INFO, logger="orchestrator.http")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    unreachable_port = sock.getsockname()[1]
    sock.close()

    secret_api_key = "SENTINEL-API-KEY-do-not-log-me-3c91"
    registry.register(
        "hermes-unreachable-logging",
        HermesAgent(
            base_url=f"http://127.0.0.1:{unreachable_port}",
            api_key=secret_api_key,
        ),
    )

    request_data = _sample_request_data(
        task_id="corr-task-hermes-failure",
        conversation_id="corr-conversation-hermes-failure",
    )

    status, _ = _post_json(
        f"{base_url}/dispatch",
        {"agent_name": "hermes-unreachable-logging", "request": request_data},
    )

    assert status == 500
    assert secret_api_key not in caplog.text
    # The correlation identifiers ARE expected here -- this failure path is
    # exactly the real-world case (Hermes/Ollama unreachable) this slice
    # exists to make diagnosable.
    assert "corr-task-hermes-failure" in caplog.text
    assert "corr-conversation-hermes-failure" in caplog.text


def test_unknown_path_returns_404(running_server):
    base_url, _, _ = running_server

    try:
        urllib.request.urlopen(f"{base_url}/does-not-exist")
    except urllib.error.HTTPError as error:
        assert error.code == 404
        assert json.loads(error.read())["error"] == "not_found"
    else:
        pytest.fail("Expected an HTTPError for an unknown path")
