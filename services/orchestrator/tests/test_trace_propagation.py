"""End-to-end trace context propagation tests for the Orchestrator.

Everything here is driven the way a real caller would drive it -- real
HTTP requests into the real `OrchestratorHTTPServer`, and a real stub
Hermes HTTP server receiving the Orchestrator's real outgoing request --
so what is asserted is the *actual bytes on the wire* (the outgoing
`traceparent` header) alongside the spans an in-memory exporter recorded,
not just that some helper function was called.

Requires no Collector, no Hermes Agent, and no Ollama: spans go to an
in-memory exporter and Hermes is a local stub.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, StatusCode

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse

from orchestrator import telemetry
from orchestrator.hermes_agent import HERMES_AGENT_NAME, HermesAgent
from orchestrator.http_server import create_server
from orchestrator.orchestrator import Orchestrator
from orchestrator.registry import AgentRegistry

from stub_agents import ExplodingAgent, RecordingAgent

ECHO_AGENT_NAME = "recording"
EXPLODING_AGENT_NAME = "exploding"

HERMES_API_KEY = "test-hermes-key-do-not-log"

# A valid W3C traceparent with a known, fixed trace id and parent span id
# (the example from the W3C Trace Context specification).
INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING_SPAN_ID = "00f067aa0ba902b7"
VALID_TRACEPARENT = f"00-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01"


class RecordingSpanExporter(SpanExporter):
    """Same idiom as apps/slack-gateway/tests/test_telemetry.py."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


class _StubHermesServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _StubHermesRequestHandler)
        self.status_code = 200
        self.response_body = b'{"output_text": "stub hermes response"}'
        self.received_headers: list[dict[str, str]] = []


class _StubHermesRequestHandler(BaseHTTPRequestHandler):
    server: _StubHermesServer

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)

        self.server.received_headers.append(
            {name.lower(): value for name, value in self.headers.items()}
        )

        self.send_response(self.server.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.server.response_body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture
def exported_spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ReadableSpan]]:
    """Install a real SDK TracerProvider feeding an in-memory exporter.

    Patched onto `telemetry.trace.get_tracer` rather than installed
    globally, because OpenTelemetry's global tracer provider can only be
    set once per process -- a global install would leak between tests and
    make ordering matter.
    """
    exporter = RecordingSpanExporter()

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tracer_provider.get_tracer("orchestrator.test")

    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda _: tracer)

    try:
        yield exporter.spans
    finally:
        tracer_provider.shutdown()


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


