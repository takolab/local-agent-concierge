"""Tests for the Orchestrator's minimal HTTP runtime boundary.

Runs the real orchestrator.http_server.OrchestratorHTTPServer on an
ephemeral localhost port in a background thread and drives it with real
HTTP requests (urllib, standard library only) -- this exercises the
actual http.server request/response cycle and the agent_contracts JSON
boundary, not just direct Python-level calls into Orchestrator.dispatch().
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse, agent_response_to_dict

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


def test_unknown_path_returns_404(running_server):
    base_url, _, _ = running_server

    try:
        urllib.request.urlopen(f"{base_url}/does-not-exist")
    except urllib.error.HTTPError as error:
        assert error.code == 404
        assert json.loads(error.read())["error"] == "not_found"
    else:
        pytest.fail("Expected an HTTPError for an unknown path")
