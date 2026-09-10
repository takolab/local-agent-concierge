"""Contract tests for the Slack Gateway -> Orchestrator dispatch boundary.

Covers the four things this boundary is responsible for:

1. the outgoing request conforms to the Orchestrator's existing
   `POST /dispatch` body and to the canonical `AgentRequest` contract;
2. W3C Trace Context is injected by the propagator, and its absence is
   not a failure;
3. a valid `AgentResponse` is returned unwrapped;
4. every failure of this boundary becomes a `RuntimeError` whose message
   carries no response body, instruction text, or underlying exception.

All values are synthetic sentinels -- no real credentials, tokens, or
personal data.
"""

import json
from typing import Any

import httpx
import pytest
from agent_contracts.agent_request import AgentRequest, agent_request_from_dict
from agent_contracts.agent_response import AgentResponse
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from slack_gateway import telemetry
from slack_gateway.orchestrator_client import (
    HERMES_AGENT_NAME,
    OrchestratorClient,
)

SENTINEL_INSTRUCTION = "synthetic-instruction-sentinel"

AGENT_RESPONSE_BODY = {
    "status": "completed",
    "summary": "synthetic summary",
    "proposed_actions": [],
    "memory_candidates": [],
}


@pytest.fixture
def tracer(monkeypatch: pytest.MonkeyPatch) -> trace.Tracer:
    tracer_provider = TracerProvider()
    test_tracer = tracer_provider.get_tracer("slack_gateway.test")

    monkeypatch.setattr(
        telemetry.trace,
        "get_tracer",
        lambda _: test_tracer,
    )

    return test_tracer


def _agent_request() -> AgentRequest:
    return AgentRequest(
        task_id="Ev-synthetic-1",
        user_id="U-synthetic",
        conversation_id="slack:T-synthetic:C-synthetic:1700000000.000100",
        instruction=SENTINEL_INSTRUCTION,
        memory_scopes=(),
        permissions=(),
        trace_id=None,
    )


def _client_with_recorder(
    captured: dict[str, Any],
    response_factory=lambda: httpx.Response(200, json=AGENT_RESPONSE_BODY),
) -> OrchestratorClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["url"] = request.url
        captured["method"] = request.method
        captured["content"] = request.content
        return response_factory()

    return OrchestratorClient(
        base_url="http://orchestrator:8700",
        transport=httpx.MockTransport(handler),
    )


def _client_raising(error: Exception) -> OrchestratorClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return OrchestratorClient(
        base_url="http://orchestrator:8700",
        transport=httpx.MockTransport(handler),
    )


def test_dispatch_posts_to_the_orchestrator_dispatch_endpoint() -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert captured["method"] == "POST"
    assert str(captured["url"]) == "http://orchestrator:8700/dispatch"

    # The previous, now-removed direct path. Asserted explicitly so a
    # regression back to it cannot pass silently.
    assert "/v1/responses" not in str(captured["url"])


def test_dispatch_body_matches_the_orchestrator_request_contract() -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    request = _agent_request()
    client.dispatch(HERMES_AGENT_NAME, request)

    payload = json.loads(captured["content"])

    assert set(payload) == {"agent_name", "request"}
    assert payload["agent_name"] == HERMES_AGENT_NAME

    # Exactly the 7 canonical AgentRequest fields, with the values the
    # caller supplied -- no Slack-specific extra field, no renaming, and
    # no silent reinterpretation.
    assert payload["request"] == {
        "task_id": "Ev-synthetic-1",
        "user_id": "U-synthetic",
        "conversation_id": (
            "slack:T-synthetic:C-synthetic:1700000000.000100"
        ),
        "instruction": SENTINEL_INSTRUCTION,
        "memory_scopes": [],
        "permissions": [],
        "trace_id": None,
    }


def test_dispatch_body_round_trips_through_the_canonical_deserializer() -> None:
    """The Orchestrator parses the body with `agent_request_from_dict`.

    Asserting the same deserializer accepts what this client sends -- and
    reconstructs the identical AgentRequest -- pins the wire contract to
    the shared package rather than to this test's own expectations.
    """
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    request = _agent_request()
    client.dispatch(HERMES_AGENT_NAME, request)

    payload = json.loads(captured["content"])

    assert agent_request_from_dict(payload["request"]) == request