@pytest.fixture
def running_server(
    stub_hermes: _StubHermesServer,
) -> Iterator[tuple[str, RecordingAgent]]:
    registry = AgentRegistry()

    recording_agent = RecordingAgent(
        response=AgentResponse(status="completed", summary="recorded"),
    )
    registry.register(ECHO_AGENT_NAME, recording_agent)
    registry.register(
        EXPLODING_AGENT_NAME,
        ExplodingAgent(RuntimeError("stub agent failure")),
    )
    registry.register(
        HERMES_AGENT_NAME,
        HermesAgent(
            base_url=f"http://127.0.0.1:{stub_hermes.server_address[1]}",
            api_key=HERMES_API_KEY,
        ),
    )

    server = create_server(Orchestrator(registry), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", recording_agent
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request_data(**overrides: object) -> dict:
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


def _dispatch(
    base_url: str,
    agent_name: str,
    *,
    headers: dict[str, str] | None = None,
    request_data: dict | None = None,
) -> tuple[int, dict]:
    payload = {
        "agent_name": agent_name,
        "request": request_data if request_data is not None else _request_data(),
    }
    request = urllib.request.Request(
        f"{base_url}/dispatch",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


SPAN_WAIT_SECONDS = 5.0


def _wait_for_spans(
    spans: list[ReadableSpan],
    count: int,
    *,
    name: str | None = None,
) -> list[ReadableSpan]:
    """Block until `count` spans (optionally, of one name) are exported.

    A `POST /dispatch` span is ended *after* its HTTP response has been
    written -- ending it earlier would mean it could not record the status
    it responded with. So a client that has already read the response can
    legitimately be a few microseconds ahead of the server thread, and
    asserting on the span list immediately would be flaky rather than
    wrong. This waits instead of sleeping a fixed amount.
    """
    deadline = time.monotonic() + SPAN_WAIT_SECONDS

    while True:
        matched = [span for span in spans if name is None or span.name == name]
        if len(matched) >= count:
            return matched
        if time.monotonic() >= deadline:
            recorded = [span.name for span in spans]
            raise AssertionError(
                f"timed out waiting for {count} span(s)"
                f"{'' if name is None else f' named {name!r}'}; "
                f"recorded: {recorded}"
            )
        time.sleep(0.01)


def _span_named(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    matches = _wait_for_spans(spans, 1, name=name)
    assert len(matches) == 1, f"expected exactly one {name!r} span, got {len(matches)}"
    return matches[0]


def _hex_trace_id(span: ReadableSpan) -> str:
    return format(span.get_span_context().trace_id, "032x")


def _hex_span_id(span: ReadableSpan) -> str:
    return format(span.get_span_context().span_id, "016x")


# --------------------------------------------------------------------------
# Incoming trace context -> SERVER span
# --------------------------------------------------------------------------


def test_valid_traceparent_becomes_the_parent_of_the_server_span(
    running_server, exported_spans
):
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url, ECHO_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT}
    )
    assert status == 200

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)

    assert server_span.kind is SpanKind.SERVER
    assert _hex_trace_id(server_span) == INCOMING_TRACE_ID
    assert server_span.parent is not None
    assert format(server_span.parent.span_id, "016x") == INCOMING_SPAN_ID


def test_traceparent_header_name_is_matched_case_insensitively(
    running_server, exported_spans
):
    """HTTP header names are case-insensitive; a `dict` lookup is not.

    A caller (or an intermediary) sending `Traceparent` must still be
    joined to the same trace -- if the carrier were built without
    lowercasing, this request would silently start a brand new trace.
    """
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url, ECHO_AGENT_NAME, headers={"TraceParent": VALID_TRACEPARENT}
    )
    assert status == 200

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    assert _hex_trace_id(server_span) == INCOMING_TRACE_ID


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="absent"),
        pytest.param({"traceparent": "not-a-traceparent"}, id="malformed"),
        pytest.param({"traceparent": ""}, id="empty"),
        pytest.param(
            {"traceparent": f"ff-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01"},
            id="forbidden-version-ff",
        ),
        pytest.param(
            {"traceparent": f"00-{INCOMING_TRACE_ID}-{'0' * 16}-01"},
            id="all-zero-parent-span-id",
        ),
        pytest.param(
            {"traceparent": "00-" + "0" * 32 + f"-{INCOMING_SPAN_ID}-01"},
            id="all-zero-trace-id",
        ),
    ],
)
def test_missing_or_invalid_traceparent_still_dispatches_as_a_root_span(
    running_server, exported_spans, headers
):
    base_url, recording_agent = running_server

    status, body = _dispatch(base_url, ECHO_AGENT_NAME, headers=headers)

    # The existing dispatch behavior is completely unchanged.
    assert status == 200
    assert body == {
        "status": "completed",
        "summary": "recorded",
        "proposed_actions": [],
        "memory_candidates": [],
    }
    assert len(recording_agent.calls) == 1

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    assert server_span.parent is None
    assert _hex_trace_id(server_span) != INCOMING_TRACE_ID


def test_a_future_traceparent_version_is_still_joined(
    running_server, exported_spans
):
    """W3C Trace Context requires an unknown-but-well-formed version to be
    accepted, not discarded, and OpenTelemetry's propagator implements
    that. Asserted here so the behavior is recorded as deliberate: this
    service defers to the propagator rather than deciding for itself which
    versions are acceptable.
    """
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url,
        ECHO_AGENT_NAME,
        headers={"traceparent": f"99-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01"},
    )
    assert status == 200

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    assert _hex_trace_id(server_span) == INCOMING_TRACE_ID


