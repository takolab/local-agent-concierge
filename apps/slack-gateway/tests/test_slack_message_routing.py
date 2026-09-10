"""Tests the Slack message path now that it dispatches through the
Orchestrator.

    Slack event -> AgentRequest -> Orchestrator POST /dispatch
                -> AgentResponse -> Slack thread reply

`handle_slack_message` is exercised directly (see its docstring for why it
is module-level) with a fake Slack `WebClient` and a fake
`OrchestratorClient`, so no Slack credentials, no network, and no real
message content are involved. Every value below is a synthetic sentinel.
"""

import logging
from typing import Any

import pytest
from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode
from slack_sdk.errors import SlackApiError

from slack_gateway import slack_app
from slack_gateway.event_deduplicator import EventDeduplicator
from slack_gateway.orchestrator_client import (
    HERMES_AGENT_NAME,
    DispatchFailedError,
    DispatchOutcomeUnknownError,
)

SENTINEL_TEXT = "synthetic-instruction-sentinel"
SENTINEL_USER = "U-synthetic-user"
SENTINEL_CHANNEL = "C-synthetic-channel"
SENTINEL_WORKSPACE = "T-synthetic-workspace"
SENTINEL_EVENT_ID = "Ev-synthetic-event"
SENTINEL_TS = "1700000000.000100"
SENTINEL_SUMMARY = "synthetic agent summary"


class FakeWebClient:
    """Records the Slack Web API calls the handler makes."""

    def __init__(self, post_error: SlackApiError | None = None) -> None:
        self.posted: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self._post_error = post_error

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)

        if (
            self._post_error is not None
            and kwargs["text"] != slack_app.PROCESSING_MESSAGE
        ):
            raise self._post_error

        return {"ts": f"processing-{len(self.posted)}"}

    def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(kwargs)
        return {"ok": True}

    @property
    def thread_replies(self) -> list[dict[str, Any]]:
        return [
            call
            for call in self.posted
            if call["text"] != slack_app.PROCESSING_MESSAGE
        ]


class FakeOrchestratorClient:
    """Records dispatches, and the OpenTelemetry context each ran under."""

    def __init__(
        self,
        response: AgentResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, AgentRequest]] = []
        self.span_contexts: list[trace.SpanContext] = []
        self._response = response or AgentResponse(
            status="completed",
            summary=SENTINEL_SUMMARY,
        )
        self._error = error

    def dispatch(
        self,
        agent_name: str,
        request: AgentRequest,
    ) -> AgentResponse:
        self.calls.append((agent_name, request))
        # Recorded through the OpenTelemetry API rather than by reading a
        # header, so this asserts the *relationship* the propagator would
        # act on, not a hard-coded traceparent string.
        self.span_contexts.append(
            trace.get_current_span().get_span_context()
        )

        if self._error is not None:
            raise self._error

        return self._response


def _event(**overrides: Any) -> dict[str, Any]:
    event = {
        "text": SENTINEL_TEXT,
        "channel": SENTINEL_CHANNEL,
        "user": SENTINEL_USER,
        "ts": SENTINEL_TS,
    }
    event.update(overrides)
    return {key: value for key, value in event.items() if value is not None}


def _body(**overrides: Any) -> dict[str, Any]:
    body = {"event_id": SENTINEL_EVENT_ID, "team_id": SENTINEL_WORKSPACE}
    body.update(overrides)
    return body


def _handle(
    *,
    event: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    client: FakeWebClient | None = None,
    orchestrator_client: FakeOrchestratorClient | None = None,
    event_deduplicator: EventDeduplicator | None = None,
) -> tuple[FakeWebClient, FakeOrchestratorClient]:
    client = client or FakeWebClient()
    orchestrator_client = orchestrator_client or FakeOrchestratorClient()

    slack_app.handle_slack_message(
        event=event if event is not None else _event(),
        body=body if body is not None else _body(),
        client=client,
        logger=logging.getLogger("slack_gateway.test"),
        orchestrator_client=orchestrator_client,
        event_deduplicator=event_deduplicator or EventDeduplicator(),
    )

    return client, orchestrator_client


def test_message_is_dispatched_to_the_orchestrator_hermes_agent() -> None:
    _, orchestrator_client = _handle()

    assert len(orchestrator_client.calls) == 1

    agent_name, _request = orchestrator_client.calls[0]
    assert agent_name == HERMES_AGENT_NAME


