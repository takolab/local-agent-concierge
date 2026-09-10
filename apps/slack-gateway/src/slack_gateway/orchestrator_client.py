"""HTTP client for the Orchestrator's existing `POST /dispatch` boundary.

This is the Slack Gateway's only outbound path to an Agent. It replaces
the direct Hermes Agent client this package used to hold: the Gateway now
hands a normalized `AgentRequest` to the Orchestrator, which dispatches it
through its registered Agent boundary.

    Slack Gateway -> POST /dispatch -> Orchestrator -> Hermes Agent

**The Gateway still selects the Agent.** It passes the fixed
`agent_name` `"hermes"` (see `HERMES_AGENT_NAME` below); the Orchestrator
owns dispatch through the registered Agent boundary, but performs no
request classification and no Agent selection. What moved to the
Orchestrator is the dispatch, not the choice -- Milestone 7's "Move
agent-selection responsibility out of the Slack Gateway" is still open.

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

## Two outcomes, not one failure

A failure of this boundary is not always evidence that nothing happened.
This module therefore raises two sibling exception types, both
`RuntimeError` subclasses (so any caller catching `RuntimeError` still
catches everything):

- `DispatchFailedError` -- the Orchestrator refused the request, or it
  provably never left this process. Nothing ran.
- `DispatchOutcomeUnknownError` -- contact was lost at a point where the
  request may already have been delivered, or the Agent demonstrably ran
  and its result could not be read. **Whether work happened, and whether
  it had a side effect, is unknown.**

They are siblings rather than parent/child on purpose: if the unknown case
were a subclass of the failed case, an `except DispatchFailedError` would
silently swallow it, which is exactly the confusion the split exists to
prevent.

Classification is fail-safe. Only the httpx errors that provably precede
delivery (`_NOT_DELIVERED_ERRORS` below) and an explicit HTTP error status
are treated as definite failures; everything else -- including anything
unclassified -- is reported as an unknown outcome, because presenting an
ambiguous outcome as a safe retry is the more dangerous mistake. The Agent
reachable through this path today is tool-capable (Hermes Agent's
`/v1/responses` runs its configured toolsets and MCP servers), so a retry
after an ambiguous outcome can duplicate a real side effect.

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

# Set above the Orchestrator's own 300s timeout on its Hermes call
# (`DEFAULT_TIMEOUT_SECONDS` in orchestrator/hermes_agent.py) so that, in
# the ordinary case, the Gateway is still waiting when the Orchestrator
# gives up and answers with its own deliberate failure response.
#
# That is best-effort ordering, NOT a guarantee, and must not be relied on
# as one. httpx's single timeout value configures connect/read/write/pool
# *inactivity* timeouts, not a total end-to-end request deadline -- and the
# Orchestrator's own timeout is likewise socket-level, not a deadline. So
# `330 > 300` does not establish that the Orchestrator always finishes
# first. See `dispatch`'s note on what a timeout does and does not tell the
# caller, and docs/slack-gateway/orchestrator-dispatch.md ("A failed
# dispatch does not mean the work stopped").
DEFAULT_TIMEOUT_SECONDS = 330.0


class DispatchFailedError(RuntimeError):
    """The dispatch definitely did not run.

    Raised when the Orchestrator answered with an error status (it was
    reached, and refused or failed the request deliberately), or when the
    request provably never left this process. Retrying is safe with
    respect to side effects: no Agent was reached by *this* attempt.

    A `RuntimeError` subclass so the Slack Gateway's existing
    `except RuntimeError` handling is unchanged.
    """


class DispatchOutcomeUnknownError(RuntimeError):
    """The dispatch may have run; the outcome is unknown.

    Raised when contact was lost at a point where the request may already
    have been delivered (read/write timeouts, transport errors after the
    request was written), or when the Orchestrator answered but the answer
    could not be read as an `AgentResponse` -- in which case the Agent
    *did* run and only the result is lost.

    Deliberately **not** a subclass of `DispatchFailedError`: this must
    never be caught by handlers written for a definite failure. The caller
    is expected to tell the user the outcome is unknown rather than invite
    an immediate retry.
    """


# httpx errors that provably occur before any byte of the request is
# delivered: no connection was established (`ConnectError`,
# `ConnectTimeout`, `ProxyError`), none was ever taken from the pool
# (`PoolTimeout`), or the request was rejected locally before being sent
# (`UnsupportedProtocol`, `LocalProtocolError`).
#
# Everything else in the `httpx.RequestError` family is deliberately left
# out: `WriteError`/`WriteTimeout` can fire mid-write, and
# `ReadError`/`ReadTimeout`/`CloseError`/`RemoteProtocolError`/
# `DecodingError` all occur after the request has been written. This tuple
# is an allowlist for "definitely not delivered", so adding a new httpx
# error class to the library cannot silently make an ambiguous outcome
# look safe.
_NOT_DELIVERED_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)

# The complete `error.type` vocabulary the Slack Gateway records for a
# dispatch failure. `ERROR_TYPE_DISPATCH_FAILED` keeps its original value
# so existing telemetry for the definite-failure case is unchanged.
ERROR_TYPE_DISPATCH_FAILED = "orchestrator.request_error"
ERROR_TYPE_OUTCOME_UNKNOWN = "orchestrator.outcome_unknown"


def dispatch_error_type(error: BaseException) -> str:
    """Classify a dispatch failure into the bounded `error.type` vocabulary.

    Fail-safe by construction: only an explicit `DispatchFailedError` is
    reported as a definite failure. Anything else -- including a
    `RuntimeError` from somewhere this module did not classify -- is
    reported as an unknown outcome, because under-reporting ambiguity is
    the dangerous direction and over-reporting it merely makes the
    response more cautious.
    """
    if isinstance(error, DispatchFailedError):
        return ERROR_TYPE_DISPATCH_FAILED

    return ERROR_TYPE_OUTCOME_UNKNOWN


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

        Returns the Orchestrator's `AgentResponse` unwrapped. Every
        failure of this boundary raises a `RuntimeError` subclass whose
        message is bounded -- never the underlying exception, a response
        body, or the instruction text -- and whose *type* says whether the
        request could have run:

        `DispatchFailedError` (nothing ran):

        - the Orchestrator answered with an error status, including
          `404 unknown_agent`, `400 invalid_request` and
          `500 internal_error`;
        - the request provably never left this process
          (`_NOT_DELIVERED_ERRORS`).

        `DispatchOutcomeUnknownError` (it may have run):

        - the request timed out reading or writing -- it may have been
          delivered and may still be executing;
        - any other transport error, which can fire after the request was
          written;
        - the Orchestrator answered `2xx` with a body that is not a valid
          `AgentResponse` -- here the Agent demonstrably *did* run and
          only its result was lost.

        Both are `RuntimeError` subclasses, so the Slack Gateway's
        existing `except RuntimeError` handling still catches every case;
        the type only lets the caller say the right thing to the user.

        **A raised error reports this client's outcome, not the downstream
        one.** Only an explicit error status from the Orchestrator is
        evidence about what happened on the other side -- and even a
        `500` does not distinguish "the Agent was never successfully
        called" from "the Agent raised after doing work", because the
        Orchestrator returns one generic body for both. See
        docs/slack-gateway/orchestrator-dispatch.md.
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
            raise DispatchFailedError(
                "Orchestrator returned "
                f"HTTP {error.response.status_code}"
            ) from error
        except _NOT_DELIVERED_ERRORS as error:
            # Checked before the TimeoutException clause below, because
            # ConnectTimeout and PoolTimeout are themselves timeouts --
            # but ones that provably happened before delivery.
            raise DispatchFailedError(
                "Failed to connect to the Orchestrator"
            ) from error
        except httpx.TimeoutException as error:
            raise DispatchOutcomeUnknownError(
                "Orchestrator request timed out"
            ) from error
        except httpx.RequestError as error:
            raise DispatchOutcomeUnknownError(
                "Lost contact with the Orchestrator"
            ) from error

        return _parse_agent_response(response)


def _parse_agent_response(response: httpx.Response) -> AgentResponse:
    # Both failures below are an *unknown outcome*, not a failure: the
    # Orchestrator answered 2xx, which it only does after
    # `Orchestrator.dispatch()` returned an `AgentResponse` -- so the Agent
    # ran to completion and only its result is unreadable here. Retrying
    # would re-run it.
    try:
        payload: Any = response.json()
    except ValueError as error:
        # httpx raises json.JSONDecodeError (a ValueError) for a body that
        # is not JSON at all, and a UnicodeDecodeError-derived ValueError
        # for one that is not decodable -- both mean "not a usable
        # Orchestrator response" here.
        raise DispatchOutcomeUnknownError(
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
        raise DispatchOutcomeUnknownError(
            "Orchestrator response was not a valid AgentResponse"
        ) from error