def test_health_endpoint_produces_no_span(running_server, exported_spans):
    base_url, _ = running_server

    with urllib.request.urlopen(f"{base_url}/health") as response:
        assert response.status == 200

    # A negative assertion cannot be waited for, so give the server thread
    # a chance to be wrong before concluding it was not.
    time.sleep(0.2)
    assert exported_spans == []


# --------------------------------------------------------------------------
# Outgoing trace context -> CLIENT span -> Hermes
# --------------------------------------------------------------------------


def test_server_client_and_outgoing_header_share_one_trace(
    running_server, stub_hermes, exported_spans
):
    """The whole contract of this slice, asserted in one place."""
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url, HERMES_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT}
    )
    assert status == 200

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    client_span = _span_named(exported_spans, telemetry.HERMES_SPAN_NAME)

    assert client_span.kind is SpanKind.CLIENT

    # 1. The CLIENT span is a child of the SERVER span...
    assert client_span.parent is not None
    assert client_span.parent.span_id == server_span.get_span_context().span_id

    # 2. ...and both continue the caller's trace.
    assert _hex_trace_id(server_span) == INCOMING_TRACE_ID
    assert _hex_trace_id(client_span) == INCOMING_TRACE_ID

    # 3. The header Hermes actually received names the CLIENT span as its
    #    parent, on the same trace.
    assert len(stub_hermes.received_headers) == 1
    outgoing = stub_hermes.received_headers[0]["traceparent"]

    version, trace_id, parent_span_id, _flags = outgoing.split("-")
    assert version == "00"
    assert trace_id == INCOMING_TRACE_ID
    assert parent_span_id == _hex_span_id(client_span)


def test_outgoing_request_keeps_its_authorization_and_content_type(
    running_server, stub_hermes, exported_spans
):
    """Injection must add headers, never replace the ones already set."""
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url, HERMES_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT}
    )
    assert status == 200

    outgoing = stub_hermes.received_headers[0]
    assert outgoing["authorization"] == f"Bearer {HERMES_API_KEY}"
    assert outgoing["content-type"] == "application/json"
    assert "traceparent" in outgoing


def test_caller_supplied_baggage_does_not_reach_hermes_or_span_data(
    running_server, stub_hermes, exported_spans
):
    """Pins observed behavior around the one part of the standard
    propagator this service does not want: `baggage`.

    OpenTelemetry's default propagator is `tracecontext,baggage`, so
    `extract()` does parse a caller's `baggage` header into the extracted
    context. It nevertheless does not reach Hermes, because
    `start_as_current_span(context=...)` uses the passed context only to
    resolve the parent span and then attaches the new span onto the
    *ambient* context -- so the extracted context's baggage is not what
    `inject()` later reads from.

    Asserted so that a future change (extracting into a context this code
    attaches directly, say) cannot silently start forwarding arbitrary
    caller-supplied key/values to an internal service on an endpoint that
    has no authentication. This is a regression tripwire, not a security
    control: a deployment that needs the guarantee should set the
    standard `OTEL_PROPAGATORS=tracecontext`. Either way, nothing here
    hand-rolls a propagator.
    """
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url,
        HERMES_AGENT_NAME,
        headers={"traceparent": VALID_TRACEPARENT, "baggage": "tenant=acme"},
    )
    assert status == 200

    outgoing = stub_hermes.received_headers[0]

    # The trace context is forwarded...
    assert outgoing["traceparent"].split("-")[1] == INCOMING_TRACE_ID
    # ...the caller's baggage is not.
    assert "baggage" not in outgoing

    # And it is not span data either.
    assert "tenant" not in _dump_spans(exported_spans)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(
            {
                "traceparent": f"00-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01",
                "tracestate": "=====,,,,@@@@",
            },
            id="malformed-tracestate",
        ),
        pytest.param({"traceparent": "00-" + "a" * 4000}, id="oversized"),
        pytest.param(
            {"traceparent": f"  00-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01  "},
            id="whitespace-padded",
        ),
        pytest.param({"baggage": "k=v"}, id="baggage-without-traceparent"),
    ],
)
def test_hostile_trace_headers_never_fail_a_dispatch(
    running_server, exported_spans, headers
):
    """Header handling must not become a new way to break dispatch."""
    base_url, _ = running_server

    status, body = _dispatch(base_url, ECHO_AGENT_NAME, headers=headers)

    assert status == 200
    assert body["summary"] == "recorded"


