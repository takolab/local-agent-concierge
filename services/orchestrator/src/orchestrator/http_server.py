"""Minimal, provisional HTTP transport boundary for the Orchestrator.

Exposes exactly two routes over Python's standard library `http.server`:

- `GET /health` -- liveness only. No business logic and no Agent
  connectivity check; a 200 means the HTTP process is running, nothing
  more.
- `POST /dispatch` -- deserializes an `AgentRequest` from the JSON body
  using `agent_contracts`'s existing (de)serializers, calls the existing
  `Orchestrator.dispatch(agent_name, request)` unchanged, and serializes
  the resulting `AgentResponse` back to JSON.

This is a thin transport adapter only:

    HTTP request -> AgentRequest deserialization -> Orchestrator.dispatch()
    -> AgentResponse serialization -> HTTP response

No new domain schema is introduced, and no authentication or authorization
is implemented here. See docs/orchestrator/domain-model.md for the full
runtime HTTP boundary documentation, including what this deliberately
does not do yet.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent_contracts.agent_request import agent_request_from_dict
from agent_contracts.agent_response import agent_response_to_dict

from orchestrator.orchestrator import Orchestrator
from orchestrator.registry import UnknownAgentError

logger = logging.getLogger("orchestrator.http")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8700


class OrchestratorHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that carries the Orchestrator it dispatches to.

    One thread per connection; safe because Orchestrator/AgentRegistry are
    not mutated once serving begins (registration happens at startup,
    before serve_forever() is called).
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        orchestrator: Orchestrator,
    ) -> None:
        super().__init__(server_address, OrchestratorRequestHandler)
        self.orchestrator = orchestrator


class OrchestratorRequestHandler(BaseHTTPRequestHandler):
    server: OrchestratorHTTPServer

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return

            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown path.")
        except Exception:
            self._handle_unexpected_error()

    def do_POST(self) -> None:
        try:
            if self.path != "/dispatch":
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Unknown path.")
                return

            self._handle_dispatch()
        except Exception:
            self._handle_unexpected_error()

    def _handle_dispatch(self) -> None:
        body = self._read_body()
        if body is None:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body was not valid JSON.",
            )
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body was not valid JSON.",
            )
            return

        if not isinstance(payload, dict):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Request body must be a JSON object with 'agent_name' and "
                "'request' fields.",
            )
            return

        agent_name = payload.get("agent_name")
        request_data = payload.get("request")

        if not isinstance(agent_name, str) or not agent_name.strip():
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "'agent_name' must be a non-empty string.",
            )
            return

        if not isinstance(request_data, dict):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "'request' must be a JSON object.",
            )
            return

        try:
            agent_request = agent_request_from_dict(request_data)
        except ValueError as error:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
            return

        try:
            agent_response = self.server.orchestrator.dispatch(
                agent_name, agent_request
            )
        except UnknownAgentError:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "unknown_agent",
                f"No agent is registered under {agent_name!r}.",
            )
            return

        self._send_json(HTTPStatus.OK, agent_response_to_dict(agent_response))

    def _read_body(self) -> bytes | None:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None

        if content_length < 0:
            return None

        return self.rfile.read(content_length)

    def _handle_unexpected_error(self) -> None:
        # Reached for anything not already translated into a defined 4xx
        # above (e.g. an Agent.handle() raising something other than
        # UnknownAgentError). Logged in full server-side, but the response
        # body stays generic and bounded -- never the exception message or
        # a traceback -- so an internal failure detail can never reach an
        # HTTP caller.
        logger.exception(
            "Unhandled error while processing %s %s", self.command, self.path
        )
        try:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "An unexpected error occurred while dispatching the request.",
            )
        except Exception:
            logger.exception(
                "Failed to send an error response after an unhandled error"
            )

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, error: str, detail: str) -> None:
        self._send_json(status, {"error": error, "detail": detail})

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


def create_server(
    orchestrator: Orchestrator,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> OrchestratorHTTPServer:
    """Build an OrchestratorHTTPServer bound to (host, port).

    Does not start serving; call serve_forever() (typically in a
    background thread) to begin accepting connections.
    """
    return OrchestratorHTTPServer((host, port), orchestrator)