def test_dispatch_injects_matching_traceparent(
    tracer: trace.Tracer,
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    with telemetry.trace_orchestrator_request() as dispatch_span:
        expected_trace_id = format(
            dispatch_span.get_span_context().trace_id, "032x"
        )
        expected_span_id = format(
            dispatch_span.get_span_context().span_id, "016x"
        )

        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    traceparent = captured["headers"]["traceparent"]
    version, trace_id, parent_id, flags = traceparent.split("-")

    assert version == "00"
    assert trace_id == expected_trace_id
    assert parent_id == expected_span_id
    assert len(flags) == 2


def test_dispatch_preserves_existing_headers(
    tracer: trace.Tracer,
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    with telemetry.trace_orchestrator_request():
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    headers = captured["headers"]

    assert headers["Content-Type"] == "application/json"
    assert "traceparent" in headers


def test_dispatch_without_active_span_still_succeeds(
    tracer: trace.Tracer,
) -> None:
    """Tracing being unavailable is not dispatch being unavailable."""
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    response = client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert "traceparent" not in captured["headers"]
    assert response.status == "completed"


def test_dispatch_sends_no_credential_header(
    tracer: trace.Tracer,
) -> None:
    """The Hermes bearer credential must not reach this hop at all.

    The Orchestrator owns the Hermes call and holds that credential; the
    Slack Gateway no longer has it, and `POST /dispatch` has no
    authentication of its own to send one to.
    """
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    with telemetry.trace_orchestrator_request():
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    header_names = {name.lower() for name in captured["headers"]}

    assert "authorization" not in header_names
    assert not any("token" in name for name in header_names)
    assert not any("api-key" in name for name in header_names)


def test_dispatch_returns_the_agent_response() -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(
        captured,
        response_factory=lambda: httpx.Response(
            200,
            json={
                "status": "needs_approval",
                "summary": "synthetic summary",
                "proposed_actions": [
                    {"action_type": "calendar.create_event"}
                ],
                "memory_candidates": [{"content": "synthetic memory"}],
            },
        ),
    )

    response = client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert isinstance(response, AgentResponse)
    assert response.status == "needs_approval"
    assert response.summary == "synthetic summary"
    assert response.proposed_actions[0]["action_type"] == (
        "calendar.create_event"
    )
    assert response.memory_candidates[0]["content"] == "synthetic memory"


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (404, {"error": "unknown_agent", "detail": "synthetic detail"}),
        (400, {"error": "invalid_request", "detail": "synthetic detail"}),
        (500, {"error": "internal_error", "detail": "synthetic detail"}),
    ],
    ids=["unknown_agent", "invalid_request", "internal_error"],
)
def test_dispatch_maps_defined_error_statuses_to_runtime_error(
    status_code: int,
    body: dict[str, str],
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(
        captured,
        response_factory=lambda: httpx.Response(status_code, json=body),
    )

    with pytest.raises(RuntimeError) as error:
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert str(error.value) == f"Orchestrator returned HTTP {status_code}"

    # The Orchestrator's own error body is deliberately not carried into
    # the message the Slack Gateway then logs.
    assert "synthetic detail" not in str(error.value)


def test_dispatch_maps_connection_failure_to_runtime_error() -> None:
    client = _client_raising(
        httpx.ConnectError("synthetic connect failure detail")
    )

    with pytest.raises(RuntimeError) as error:
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert str(error.value) == "Failed to connect to the Orchestrator"
    assert "synthetic connect failure detail" not in str(error.value)


def test_dispatch_maps_timeout_to_a_distinct_runtime_error() -> None:
    """A timeout is a materially different state from an unreachable
    Orchestrator, so it gets its own bounded message rather than being
    collapsed into the connection-failure one."""
    client = _client_raising(
        httpx.ReadTimeout("synthetic timeout detail")
    )

    with pytest.raises(RuntimeError) as error:
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert str(error.value) == "Orchestrator request timed out"
    assert "synthetic timeout detail" not in str(error.value)


def test_dispatch_maps_non_json_body_to_runtime_error() -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(
        captured,
        response_factory=lambda: httpx.Response(
            200,
            content=b"synthetic not json",
        ),
    )

    with pytest.raises(RuntimeError) as error:
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert str(error.value) == "Orchestrator response was not valid JSON"
    assert "synthetic not json" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        {"status": "completed"},
        {"status": "completed", "summary": "s", "extra": "synthetic"},
        {
            "status": "",
            "summary": "s",
            "proposed_actions": [],
            "memory_candidates": [],
        },
        ["synthetic", "list"],
    ],
    ids=["missing_fields", "unknown_field", "invalid_field", "not_an_object"],
)
def test_dispatch_maps_non_agent_response_body_to_runtime_error(
    body: Any,
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(
        captured,
        response_factory=lambda: httpx.Response(200, json=body),
    )

    with pytest.raises(RuntimeError) as error:
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert str(error.value) == (
        "Orchestrator response was not a valid AgentResponse"
    )
    assert "synthetic" not in str(error.value)


def test_dispatch_failures_never_carry_the_instruction_text() -> None:
    client = _client_raising(httpx.ConnectError(SENTINEL_INSTRUCTION))

    with pytest.raises(RuntimeError) as error:
        client.dispatch(HERMES_AGENT_NAME, _agent_request())

    assert SENTINEL_INSTRUCTION not in str(error.value)