def test_dispatched_request_maps_slack_state_onto_the_agent_contract() -> None:
    _, orchestrator_client = _handle()

    _agent_name, request = orchestrator_client.calls[0]

    assert isinstance(request, AgentRequest)
    assert request.task_id == SENTINEL_EVENT_ID
    assert request.user_id == SENTINEL_USER
    assert request.conversation_id == (
        f"slack:{SENTINEL_WORKSPACE}:{SENTINEL_CHANNEL}:{SENTINEL_TS}"
    )
    assert request.instruction == SENTINEL_TEXT

    # Nothing in this path grants or asserts a permission, and no memory
    # scope is defined yet -- so neither is populated with a guess.
    assert request.memory_scopes == ()
    assert request.permissions == ()


def test_dispatched_request_trace_id_is_not_the_w3c_trace_id(
    exported_spans: list[ReadableSpan],
) -> None:
    """`AgentRequest.trace_id` is an application-level correlation field.

    The Slack Gateway deliberately leaves it unset rather than copying the
    active W3C trace id into it: propagation is the `traceparent` header's
    job, and what a caller should put in `trace_id` is still an open
    schema question (docs/agent-contracts/domain-model.md).
    """
    _, orchestrator_client = _handle()

    _agent_name, request = orchestrator_client.calls[0]
    assert request.trace_id is None

    active_trace_id = format(
        orchestrator_client.span_contexts[0].trace_id, "032x"
    )
    assert request.trace_id != active_trace_id


def test_instruction_is_stripped_like_before_the_rewiring() -> None:
    _, orchestrator_client = _handle(
        event=_event(text=f"  {SENTINEL_TEXT}  "),
    )

    _agent_name, request = orchestrator_client.calls[0]
    assert request.instruction == SENTINEL_TEXT


def test_threaded_message_keeps_the_thread_root_conversation() -> None:
    root_ts = "1699999999.000100"

    _, orchestrator_client = _handle(
        event=_event(thread_ts=root_ts),
    )

    _agent_name, request = orchestrator_client.calls[0]
    assert request.conversation_id == (
        f"slack:{SENTINEL_WORKSPACE}:{SENTINEL_CHANNEL}:{root_ts}"
    )


def test_agent_summary_is_posted_to_the_slack_thread() -> None:
    client, _ = _handle()

    replies = client.thread_replies
    assert len(replies) == 1
    assert replies[0]["text"] == SENTINEL_SUMMARY
    assert replies[0]["channel"] == SENTINEL_CHANNEL
    assert replies[0]["thread_ts"] == SENTINEL_TS

    # The temporary processing status is still posted first and removed
    # after the reply, exactly as before the rewiring.
    assert client.posted[0]["text"] == slack_app.PROCESSING_MESSAGE
    assert len(client.deleted) == 1


def test_non_completed_status_still_delivers_the_summary() -> None:
    """No `AgentResponse.status` vocabulary exists yet, so the Slack
    Gateway does not invent one -- see the comment in slack_app.py."""
    client, _ = _handle(
        orchestrator_client=FakeOrchestratorClient(
            response=AgentResponse(
                status="synthetic_unknown_status",
                summary=SENTINEL_SUMMARY,
            ),
        ),
    )

    assert client.thread_replies[0]["text"] == SENTINEL_SUMMARY


def test_definite_failure_shows_the_existing_user_facing_error() -> None:
    """A dispatch that provably did not run keeps the retry message."""
    client, _ = _handle(
        orchestrator_client=FakeOrchestratorClient(
            error=DispatchFailedError("synthetic dispatch failure detail"),
        ),
    )

    replies = client.thread_replies
    assert len(replies) == 1
    assert replies[0]["text"] == slack_app.ERROR_MESSAGE

    # The failure detail must never reach Slack.
    assert "synthetic dispatch failure detail" not in replies[0]["text"]

    # The processing status is still cleaned up on the failure path.
    assert len(client.deleted) == 1


def test_unknown_outcome_does_not_invite_a_retry() -> None:
    """An ambiguous outcome must not be presented as a safe retry.

    The Agent reachable through this path is tool-capable, so a retry
    after a possibly-delivered request can duplicate a real side effect.
    """
    client, _ = _handle(
        orchestrator_client=FakeOrchestratorClient(
            error=DispatchOutcomeUnknownError("synthetic timeout detail"),
        ),
    )

    replies = client.thread_replies
    assert len(replies) == 1

    text = replies[0]["text"]
    assert text == slack_app.UNKNOWN_OUTCOME_MESSAGE
    assert text != slack_app.ERROR_MESSAGE

    # The distinguishing property, asserted directly rather than via the
    # exact wording: the ambiguous message says the result is unknown and
    # does not tell the user to try again.
    assert "unknown" in text.lower()
    assert "try again" not in text.lower()

    assert "synthetic timeout detail" not in text

    # Cleanup behavior is identical to the definite-failure path.
    assert len(client.deleted) == 1