def test_hermes_call_without_a_tracer_provider_sends_no_traceparent(
    running_server, stub_hermes
):
    """No `exported_spans` fixture here: nothing is recording.

    This is the "telemetry disabled" case -- the OpenTelemetry API's
    no-op tracer. Dispatch must behave identically, and the outgoing
    request must simply carry no trace context rather than a bogus one.
    """
    base_url, _ = running_server

    status, body = _dispatch(base_url, HERMES_AGENT_NAME)

    assert status == 200
    assert body["summary"] == "stub hermes response"
    assert "traceparent" not in stub_hermes.received_headers[0]


# --------------------------------------------------------------------------
# AgentRequest.trace_id is not W3C trace context
# --------------------------------------------------------------------------


def test_json_trace_id_neither_overrides_nor_is_altered_by_http_context(
    running_server, stub_hermes, exported_spans
):
    """`AgentRequest.trace_id` and the HTTP trace context are separate.

    A caller sends a JSON `trace_id` that is deliberately *not* the
    incoming W3C trace id. The Orchestrator must propagate the HTTP one
    and leave the JSON one exactly as received.
    """
    base_url, _ = running_server
    json_trace_id = "logical-correlation-id-not-a-w3c-trace-id"

    status, _ = _dispatch(
        base_url,
        HERMES_AGENT_NAME,
        headers={"traceparent": VALID_TRACEPARENT},
        request_data=_request_data(trace_id=json_trace_id),
    )
    assert status == 200

    # Propagation follows the HTTP context, not the JSON field.
    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    assert _hex_trace_id(server_span) == INCOMING_TRACE_ID
    assert stub_hermes.received_headers[0]["traceparent"].split("-")[1] == (
        INCOMING_TRACE_ID
    )

    # The JSON field is neither rewritten nor forwarded to Hermes.
    assert json_trace_id not in json.dumps(_span_attributes(server_span))


def test_agent_receives_the_request_unmodified_when_tracing_is_active(
    running_server, exported_spans
):
    base_url, recording_agent = running_server
    request_data = _request_data(trace_id="caller-supplied-trace-id")

    status, _ = _dispatch(
        base_url,
        ECHO_AGENT_NAME,
        headers={"traceparent": VALID_TRACEPARENT},
        request_data=request_data,
    )
    assert status == 200

    assert recording_agent.calls == [
        AgentRequest(
            task_id="task-1",
            user_id="user-1",
            conversation_id="conversation-1",
            instruction="do something",
            memory_scopes=(),
            permissions=(),
            trace_id="caller-supplied-trace-id",
        )
    ]


# --------------------------------------------------------------------------
# Context isolation between requests, and after failures
# --------------------------------------------------------------------------


def test_two_requests_with_different_traceparents_do_not_share_context(
    running_server, exported_spans
):
    base_url, _ = running_server
    other_trace_id = "0af7651916cd43dd8448eb211c80319c"
    other_span_id = "b7ad6b7169203331"

    _dispatch(base_url, ECHO_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT})
    _dispatch(
        base_url,
        ECHO_AGENT_NAME,
        headers={"traceparent": f"00-{other_trace_id}-{other_span_id}-01"},
    )

    server_spans = _wait_for_spans(
        exported_spans, 2, name=telemetry.DISPATCH_SPAN_NAME
    )
    assert len(server_spans) == 2

    first, second = server_spans
    assert _hex_trace_id(first) == INCOMING_TRACE_ID
    assert _hex_trace_id(second) == other_trace_id
    assert format(second.parent.span_id, "016x") == other_span_id


