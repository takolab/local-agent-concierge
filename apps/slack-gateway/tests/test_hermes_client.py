from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from slack_gateway import telemetry
from slack_gateway.hermes_client import HermesClient


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


def _client_with_recorder(
    captured: dict[str, Any],
) -> HermesClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={"output_text": "hello from hermes"},
        )

    return HermesClient(
        base_url="http://hermes-agent:8642",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_create_response_injects_matching_traceparent(
    tracer: trace.Tracer,
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    with telemetry.trace_hermes_request() as hermes_span:
        expected_trace_id = format(
            hermes_span.get_span_context().trace_id, "032x"
        )
        expected_span_id = format(
            hermes_span.get_span_context().span_id, "016x"
        )

        client.create_response(
            input_text="hello",
            conversation="slack:workspace:channel:ts",
        )

    traceparent = captured["headers"]["traceparent"]
    version, trace_id, parent_id, flags = traceparent.split("-")

    assert version == "00"
    assert trace_id == expected_trace_id
    assert parent_id == expected_span_id
    assert len(flags) == 2


def test_create_response_preserves_existing_headers(
    tracer: trace.Tracer,
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    with telemetry.trace_hermes_request():
        client.create_response(
            input_text="hello",
            conversation="slack:workspace:channel:ts",
        )

    headers = captured["headers"]

    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"
    assert "traceparent" in headers


def test_create_response_without_active_span_omits_traceparent(
    tracer: trace.Tracer,
) -> None:
    captured: dict[str, Any] = {}
    client = _client_with_recorder(captured)

    client.create_response(
        input_text="hello",
        conversation="slack:workspace:channel:ts",
    )

    assert "traceparent" not in captured["headers"]
