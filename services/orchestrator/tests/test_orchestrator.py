"""Tests for orchestrator.orchestrator.Orchestrator.dispatch: explicit-name
routing, unchanged request/response pass-through, unknown-Agent rejection
with no Agent called, and unchanged Agent exception propagation.
"""

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.registry import AgentRegistry, UnknownAgentError

from stub_agents import ExplodingAgent, RecordingAgent
from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse


def _request(task_id: str = "task-1") -> AgentRequest:
    return AgentRequest(
        task_id=task_id,
        user_id="user-1",
        conversation_id="conversation-1",
        instruction="Find a two-hour study slot next week",
    )


def test_dispatch_returns_the_agents_response_unchanged():
    response = AgentResponse(status="ok", summary="Tuesday works.")
    agent = RecordingAgent(response)
    registry = AgentRegistry()
    registry.register("concierge", agent)
    orchestrator = Orchestrator(registry)

    result = orchestrator.dispatch("concierge", _request())

    assert result is response


def test_dispatch_passes_the_exact_request_object():
    agent = RecordingAgent(AgentResponse(status="ok", summary="stub"))
    registry = AgentRegistry()
    registry.register("concierge", agent)
    orchestrator = Orchestrator(registry)
    request = _request()

    orchestrator.dispatch("concierge", request)

    assert agent.calls == [request]
    assert agent.calls[0] is request


def test_dispatch_selects_agent_by_explicit_name_only():
    concierge_response = AgentResponse(status="ok", summary="concierge")
    calendar_response = AgentResponse(status="ok", summary="calendar")
    concierge = RecordingAgent(concierge_response)
    calendar = RecordingAgent(calendar_response)
    registry = AgentRegistry()
    registry.register("concierge", concierge)
    registry.register("calendar", calendar)
    orchestrator = Orchestrator(registry)
    request = _request()

    result = orchestrator.dispatch("calendar", request)

    assert result is calendar_response
    assert calendar.calls == [request]
    assert concierge.calls == []


def test_dispatch_unknown_agent_raises_unknown_agent_error():
    orchestrator = Orchestrator(AgentRegistry())

    with pytest.raises(UnknownAgentError):
        orchestrator.dispatch("missing", _request())


def test_dispatch_unknown_agent_calls_no_agent():
    agent = RecordingAgent(AgentResponse(status="ok", summary="stub"))
    registry = AgentRegistry()
    registry.register("concierge", agent)
    orchestrator = Orchestrator(registry)

    with pytest.raises(UnknownAgentError):
        orchestrator.dispatch("missing", _request())

    assert agent.calls == []


def test_dispatch_propagates_agent_exceptions_unchanged():
    error = ValueError("calendar API unavailable")
    agent = ExplodingAgent(error)
    registry = AgentRegistry()
    registry.register("calendar", agent)
    orchestrator = Orchestrator(registry)

    with pytest.raises(ValueError) as exc_info:
        orchestrator.dispatch("calendar", _request())

    assert exc_info.value is error