def test_a_traced_request_does_not_leak_context_into_an_untraced_one(
    running_server, exported_spans
):
    """A request with no `traceparent` immediately after a traced one must
    start a fresh root trace, not inherit the previous request's context.

    `http.server`'s keep-alive handling can serve several requests from
    one handler instance on one thread, so a span left attached would be
    inherited here.
    """
    base_url, _ = running_server

    _dispatch(base_url, ECHO_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT})
    _dispatch(base_url, ECHO_AGENT_NAME)

    server_spans = _wait_for_spans(
        exported_spans, 2, name=telemetry.DISPATCH_SPAN_NAME
    )
    assert len(server_spans) == 2
    assert server_spans[1].parent is None
    assert _hex_trace_id(server_spans[1]) != INCOMING_TRACE_ID


def test_context_does_not_survive_an_agent_exception(running_server, exported_spans):
    base_url, _ = running_server

    status, body = _dispatch(
        base_url, EXPLODING_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT}
    )

    # The pre-existing error contract is untouched.
    assert status == 500
    assert body["error"] == "internal_error"

    # The next request starts clean.
    _dispatch(base_url, ECHO_AGENT_NAME)

    server_spans = _wait_for_spans(
        exported_spans, 2, name=telemetry.DISPATCH_SPAN_NAME
    )
    assert len(server_spans) == 2
    assert server_spans[1].parent is None

    # And no span is left unfinished by the exception path.
    for span in server_spans:
        assert span.end_time is not None
    assert trace.get_current_span() is trace.INVALID_SPAN


# --------------------------------------------------------------------------
# Status and bounded error classification
# --------------------------------------------------------------------------


def test_successful_dispatch_records_status_200_and_no_error(
    running_server, exported_spans
):
    base_url, _ = running_server

    _dispatch(base_url, ECHO_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT})

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    attributes = _span_attributes(server_span)

    assert attributes["http.status_code"] == 200
    assert attributes["http.method"] == "POST"
    assert attributes["http.route"] == "/dispatch"
    assert attributes["concierge.operation"] == "dispatch"
    assert "error.type" not in attributes
    assert server_span.status.status_code is not StatusCode.ERROR


def test_unknown_agent_records_404_without_marking_the_span_failed(
    running_server, exported_spans
):
    """A 4xx is the caller's error, not the server span's."""
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url, "does-not-exist", headers={"traceparent": VALID_TRACEPARENT}
    )
    assert status == 404

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    attributes = _span_attributes(server_span)

    assert attributes["http.status_code"] == 404
    assert "error.type" not in attributes
    assert server_span.status.status_code is not StatusCode.ERROR


def test_agent_exception_records_500_with_a_bounded_error_type(
    running_server, exported_spans
):
    base_url, _ = running_server

    status, _ = _dispatch(
        base_url, EXPLODING_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT}
    )
    assert status == 500

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    attributes = _span_attributes(server_span)

    assert attributes["http.status_code"] == 500
    assert attributes["error.type"] == telemetry.ERROR_TYPE_DISPATCH_SERVER_ERROR
    assert server_span.status.status_code is StatusCode.ERROR


def test_hermes_non_success_status_keeps_the_existing_error_response(
    running_server, stub_hermes, exported_spans
):
    base_url, _ = running_server
    stub_hermes.status_code = 503
    stub_hermes.response_body = b'{"error": "hermes is unhappy"}'

    status, body = _dispatch(
        base_url, HERMES_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT}
    )

    assert status == 500
    assert body == {
        "error": "internal_error",
        "detail": "An unexpected error occurred while dispatching the request.",
    }

    client_span = _span_named(exported_spans, telemetry.HERMES_SPAN_NAME)
    attributes = _span_attributes(client_span)

    assert attributes["error.type"] == telemetry.ERROR_TYPE_HERMES_HTTP_STATUS
    assert attributes["http.status_code"] == 503
    assert client_span.status.status_code is StatusCode.ERROR