def test_unclassified_runtime_error_is_treated_as_unknown() -> None:
    """Fail-safe: only an explicit DispatchFailedError gets the retry
    message. Anything else -- including a RuntimeError from code this
    module did not classify -- is reported as an unknown outcome, because
    under-reporting ambiguity is the dangerous direction."""
    client, _ = _handle(
        orchestrator_client=FakeOrchestratorClient(
            error=RuntimeError("synthetic unclassified detail"),
        ),
    )

    assert client.thread_replies[0]["text"] == (
        slack_app.UNKNOWN_OUTCOME_MESSAGE
    )


def test_slack_delivery_failure_after_dispatch_is_survived() -> None:
    client = FakeWebClient(
        post_error=SlackApiError(
            message="synthetic slack failure",
            response={"ok": False},
        ),
    )

    _handle(client=client)

    assert len(client.thread_replies) == 1
    assert len(client.deleted) == 0


def test_duplicate_event_is_not_dispatched_twice() -> None:
    deduplicator = EventDeduplicator()
    orchestrator_client = FakeOrchestratorClient()

    for _ in range(2):
        _handle(
            orchestrator_client=orchestrator_client,
            event_deduplicator=deduplicator,
        )

    assert len(orchestrator_client.calls) == 1


@pytest.mark.parametrize(
    ("event", "body"),
    [
        (_event(bot_id="B-synthetic"), _body()),
        (_event(subtype="message_changed"), _body()),
        (_event(), _body(event_id=None)),
        (_event(text="   "), _body()),
        (_event(user=None), _body()),
        (_event(), _body(team_id=None)),
        (_event(channel=None), _body()),
        (_event(thread_ts=""), _body()),
    ],
    ids=[
        "bot_message",
        "subtype",
        "missing_event_id",
        "blank_text",
        "missing_user",
        "missing_workspace",
        "missing_channel",
        "invalid_thread_ts",
    ],
)
def test_ignored_events_reach_neither_the_orchestrator_nor_slack(
    event: dict[str, Any],
    body: dict[str, Any],
) -> None:
    client, orchestrator_client = _handle(event=event, body=body)

    assert orchestrator_client.calls == []
    assert client.posted == []


def test_dispatch_runs_inside_the_slack_request_trace(
    exported_spans: list[ReadableSpan],
) -> None:
    """A valid, recording context is active when the client injects.

    Asserted through the OpenTelemetry API: the span active during
    `dispatch()` is the `orchestrator.dispatch` CLIENT span, which is a
    child of the `concierge.request` span -- so whatever the configured
    propagator injects into the outgoing request continues this trace.
    """
    _, orchestrator_client = _handle()

    dispatch_context = orchestrator_client.span_contexts[0]
    assert dispatch_context.is_valid
    assert dispatch_context.trace_id != 0

    spans = {span.name: span for span in exported_spans}
    request_span = spans["concierge.request"]
    dispatch_span = spans["orchestrator.dispatch"]

    assert dispatch_span.context.span_id == dispatch_context.span_id
    assert dispatch_span.kind == SpanKind.CLIENT
    assert dispatch_span.parent.span_id == request_span.context.span_id
    assert dispatch_span.context.trace_id == request_span.context.trace_id


@pytest.mark.parametrize(
    ("error", "expected_error_type"),
    [
        (
            DispatchFailedError("synthetic dispatch failure detail"),
            "orchestrator.request_error",
        ),
        (
            DispatchOutcomeUnknownError("synthetic dispatch failure detail"),
            "orchestrator.outcome_unknown",
        ),
    ],
    ids=["failed", "outcome_unknown"],
)
def test_dispatch_failure_marks_both_spans_with_a_bounded_error(
    exported_spans: list[ReadableSpan],
    error: RuntimeError,
    expected_error_type: str,
) -> None:
    _handle(orchestrator_client=FakeOrchestratorClient(error=error))

    spans = {span.name: span for span in exported_spans}

    for name in ("concierge.request", "orchestrator.dispatch"):
        span = spans[name]
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description is None
        assert span.attributes["error.type"] == expected_error_type
        assert len(span.events) == 0
        assert "synthetic dispatch failure detail" not in str(span.attributes)


def test_no_span_carries_slack_content_or_identifiers(
    exported_spans: list[ReadableSpan],
) -> None:
    """The rewiring must not newly place sensitive values on a span."""
    _handle()

    assert exported_spans

    for span in exported_spans:
        rendered = f"{span.name} {span.attributes} {span.status} {span.events}"

        for sentinel in (
            SENTINEL_TEXT,
            SENTINEL_SUMMARY,
            SENTINEL_USER,
            SENTINEL_CHANNEL,
            SENTINEL_WORKSPACE,
            SENTINEL_EVENT_ID,
            SENTINEL_TS,
        ):
            assert sentinel not in rendered
