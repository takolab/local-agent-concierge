"""Agent adapter that dispatches to the real Hermes Agent service.

Implements orchestrator.agent.Agent by calling Hermes Agent's existing
`/v1/responses` HTTP API -- the same API apps/slack-gateway's HermesClient
(src/slack_gateway/hermes_client.py) already calls. This is the second
registered Agent, alongside the synthetic orchestrator.dev_agents.EchoAgent
("dev-echo"); registering it does not remove or change EchoAgent.

Uses only the standard library (urllib), matching http_server.py's own
"why the standard library instead of a framework" rationale -- this keeps
services/orchestrator at zero non-agent_contracts runtime dependencies.

No OpenTelemetry / trace-context propagation is implemented here: the
Orchestrator has no tracing instrumentation of its own yet (tracked under
Milestone 9), so this adapter makes plain HTTP calls with no `traceparent`
injection. See docs/orchestrator/domain-model.md for the full design notes
and what this deliberately does not do yet.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse

HERMES_AGENT_NAME = "hermes"

DEFAULT_TIMEOUT_SECONDS = 300.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise (as HTTPError) instead of automatically following a 3xx.

    urllib's default HTTPRedirectHandler follows 301/302/303 (silently
    converting POST to GET) and carries all non-content headers -- Auth-
    orization included -- onto the redirect target, even across hosts.
    Reused unchanged from stdlib apart from this: an Agent's declared
    contract is 2xx-maps / non-2xx-raises, and this adapter's caller-
    supplied bearer credential must never be sent anywhere but the
    configured Hermes base_url.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class HermesAgent:
    """Dispatches an AgentRequest to a real Hermes Agent's /v1/responses API.

    base_url and api_key are supplied by the caller (see __main__.py) --
    this class does not read environment variables itself.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def handle(self, request: AgentRequest) -> AgentResponse:
        """Call Hermes Agent and map its response to an AgentResponse.

        Raises RuntimeError (not caught here) if Hermes is unreachable,
        returns a non-2xx status (redirects included -- never followed,
        see _NoRedirectHandler above), or returns a body this adapter
        cannot extract output text from. This intentionally mirrors
        Orchestrator.dispatch()'s existing behavior of letting an Agent's
        exception propagate uncaught -- the HTTP layer's existing generic
        500 handling (http_server.py) already covers it without needing a
        new AgentResponse status value.
        """
        response_data = self._call_hermes(request)
        summary = _extract_output_text(response_data)
        return AgentResponse(status="completed", summary=summary)

    def _call_hermes(self, request: AgentRequest) -> dict[str, Any]:
        body = json.dumps(
            {
                "model": "hermes-agent",
                "input": request.instruction,
                "conversation": request.conversation_id,
                "store": True,
            }
        ).encode("utf-8")

        http_request = urllib.request.Request(
            f"{self._base_url}/v1/responses",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with _NO_REDIRECT_OPENER.open(
                http_request, timeout=self._timeout_seconds
            ) as response:
                response_body = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Hermes API returned HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError("Failed to connect to Hermes API") from error

        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            # json.loads(bytes) decodes UTF-8 internally before parsing, so
            # a non-UTF-8 body raises UnicodeDecodeError, not
            # JSONDecodeError -- both mean "not a usable Hermes response"
            # from this adapter's point of view. Mirrors http_server.py's
            # own handling of the same underlying quirk.
            raise RuntimeError(
                "Hermes API response was not valid JSON"
            ) from error

        if not isinstance(payload, dict):
            raise RuntimeError("Hermes API response was not a JSON object")

        return payload


def _extract_output_text(payload: dict[str, Any]) -> str:
    """Extract Hermes' output text -- ported from HermesClient's own logic
    (apps/slack-gateway/src/slack_gateway/hermes_client.py) since the two
    services share no common package to import it from.
    """
    direct_output = payload.get("output_text")

    if isinstance(direct_output, str) and direct_output.strip():
        return direct_output.strip()

    text_parts: list[str] = []

    for output_item in payload.get("output", []):
        if not isinstance(output_item, dict):
            continue

        if output_item.get("type") != "message":
            continue

        for content_item in output_item.get("content", []):
            if not isinstance(content_item, dict):
                continue

            if content_item.get("type") != "output_text":
                continue

            text = content_item.get("text")

            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())

    result = "\n".join(text_parts).strip()

    if not result:
        raise RuntimeError("Hermes API response did not contain output text")

    return result