def test_hermes_connection_failure_keeps_the_existing_error_response(
    exported_spans,
):
    # Bound and immediately close a port so the connection is refused.
    unreachable = _closed_port()
    agent = HermesAgent(
        base_url=f"http://127.0.0.1:{unreachable}", api_key=HERMES_API_KEY
    )

    with pytest.raises(RuntimeError, match="Failed to connect to Hermes API"):
        agent.handle(
            AgentRequest(
                task_id="task-1",
                user_id="user-1",
                conversation_id="conversation-1",
                instruction="do something",
            )
        )

    client_span = _span_named(exported_spans, telemetry.HERMES_SPAN_NAME)
    attributes = _span_attributes(client_span)

    assert attributes["error.type"] == telemetry.ERROR_TYPE_HERMES_CONNECTION
    assert client_span.status.status_code is StatusCode.ERROR


def test_hermes_unusable_body_is_classified_as_an_invalid_response(
    running_server, stub_hermes, exported_spans
):
    base_url, _ = running_server
    stub_hermes.response_body = b'{"output": []}'

    status, _ = _dispatch(base_url, HERMES_AGENT_NAME)
    assert status == 500

    client_span = _span_named(exported_spans, telemetry.HERMES_SPAN_NAME)
    assert (
        _span_attributes(client_span)["error.type"]
        == telemetry.ERROR_TYPE_HERMES_INVALID_RESPONSE
    )


def test_every_recorded_error_type_comes_from_the_declared_vocabulary(
    running_server, stub_hermes, exported_spans
):
    """Guards the "bounded error classification" property directly.

    If a future change starts attaching, say, an exception's own class
    name or message as `error.type`, this fails.
    """
    base_url, _ = running_server

    _dispatch(base_url, EXPLODING_AGENT_NAME)
    stub_hermes.status_code = 500
    _dispatch(base_url, HERMES_AGENT_NAME)
    stub_hermes.status_code = 200
    stub_hermes.response_body = b"not json at all"
    _dispatch(base_url, HERMES_AGENT_NAME)

    _wait_for_spans(exported_spans, 5)

    recorded = {
        span.attributes["error.type"]
        for span in exported_spans
        if "error.type" in span.attributes
    }
    assert recorded
    assert recorded <= telemetry.ERROR_TYPES


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_no_span_carries_instruction_text_credentials_or_identifiers(
    running_server, stub_hermes, exported_spans
):
    """Distinctive sentinels, so a leak is unambiguous rather than a
    guess about which generic placeholder ended up where.
    """
    base_url, _ = running_server

    instruction = "SENTINEL-INSTRUCTION-c0ffee-book me a dentist appointment"
    conversation_id = "SENTINEL-CONVERSATION-c0ffee"
    user_id = "SENTINEL-USER-c0ffee"
    task_id = "SENTINEL-TASK-c0ffee"
    hermes_reply = "SENTINEL-HERMES-REPLY-c0ffee"

    stub_hermes.response_body = json.dumps({"output_text": hermes_reply}).encode()

    status, body = _dispatch(
        base_url,
        HERMES_AGENT_NAME,
        headers={"traceparent": VALID_TRACEPARENT},
        request_data=_request_data(
            instruction=instruction,
            conversation_id=conversation_id,
            user_id=user_id,
            task_id=task_id,
        ),
    )
    assert status == 200
    assert body["summary"] == hermes_reply  # it really did flow through

    _wait_for_spans(exported_spans, 2)

    dumped = _dump_spans(exported_spans)
    for sentinel in (
        instruction,
        conversation_id,
        user_id,
        task_id,
        hermes_reply,
        HERMES_API_KEY,
        "Bearer",
        "authorization",
    ):
        assert sentinel.lower() not in dumped.lower(), (
            f"{sentinel!r} leaked into span data"
        )


