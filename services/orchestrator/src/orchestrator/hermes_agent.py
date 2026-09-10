"""Agent adapter that dispatches to the real Hermes Agent service.

Implements orchestrator.agent.Agent by calling Hermes Agent's existing
`/v1/responses` HTTP API -- the same API apps/slack-gateway's HermesClient
(src/slack_gateway/hermes_client.py) used to call directly. That client
has since been removed: the Slack Gateway now dispatches through this
service instead, making this adapter the only caller of Hermes Agent and
the only holder of its credential (see
docs/slack-gateway/orchestrator-dispatch.md). This is the second
registered Agent, alongside the synthetic orchestrator.dev_agents.EchoAgent
("dev-echo"); registering it does not remove or change EchoAgent.

Uses the standard library (urllib) for the HTTP call itself, matching
http_server.py's own "why the standard library instead of a framework"
rationale -- the only third-party code involved is OpenTelemetry, for
tracing.

Every call is wrapped in a CLIENT span whose trace context is injected
into the outgoing request's headers, so this hop joins the same
distributed trace as the incoming `POST /dispatch` request that caused it
(see orchestrator.telemetry). `AgentRequest.trace_id` is *not* involved:
it is forwarded nowhere and read nowhere here, exactly as before. See
docs/orchestrator/domain-model.md for the full design notes and what this
deliberately does not do yet.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse

from orchestrator import telemetry

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

        Every one of those failures is also marked on the CLIENT span,
        with a fixed `error.type` value and never the exception itself;
        the exception raised to the caller is unchanged in type and
        message.
        """
        with telemetry.trace_hermes_request():
            response_data = self._call_hermes(request)

            try:
                summary = _extract_output_text(response_data)
            except RuntimeError:
                telemetry.mark_current_span_error(
                    error_type=telemetry.ERROR_TYPE_HERMES_INVALID_RESPONSE,
                )
                raise

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

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Injected into its own dict and merged in this direction on
        # purpose: whatever the configured propagator writes can add
        # `traceparent`/`tracestate` but can never replace `Authorization`
        # or `Content-Type` above.
        headers.update(telemetry.trace_context_headers())

        http_request = urllib.request.Request(
            f"{self._base_url}/v1/responses",
            data=body,
            method="POST",
            headers=headers,
        )

        try:
            with _NO_REDIRECT_OPENER.open(
                http_request, timeout=self._timeout_seconds
            ) as response:
                response_body = response.read()
        except urllib.error.HTTPError as error:
            telemetry.mark_current_span_error(
                error_type=telemetry.ERROR_TYPE_HERMES_HTTP_STATUS,
                http_status_code=error.code,
            )
            raise RuntimeError(
                f"Hermes API returned HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            telemetry.mark_current_span_error(
                error_type=telemetry.ERROR_TYPE_HERMES_CONNECTION,
            )
            raise RuntimeError("Failed to connect to Hermes API") from error

        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            # json.loads(bytes) decodes UTF-8 internally before parsing, so
            # a non-UTF-8 body raises UnicodeDecodeError, not
            # JSONDecodeError -- both mean "not a usable Hermes response"
            # from this adapter's point of view. Mirrors http_server.py's
            # own handling of the same underlying quirk.
            telemetry.mark_current_span_error(
                error_type=telemetry.ERROR_TYPE_HERMES_INVALID_RESPONSE,
            )
            raise RuntimeError(
                "Hermes API response was not valid JSON"
            ) from error

        if not isinstance(payload, dict):
            telemetry.mark_current_span_error(
                error_type=telemetry.ERROR_TYPE_HERMES_INVALID_RESPONSE,
            )
            raise RuntimeError("Hermes API response was not a JSON object")

        return payload


def _extract_output_text(payload: dict[str, Any]) -> str:
    """Extract Hermes' output text -- ported from the extraction logic that
    lived in apps/slack-gateway's HermesClient
    (src/slack_gateway/hermes_client.py, since removed) because the two
    services shared no common package to import it from. It is now the
    only copy.
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
