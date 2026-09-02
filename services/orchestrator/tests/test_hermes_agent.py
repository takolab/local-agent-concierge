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
        self.extra_response_headers: dict[str, str] = {}
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

        self._record_request(parsed_body)
        self._respond()

    def do_GET(self) -> None:
        # urllib's default (unpatched) redirect handling converts a POST
        # to a GET when following a 301/302/303 -- this stub must record
        # GET requests too, or a regression that reintroduces
        # redirect-following in HermesAgent could go undetected by a
        # last_request-based "was the redirect target ever contacted"
        # assertion (do_POST alone would never see it). Found by review
        # on this exact test.
        self._record_request(body=None)
        self._respond()

    def _record_request(self, body: Any) -> None:
        self.server.last_request = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        }

    def _respond(self) -> None:
        self.send_response(self.server.status_code)
        self.send_header("Content-Type", "application/json")
        for header_name, header_value in self.server.extra_response_headers.items():
            self.send_header(header_name, header_value)
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


def test_redirect_response_raises_and_does_not_forward_credentials(stub_hermes):
    # A second stub server stands in for a redirect target. If HermesAgent
    # ever followed the redirect (urllib's default behavior), this server
    # would receive a request -- and the Authorization header along with
    # it, since urllib's default redirect handling does not strip it even
    # across hosts. Confirmed empirically against unpatched code before
    # this fix: the redirect was followed, POST silently became GET, and
    # the bearer credential was forwarded to the redirect target.
    redirect_target = _StubHermesServer(("127.0.0.1", 0))
    redirect_target_thread = threading.Thread(
        target=redirect_target.serve_forever, daemon=True
    )
    redirect_target_thread.start()

    try:
        redirect_target_url = (
            f"http://127.0.0.1:{redirect_target.server_address[1]}/v1/responses"
        )
        stub_hermes.status_code = 302
        stub_hermes.response_body = b""
        stub_hermes.extra_response_headers = {"Location": redirect_target_url}

        agent = HermesAgent(
            base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
            api_key=API_KEY,
        )

        with pytest.raises(RuntimeError, match="HTTP 302"):
            agent.handle(_sample_request())

        assert redirect_target.last_request is None, (
            "the redirect target must never be contacted -- if it is, "
            "the Authorization credential was forwarded to it"
        )
    finally:
        redirect_target.shutdown()
        redirect_target.server_close()
        redirect_target_thread.join(timeout=5)


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