def test_hermes_failure_records_no_exception_event_on_the_client_span(
    running_server, stub_hermes, exported_spans
):
    """The CLIENT span is the one an exception actually propagates through.

    `hermes_agent.handle()` lets its `RuntimeError` escape
    `trace_hermes_request()`'s context manager, so this is where
    `record_exception=False` / `set_status_on_exception=False` are
    load-bearing: with the SDK defaults, the raised exception would be
    attached to the span as an `exception` event carrying
    `exception.message` and `exception.stacktrace`.

    (The SERVER span cannot be checked the same way: `_handle_dispatch`
    catches every exception itself and returns normally, so nothing is
    ever raised through that span. Its `record_exception=False` is
    defense-in-depth for a future path that does raise.)
    """
    base_url, _ = running_server
    stub_hermes.status_code = 503

    status, _ = _dispatch(base_url, HERMES_AGENT_NAME)
    assert status == 500

    client_span = _span_named(exported_spans, telemetry.HERMES_SPAN_NAME)

    assert client_span.events == ()
    assert "exception.message" not in (client_span.attributes or {})
    assert "exception.stacktrace" not in (client_span.attributes or {})
    assert client_span.status.description is None


def test_agent_exception_message_never_reaches_span_data(
    exported_spans, stub_hermes
):
    """No span may carry an Agent exception's message or type.

    True today because nothing raised inside `_handle_dispatch` reaches a
    span at all -- asserted anyway so that a future change which *does*
    route an exception onto the SERVER span cannot do so silently.
    """
    registry = AgentRegistry()
    secret_message = "SENTINEL-EXCEPTION-c0ffee leaked the user's instruction"
    registry.register(EXPLODING_AGENT_NAME, ExplodingAgent(ValueError(secret_message)))

    server = create_server(Orchestrator(registry), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        status, _ = _dispatch(base_url, EXPLODING_AGENT_NAME)
        assert status == 500
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    _wait_for_spans(exported_spans, 1, name=telemetry.DISPATCH_SPAN_NAME)

    dumped = _dump_spans(exported_spans)
    assert "SENTINEL-EXCEPTION-c0ffee" not in dumped
    assert "ValueError" not in dumped

    server_span = _span_named(exported_spans, telemetry.DISPATCH_SPAN_NAME)
    assert server_span.events == ()


def test_span_attribute_keys_are_limited_to_the_expected_set(
    running_server, stub_hermes, exported_spans
):
    base_url, _ = running_server

    _dispatch(base_url, HERMES_AGENT_NAME, headers={"traceparent": VALID_TRACEPARENT})
    stub_hermes.status_code = 500
    _dispatch(base_url, HERMES_AGENT_NAME)

    _wait_for_spans(exported_spans, 4)

    allowed_keys = {
        "concierge.operation",
        "concierge.downstream.service",
        "http.method",
        "http.route",
        "http.status_code",
        "error.type",
    }

    for span in exported_spans:
        unexpected = set(span.attributes) - allowed_keys
        assert not unexpected, f"{span.name} carries unexpected attributes: {unexpected}"


# --------------------------------------------------------------------------
# Telemetry configuration
# --------------------------------------------------------------------------


def test_configure_tracing_returns_none_when_the_sdk_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert telemetry.configure_tracing() is None


def test_mark_current_span_error_rejects_an_undeclared_error_type():
    with pytest.raises(ValueError):
        telemetry.mark_current_span_error(error_type="something.invented")


def test_trace_context_headers_are_empty_without_an_active_span():
    assert telemetry.trace_context_headers() == {}


def _span_attributes(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _dump_spans(spans: Sequence[ReadableSpan]) -> str:
    """Everything a span would carry to the Collector, as one string."""
    return json.dumps(
        [
            {
                "name": span.name,
                "attributes": {
                    key: str(value) for key, value in (span.attributes or {}).items()
                },
                "status_description": span.status.description,
                "events": [
                    {
                        "name": event.name,
                        "attributes": {
                            key: str(value)
                            for key, value in (event.attributes or {}).items()
                        },
                    }
                    for event in span.events
                ],
                "resource": {
                    key: str(value)
                    for key, value in span.resource.attributes.items()
                },
            }
            for span in spans
        ]
    )


def _closed_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
