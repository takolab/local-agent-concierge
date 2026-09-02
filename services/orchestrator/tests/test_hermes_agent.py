"""Tests for HermesAgent, the Agent adapter that calls a real Hermes Agent.

Runs a minimal stub HTTP server (standard library only, same idiom as
test_http_server.py's `running_server` fixture) standing in for Hermes
Agent's `/v1/responses` endpoint, so these tests need no live Ollama or
Hermes Agent container.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse

from orchestrator.hermes_agent import HermesAgent

API_KEY = "test-hermes-key"


class _StubHermesServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _StubHermesRequestHandler)
        self.status_code = 200
        self.response_body = b'{"output_text": "default stub response"}'
        self.last_request: dict[str, Any] | None = None


class _StubHermesRequestHandler(BaseHTTPRequestHandler):
    server: _StubHermesServer

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            parsed_body: Any = json.loads(raw_body)
        except json.JSONDecodeError:
            parsed_body = None

        self.server.last_request = {
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": parsed_body,
        }

        self.send_response(self.server.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.server.response_body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture
def stub_hermes() -> Iterator[_StubHermesServer]:
    server = _StubHermesServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _unreachable_base_url() -> str:
    """Return an http://127.0.0.1:<port> URL with nothing listening on it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _sample_request(**overrides: object) -> AgentRequest:
    data = {
        "task_id": "task-1",
        "user_id": "user-1",
        "conversation_id": "conversation-1",
        "instruction": "Find a two-hour study slot next week",
        "memory_scopes": (),
        "permissions": (),
        "trace_id": None,
    }
    data.update(overrides)
    return AgentRequest(**data)


def test_successful_dispatch_extracts_direct_output_text(stub_hermes):
    stub_hermes.response_body = json.dumps(
        {"output_text": "  hello world  "}
    ).encode("utf-8")
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    response = agent.handle(_sample_request())

    assert response == AgentResponse(status="completed", summary="hello world")


def test_successful_dispatch_extracts_output_items_text_when_no_direct_text(
    stub_hermes,
):
    stub_hermes.response_body = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "part one"}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "part two"}],
                },
            ]
        }
    ).encode("utf-8")
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    response = agent.handle(_sample_request())

    assert response == AgentResponse(
        status="completed", summary="part one\npart two"
    )


def test_request_sent_to_hermes_has_expected_shape(stub_hermes):
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    agent.handle(
        _sample_request(
            instruction="Find a two-hour study slot next week",
            conversation_id="conversation-42",
        )
    )

    sent = stub_hermes.last_request
    assert sent is not None
    assert sent["path"] == "/v1/responses"
    assert sent["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["body"] == {
        "model": "hermes-agent",
        "input": "Find a two-hour study slot next week",
        "conversation": "conversation-42",
        "store": True,
    }


def test_http_error_status_raises_runtime_error(stub_hermes):
    stub_hermes.status_code = 500
    stub_hermes.response_body = b"internal error"
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        agent.handle(_sample_request())


def test_connection_error_raises_runtime_error():
    agent = HermesAgent(base_url=_unreachable_base_url(), api_key=API_KEY)

    with pytest.raises(RuntimeError, match="Failed to connect"):
        agent.handle(_sample_request())


def test_malformed_json_response_raises_runtime_error(stub_hermes):
    stub_hermes.response_body = b"not valid json"
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    with pytest.raises(RuntimeError, match="not valid JSON"):
        agent.handle(_sample_request())


def test_non_object_json_response_raises_runtime_error(stub_hermes):
    stub_hermes.response_body = b"[1, 2, 3]"
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    with pytest.raises(RuntimeError, match="not a JSON object"):
        agent.handle(_sample_request())


def test_response_without_output_text_raises_runtime_error(stub_hermes):
    stub_hermes.response_body = json.dumps({"output": []}).encode("utf-8")
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
        api_key=API_KEY,
    )

    with pytest.raises(RuntimeError, match="did not contain output text"):
        agent.handle(_sample_request())
