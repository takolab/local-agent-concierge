import pytest
from opentelemetry.sdk.trace import ReadableSpan
from slack_sdk.errors import SlackApiError
from opentelemetry.trace import SpanKind, StatusCode

from slack_gateway import telemetry
from slack_gateway.orchestrator_client import (
    DispatchFailedError,
    DispatchOutcomeUnknownError,
)

# `exported_spans` comes from tests/conftest.py -- shared with
# test_slack_message_routing.py, which asserts against the same spans as
# they are produced by the real message-handling path.


def test_orchestrator_request_success_has_no_error(
    exported_spans: list[ReadableSpan],
) -> None:
    with telemetry.trace_orchestrator_request():
        pass

    span = exported_spans[-1]

    assert span.name == "orchestrator.dispatch"
    assert span.kind == SpanKind.CLIENT
    assert (
        span.attributes["concierge.downstream.service"]
        == "orchestrator"
    )
    assert span.attributes["concierge.operation"] == "dispatch"
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes
    assert len(span.events) == 0


def test_orchestrator_request_error_is_sanitized(
    exported_spans: list[ReadableSpan],
) -> None:
    raw_error = "synthetic sensitive failure detail"

    with pytest.raises(RuntimeError):
        with telemetry.trace_orchestrator_request():
            raise DispatchFailedError(raw_error)

    span = exported_spans[-1]

    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None

    assert (
        span.attributes["error.type"]
        == "orchestrator.request_error"
    )

    assert len(span.events) == 0

    assert raw_error not in str(span.attributes)

@pytest.mark.parametrize(
    ("error", "expected_error_type"),
    [
        (
            DispatchFailedError("synthetic detail"),
            "orchestrator.request_error",
        ),
        (
            DispatchOutcomeUnknownError("synthetic detail"),
            "orchestrator.outcome_unknown",
        ),
        # Not raised by OrchestratorClient -- an unclassified RuntimeError
        # from anywhere else. It must land in the unknown bucket, never be
        # recorded as a definite failure.
        (RuntimeError("synthetic detail"), "orchestrator.outcome_unknown"),
    ],
    ids=["failed", "outcome_unknown", "unclassified"],
)
def test_orchestrator_request_records_the_outcome_classification(
    exported_spans: list[ReadableSpan],
    error: RuntimeError,
    expected_error_type: str,
) -> None:
    with pytest.raises(RuntimeError):
        with telemetry.trace_orchestrator_request():
            raise error

    span = exported_spans[-1]

    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == expected_error_type
    assert "synthetic detail" not in str(span.attributes)

def test_slack_request_has_safe_attributes(
    exported_spans: list[ReadableSpan],
) -> None:
    with telemetry.trace_slack_request(threaded=True):
        pass

    span = exported_spans[-1]

    assert span.name == "concierge.request"
    assert span.kind == SpanKind.CONSUMER
    assert span.status.status_code == StatusCode.UNSET

    assert span.attributes["concierge.request.source"] == "slack"
    assert span.attributes["slack.event.type"] == "message"
    assert span.attributes["slack.message.threaded"] is True

    assert "error.type" not in span.attributes
    assert len(span.events) == 0

def test_request_child_spans_share_trace(
    exported_spans: list[ReadableSpan],
) -> None:
    with telemetry.trace_slack_request(
        threaded=False,
    ):
        with telemetry.trace_orchestrator_request():
            pass

        with telemetry.trace_slack_response():
            pass

    spans_by_name = {
        span.name: span
        for span in exported_spans
    }

    request_span = spans_by_name["concierge.request"]
    dispatch_span = spans_by_name["orchestrator.dispatch"]
    slack_span = spans_by_name["slack.response"]

    assert (
        dispatch_span.context.trace_id
        == request_span.context.trace_id
    )
    assert (
        slack_span.context.trace_id
        == request_span.context.trace_id
    )

    assert dispatch_span.parent is not None
    assert slack_span.parent is not None

    assert (
        dispatch_span.parent.span_id
        == request_span.context.span_id
    )
    assert (
        slack_span.parent.span_id
        == request_span.context.span_id
    )

    assert dispatch_span.kind == SpanKind.CLIENT
    assert slack_span.kind == SpanKind.CLIENT

def test_slack_response_error_is_sanitized(
    exported_spans: list[ReadableSpan],
) -> None:
    raw_error = "synthetic sensitive slack failure"

    with pytest.raises(SlackApiError):
        with telemetry.trace_slack_response():
            raise SlackApiError(
                message=raw_error,
                response={"ok": False},
            )

    span = exported_spans[-1]

    assert span.name == "slack.response"
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None

    assert (
        span.attributes["error.type"]
        == "slack.response_error"
    )

    assert len(span.events) == 0

    assert raw_error not in str(span.attributes)
    assert raw_error not in str(span.status)
    assert raw_error not in str(span.events)
