"""HTTP client for the Orchestrator's existing `POST /dispatch` boundary.

This is the Slack Gateway's only outbound path to an Agent. It replaces
the direct Hermes Agent client this package used to hold: the Gateway now
hands a normalized `AgentRequest` to the Orchestrator, and the
Orchestrator decides which Agent runs it.

    Slack Gateway -> POST /dispatch -> Orchestrator -> Hermes Agent

No new schema is introduced here. The request body is exactly the shape
`services/orchestrator/src/orchestrator/http_server.py` already accepts --
`{"agent_name": ..., "request": <agent_request_to_dict(...)>}` -- and the
response is parsed with `agent_contracts`' own `agent_response_from_dict`,
so this client can never drift from the canonical contract without a test
failing.

## Trace context

The active OpenTelemetry context is injected into the outgoing request's
headers by the configured propagator (`opentelemetry.propagate.inject`),
exactly as this package's Hermes client did before it, and exactly as
`orchestrator.hermes_agent` does on the next hop. `traceparent` is never
constructed or parsed by hand. Injection into a *fresh* dict that is then
merged over the per-request headers means the propagator can only add
`traceparent`/`tracestate`; it can never replace a header this client set.

With no active recording span, `inject()` writes nothing and the request
simply carries no `traceparent` -- the correct behavior, not an error.
Tracing being unavailable never affects whether a dispatch is attempted or
how its result is interpreted.

`AgentRequest.trace_id` is a *different thing* and is not touched here:
this client neither reads it, derives it from trace context, nor writes it
back. See docs/slack-gateway/orchestrator-dispatch.md.

## Credentials

There is deliberately no `Authorization` header. `POST /dispatch` has no
authentication (docs/orchestrator/domain-model.md, "Authorization
boundary"), and the Hermes bearer credential is held only by the
Orchestrator, which owns the Hermes hop. Routing through the Orchestrator
therefore removes the Hermes credential from this service entirely rather
than forwarding it one hop further.
"""

from typing import Any

import httpx
from agent_contracts.agent_request import AgentRequest, agent_request_to_dict
from agent_contracts.agent_response import AgentResponse, agent_response_from_dict
from opentelemetry.propagate import inject

# The name the Orchestrator registers its real Hermes Agent adapter under
# (`HERMES_AGENT_NAME` in services/orchestrator/src/orchestrator/hermes_agent.py).
# Duplicated as a literal rather than imported because the two services
# share no package: the Slack Gateway depends on `agent-contracts`, not on
# `services/orchestrator`. A mismatch is not silent -- the Orchestrator
# answers an unregistered name with a defined `404 unknown_agent`, which
# this client surfaces as a dispatch failure.
HERMES_AGENT_NAME = "hermes"

# Slightly longer than the Orchestrator's own 300s timeout on its Hermes
# call (`DEFAULT_TIMEOUT_SECONDS` in orchestrator/hermes_agent.py), so a
# slow model run ends as the Orchestrator's own deliberate failure
# response rather than as a client-side timeout here that races it.
DEFAULT_TIMEOUT_SECONDS = 330.0


class OrchestratorClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def dispatch(
        self,
        agent_name: str,
        request: AgentRequest,
    ) -> AgentResponse:
        """Dispatch `request` to `agent_name` through `POST /dispatch`.

        Returns the Orchestrator's `AgentResponse` unwrapped. Raises
        `RuntimeError` -- with a bounded message that never carries the
        underlying exception, a response body, or the instruction text --
        for every failure of this boundary: the Orchestrator being
        unreachable, a request that times out, any non-2xx status
        (including `404 unknown_agent` and `500 internal_error`), a body
        that is not JSON, and a body that is not a valid `AgentResponse`.

        `RuntimeError` on purpose: it is the same failure type the Slack
        Gateway's message handler already caught around its previous
        outbound call, so the user-facing Slack behavior on failure is
        unchanged by the rewiring.
        """
        trace_headers: dict[str, str] = {}
        inject(trace_headers)

        try:
            response = self._client.post(
                "/dispatch",
                headers=trace_headers,
                json={
                    "agent_name": agent_name,
                    "request": agent_request_to_dict(request),
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RuntimeError(
                "Orchestrator returned "
                f"HTTP {error.response.status_code}"
            ) from error
        except httpx.TimeoutException as error:
            raise RuntimeError(
                "Orchestrator request timed out"
            ) from error
        except httpx.RequestError as error:
            raise RuntimeError(
                "Failed to connect to the Orchestrator"
            ) from error

        return _parse_agent_response(response)


def _parse_agent_response(response: httpx.Response) -> AgentResponse:
    try:
        payload: Any = response.json()
    except ValueError as error:
        # httpx raises json.JSONDecodeError (a ValueError) for a body that
        # is not JSON at all, and a UnicodeDecodeError-derived ValueError
        # for one that is not decodable -- both mean "not a usable
        # Orchestrator response" here.
        raise RuntimeError(
            "Orchestrator response was not valid JSON"
        ) from error

    try:
        return agent_response_from_dict(payload)
    except ValueError as error:
        # agent_response_from_dict rejects a non-mapping, a missing field,
        # an unknown field, and any field that fails AgentResponse's own
        # validation. Its message is a schema message, but it can quote a
        # caller-supplied value, so it is deliberately not carried into
        # this bounded message.
        raise RuntimeError(
            "Orchestrator response was not a valid AgentResponse"
        ) from error
